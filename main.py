"""Deterministic launch-day forecasts from robust comparable launch curves."""
import json
import math
import os
from datetime import datetime
from pathlib import Path
from statistics import median, pstdev


Z = (-1.645, -0.674, 0.0, 0.674, 1.645)
LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
RAW = (.87, .75, .73, .75, .79, .80, .90, .88, .91, .93, .98,
       1.06, 1.18, 1.52, 1.65, 1.77, 1.95, 2.40, 2.37, 2.33,
       2.30, 2.18, 2.04)
ADJUSTED = RAW[:5] + (.85, .95, .98, 1.02, 1.05, 1.15, 1.28,
                           1.42, 1.60, 1.78, 1.95, 2.10, 2.25,
                           2.28, 2.25, 2.18, 2.05, 1.90)


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def stream_sum(streams):
    if isinstance(streams, dict):
        streams = streams.get('streams', streams.get('top', []))
    if not isinstance(streams, list):
        return None
    values = [number(row.get('viewers', row.get('viewer_count')))
              for row in streams[:20] if isinstance(row, dict)]
    values = [value for value in values if value is not None and value >= 0]
    return sum(values) if values else None


def comparable_curves(case):
    curves, anchors, calibrations = [], [], []
    seen = set()
    for comp in case.get('comparables') or []:
        start = comp.get('day_start')
        if not start:
            continue
        identity = (comp.get('name'), start)
        if identity in seen:
            continue
        seen.add(identity)
        start_time = timestamp(start)
        hourly = {}
        for row in comp.get('hourly') or []:
            if not row.get('from'):
                continue
            offset = (timestamp(row['from']) - start_time).total_seconds() / 3600
            value = number(row.get('viewers'))
            if offset.is_integer() and 0 <= offset < 24 and value is not None and value > 0:
                hourly[int(offset) + 1] = value
        anchor = hourly.get(1)
        if anchor is None:
            continue
        anchors.append(anchor)
        # Exclude midday unlocks whose tiny hour-one denominator dominates ratios.
        if anchor < 50 or anchor < .01 * max(hourly.values()):
            continue
        curves.append({h: math.log(v / anchor) for h, v in hourly.items() if h >= 2})
        total = stream_sum(comp.get('streams'))
        if total is not None and total > 0:
            calibrations.append(total / anchor)
    return curves, anchors, calibrations


def spreads(curves, shape):
    result = []
    for index, ratio in enumerate(shape):
        errors = [curve[index + 2] - math.log(ratio)
                  for curve in curves if index + 2 in curve]
        if len(errors) < 2:
            result.append(.8)
            continue
        lower, upper = percentile(errors, .10), percentile(errors, .90)
        result.append(pstdev([max(lower, min(upper, v)) for v in errors]))
    return result


def comparable_loss(curves, shape, sigmas):
    losses = []
    for curve in curves:
        for index, ratio in enumerate(shape):
            actual = curve.get(index + 2)
            if actual is None:
                continue
            for level, z in zip(LEVELS, Z):
                error = actual - (math.log(ratio) + sigmas[index] * z)
                losses.append(max(level * error, (level - 1) * error))
    return sum(losses) / len(losses) if losses else 0.0


def category_surge(case):
    snapshots = case.get('categories_hourly') or []
    if not isinstance(snapshots, list):
        return False
    matches = []
    game = case.get('game') or {}
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        rows = snapshot.get('categories', snapshot.get('top', []))
        if not isinstance(rows, list):
            continue
        for rank, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            same = (str(row.get('twitch_id', row.get('id', ''))) == str(game.get('twitch_id'))
                    or row.get('name') == game.get('name'))
            value = number(row.get('viewers', row.get('viewer_count')))
            if same and value is not None:
                matches.append((snapshot.get('from', snapshot.get('at', '')), rank, value))
    matches.sort()
    if len(matches) < 2:
        return False
    previous, latest = matches[-2:]
    if latest[1] > 3 or previous[2] <= 0:
        return False
    try:
        elapsed = (timestamp(latest[0]) - timestamp(previous[0])).total_seconds() / 3600
    except (ValueError, TypeError):
        return False
    return elapsed > 0 and math.log(latest[2] / previous[2]) / elapsed > math.log(1.5)


def forecast(case):
    curves, anchors, calibrations = comparable_curves(case)
    # Equal log-space shrinkage toward the supplied raw median ratios.
    shape = [math.sqrt(raw * adjusted) for raw, adjusted in zip(RAW, ADJUSTED)]
    logs = list(map(math.log, shape))
    shape = [math.exp(max(sum(logs[max(0, i-1):i+2]) / len(logs[max(0, i-1):i+2]) - .35,
                          min(sum(logs[max(0, i-1):i+2]) / len(logs[max(0, i-1):i+2]) + .35, v)))
             for i, v in enumerate(logs)]
    sigma = spreads(curves, shape)
    # Jackknife influence is measured on the same full comparable evaluation set.
    if len(curves) >= 4:
        full_loss = comparable_loss(curves, shape, sigma)
        retained = []
        for index, curve in enumerate(curves):
            reduced = spreads(curves[:index] + curves[index+1:], shape)
            if abs(comparable_loss(curves, shape, reduced) - full_loss) <= .03:
                retained.append(curve)
        if len(retained) >= 2:
            sigma = spreads(retained, shape)
    known = case.get('known') or []
    row = next((row for row in known if row.get('hour') == 1), {})
    original = row.get('viewers')
    p1 = number(original)
    extra = 0.0
    invalid = original is not None and (p1 is None or p1 <= 0)
    if invalid:
        p1, sigma = 1000.0, [.8] * 23
    elif p1 is None or p1 < 50:
        p1 = max(1632.0, .5 * median(anchors)) if anchors else 1632.0
        extra = .15
    total = stream_sum(case.get('streams'))
    factor = 1.0
    if total is not None and calibrations:
        factor = math.sqrt(max(.7, min(1.5, total / (median(calibrations) * p1))))
    surge = category_surge(case)
    rows, previous = [], [0.0] * 5
    for index, ratio in enumerate(shape):
        spread = sigma[index] + extra + (.05 if surge and index + 2 >= 15 else 0)
        values = []
        for j, z in enumerate(Z):
            log_value = math.log(p1) + math.log(factor) + math.log(ratio) + spread * z
            value = math.exp(max(math.log(1e-9), min(math.log(1e9), log_value)))
            value = min(1e9, max(1e-9, value, previous[j], values[-1] if values else 0))
            values.append(value)
        rows.append({'hour': index + 2, 'q': values})
        previous = values
    return {'hours': rows}


def main():
    source = Path(os.environ.get('MAKEFUN_INPUT_DIR', '/input'))
    destination = Path(os.environ.get('MAKEFUN_OUTPUT_DIR', '/output'))
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob('*.in')):
        case = json.loads(path.read_text(encoding='utf-8'))
        output = forecast(case)
        (destination / (path.stem + '.out')).write_text(
            json.dumps(output, separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
