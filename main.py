#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""gta6-twitch-ccu-forecast-01 entry (round 2).

Kernel-weighted comparable-ratio forecast for Twitch concurrent viewers on
GTA VI's release day (2026-11-19 UTC). Reads one case per input file (schema
"makefun/twitch-ccu-case/v1") from $MAKEFUN_INPUT_DIR (else /input) and writes
one JSON output per case to $MAKEFUN_OUTPUT_DIR (else /output): 23 rows,
hours 2-24, five quantiles each.

Model (implemented fresh from the R&D design; see the analysis dir's
IMPROVED_DESIGN.md for the validation numbers):
- Every comparable launch contributes its hourly ratio curve (viewers /
  hour-1 print), weighted by a Gaussian kernel on log hour-1 size
  (bandwidth 1.0): launches of similar size have similar shapes. Tiny-p1
  launches surge 10-50x from hour 1, big-p1 launches barely double, and one
  global median curve cannot fit both.
- Ratio curve r(h): kernel-weighted median per hour, 3h-smoothed, plus the
  SPEC_DELTA US-unlock-ramp nudge; deviation from its own 3h smooth capped
  at +-0.35 in log.
- Sigma(h): kernel-weighted sd of winsorized (5th/95th) log-residuals,
  clipped to [0.15, 1.2].
- Quantiles are lognormal: q = p1 * r(h) * anchor * exp(sigma(h) * z).
- Stream anchor (level only, never shape): clipped to [0.8, 1.25],
  log-shrunk by 0.5.
- Validity enforced: every q finite, >0, <=1e9, non-decreasing within a
  row, monotone across hours per quantile level.

Stdlib only, deterministic (no randomness, no timestamps, comparables
sorted by name), no network. Python 3.12 and 3.13 compatible.
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
Z = (-1.645, -0.674, 0.0, 0.674, 1.645)
BANDWIDTH = 1.0
SPEC_DELTA = {
    2: -0.002, 3: -0.005, 4: -0.005, 5: 0.004, 6: 0.002,
    7: 0.058, 8: 0.055, 9: 0.106, 10: 0.112, 11: 0.123, 12: 0.162,
    13: 0.189, 14: 0.185, 15: 0.052, 16: 0.074, 17: 0.096, 18: 0.074,
    19: -0.065, 20: -0.039, 21: -0.035, 22: -0.053, 23: -0.062, 24: -0.070,
}
SIGMA_FLOOR = 0.15
SIGMA_CAP = 1.2
SIGMA_SCALE = 1.0
WINSOR = 0.05
CAP_DEV = 0.35
ANCHOR_K = 0.7
ANCHOR_CLIP = (0.8, 1.25)
ANCHOR_SHRINK = 0.5
CATEGORY_WIDEN = 0.05


def _parse_time(text):
    """Parse an ISO-8601 UTC timestamp (with a trailing Z) to a datetime."""
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def _median(xs):
    """Median of a non-empty list of numbers (linear interpolation)."""
    s = sorted(xs)
    n = len(s)
    mid = (n - 1) / 2.0
    lo = int(mid)
    hi = min(lo + 1, n - 1)
    return s[lo] + (s[hi] - s[lo]) * (mid - lo)


def _percentile(sorted_xs, p):
    """p-th percentile (0..1) of an already-sorted list, linear interpolation."""
    n = len(sorted_xs)
    if n == 1:
        return sorted_xs[0]
    i = p * (n - 1)
    lo = int(i)
    hi = min(lo + 1, n - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (i - lo)


def _weighted_median(pairs):
    """Weighted median: pairs = [(value, weight)]. Returns the value where
    cumulative weight (sorted by value) first reaches half the total weight."""
    total = sum(w for _, w in pairs)
    if total <= 0:
        return _median([v for v, _ in pairs])
    acc = 0.0
    for value, weight in sorted(pairs, key=lambda p: p[0]):
        acc += weight
        if acc >= 0.5 * total:
            return value
    return sorted(pairs, key=lambda p: p[0])[-1][0]


def _smooth3(values):
    """3-hour centered smooth of a dict {hour: value} for hours 2..24.
    At the ends, average the two available hours."""
    out = {}
    for h in range(2, 25):
        near = [values[k] for k in (h - 1, h, h + 1) if k in values]
        out[h] = sum(near) / len(near)
    return out


def _build_table(case):
    """Build the comparable table: a list of (name, p1, {hour: viewers})
    sorted by (name, day_start), using only the release-day 24 hours.
    Drops a comparable when any of the 24 hours is missing, when any hourly
    viewer count is not a positive number, or when its hour-1 print is <= 0."""
    table = []
    for comp in case.get("comparables") or []:
        try:
            day_start = _parse_time(comp["day_start"])
        except Exception:
            continue
        by_time = {}
        for pt in comp.get("hourly") or []:
            try:
                by_time[_parse_time(pt["from"])] = pt["viewers"]
            except Exception:
                continue
        hours = {}
        ok = True
        for h in range(1, 25):
            t = day_start + timedelta(hours=h - 1)
            v = by_time.get(t)
            if not isinstance(v, (int, float)) or isinstance(v, bool) \
                    or not math.isfinite(v) or v <= 0:
                ok = False
                break
            hours[h] = float(v)
        if not ok:
            continue
        p1_c = hours[1]
        if p1_c <= 0:
            continue
        table.append((str(comp.get("name", "")), str(comp.get("day_start", "")),
                      p1_c, hours))
    table.sort(key=lambda row: (row[0], row[1]))
    return table


def _hour1_print(case):
    """The target's hour-1 print (viewers) from the known rows, or None."""
    known = case.get("known") or []
    for row in known:
        if isinstance(row, dict) and row.get("hour") == 1:
            return row.get("viewers")
    if known and isinstance(known[0], dict):
        return known[0].get("viewers")
    return None


def _stream_anchor(case, p1):
    """Level-only anchor from the top-20 live streams at the last read.
    Returns 1.0 when there are no streams (practice cases) or bad data."""
    streams = case.get("streams")
    total = None
    if isinstance(streams, list) and streams:
        vals = []
        for s in streams:
            if isinstance(s, dict) and isinstance(s.get("viewers"), (int, float)) \
                    and math.isfinite(s["viewers"]) and s["viewers"] > 0:
                vals.append(float(s["viewers"]))
        if vals:
            total = sum(vals)
    elif isinstance(streams, dict):
        for key in ("total", "sum"):
            v = streams.get(key)
            if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
                total = float(v)
                break
    if total is None or total <= 0 or p1 is None or p1 <= 0:
        return 1.0
    raw = total / (ANCHOR_K * p1)
    clipped = min(ANCHOR_CLIP[1], max(ANCHOR_CLIP[0], raw))
    return math.exp(ANCHOR_SHRINK * math.log(clipped))


def _category_widen(case):
    """True when GTA VI's category is ranked <= 3 and growing > 1.5x/hr in
    the latest categories_hourly rows. Defensive: any unparsable shape
    returns False (the rule is a small, untested widening only)."""
    try:
        rows = case.get("categories_hourly") or []
        if len(rows) < 2:
            return False
        ordered = sorted(rows, key=lambda r: str(r.get("from", "")))
        latest, prev = ordered[-1], ordered[-2]

        def gta_viewers(row):
            top = row.get("top") or []
            for i, cat in enumerate(top):
                name = str(cat.get("name", ""))
                if "Grand Theft Auto VI" in name:
                    return i + 1, float(cat.get("viewers"))
            return None, None

        rank, now = gta_viewers(latest)
        _, before = gta_viewers(prev)
        if rank is None or now is None or before is None or before <= 0:
            return False
        return rank <= 3 and (now / before) > 1.5
    except Exception:
        return False


def forecast(case):
    """Forecast one case. Returns {hour: [q05, q25, q50, q75, q95]} for hours
    2..24, all finite, >0, <=1e9, non-decreasing within each row and monotone
    across hours per level."""
    table = _build_table(case)

    # --- hour-1 print + fallback (design 1.9) ---
    p1 = _hour1_print(case)
    if isinstance(p1, bool) or not isinstance(p1, (int, float)) \
            or not math.isfinite(p1):
        p1 = None
    sigma_boost = 0.0
    if p1 is None or p1 <= 0:
        p1 = 1000.0
        sigma_boost = 0.8
    elif p1 < 50:
        comp_p1s = [row[2] for row in table]
        med = _median(comp_p1s) if comp_p1s else 0.0
        p1 = max(1632.0, 0.5 * med)
        sigma_boost = 0.15
    p1 = float(p1)

    # --- kernel weights on log hour-1 size (design 1.3) ---
    log_p1 = math.log(max(p1, 1.0))
    weights = []
    for _name, _day, p1_c, _hours in table:
        d = (math.log(max(p1_c, 1.0)) - log_p1) / BANDWIDTH
        weights.append(math.exp(-0.5 * d * d))

    # --- ratio curve r(h) (design 1.4) ---
    if table:
        raw_log = {}
        for h in range(2, 25):
            pairs = [(hours[h] / p1_c, w)
                     for w, (_n, _d, p1_c, hours) in zip(weights, table)]
            raw_log[h] = math.log(max(_weighted_median(pairs), 1e-9))
        smoothed = _smooth3(raw_log)
        final_log = {h: smoothed[h] + SPEC_DELTA[h] for h in range(2, 25)}
        smooth_final = _smooth3(final_log)
        for h in range(2, 25):
            dev = final_log[h] - smooth_final[h]
            if dev > CAP_DEV:
                final_log[h] = smooth_final[h] + CAP_DEV
            elif dev < -CAP_DEV:
                final_log[h] = smooth_final[h] - CAP_DEV
        ratio = {h: math.exp(final_log[h]) for h in range(2, 25)}
    else:
        # Degenerate training (no comparables): flat curve, wide sigma.
        ratio = {h: 1.0 for h in range(2, 25)}

    # --- sigma(h) (design 1.5) ---
    widen = _category_widen(case)
    sigma = {}
    for h in range(2, 25):
        if table:
            res = []
            for w, (_n, _d, p1_c, hours) in zip(weights, table):
                res.append(math.log(hours[h] / (p1_c * ratio[h])))
            sres = sorted(res)
            lo = _percentile(sres, WINSOR)
            hi = _percentile(sres, 1.0 - WINSOR)
            clipped = [min(hi, max(lo, r)) for r in res]
            total_w = sum(weights)
            if total_w > 0:
                mean_w = sum(w * r for w, r in zip(weights, clipped)) / total_w
                var_w = sum(w * (r - mean_w) ** 2
                            for w, r in zip(weights, clipped)) / total_w
                sig = math.sqrt(max(var_w, 0.0))
            else:
                sig = 0.8
        else:
            sig = 0.8
        sig = min(SIGMA_CAP, max(SIGMA_FLOOR, SIGMA_SCALE * sig))
        sig = min(SIGMA_CAP, sig + sigma_boost
                  + (CATEGORY_WIDEN if (widen and h >= 15) else 0.0))
        sigma[h] = sig

    # --- quantiles (design 1.6) + validity (design 1.10) ---
    anchor = _stream_anchor(case, p1)
    out = {}
    prev_row = None
    for h in range(2, 25):
        row = []
        for z in Z:
            q = p1 * ratio[h] * anchor * math.exp(sigma[h] * z)
            if not math.isfinite(q) or q <= 0:
                q = 1.0
            q = min(q, 1e9)
            row.append(q)
        for i in range(1, 5):
            row[i] = max(row[i], row[i - 1])
        if prev_row is not None:
            row = [max(row[i], prev_row[i]) for i in range(5)]
        out[h] = row
        prev_row = row
    return out


def _write_output(out_dir, case_id, forecast_rows):
    """Write one <case_id>.out file (JSON, mode 0644 — the grader must be
    able to read it; a 0600 file is rejected). The mode is set explicitly
    with os.chmod after writing: plain open() alone inherits the caller's
    umask, and the 0600 files seen in a grader-sim OUT dir are a
    cross-filesystem cp -a artifact, not the entry — the files the entry
    writes inside the sandbox are 0644 (verified as user nobody)."""
    data = {"hours": [{"hour": h, "q": forecast_rows[h]}
                      for h in range(2, 25)]}
    text = json.dumps(data, separators=(",", ":"))
    path = Path(out_dir) / (case_id + ".out")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o644)
    return path


def main():
    in_dir = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
    out_dir = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    for in_path in sorted(in_dir.glob("*.in")):
        with open(in_path, "r", encoding="utf-8") as f:
            case = json.load(f)
        case_id = case.get("id") or in_path.stem
        rows = forecast(case)
        _write_output(out_dir, str(case_id), rows)


if __name__ == "__main__":
    main()
