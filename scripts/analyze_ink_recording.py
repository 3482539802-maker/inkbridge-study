"""Inspect real JSONL continuity, pressure and cadence; draw coordinates only."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics

from ink_protocol import PEN_TOOLS, validate_point
from render_ink import split_strokes


def analyze(path):
    points = [validate_point(json.loads(line)) for line in path.read_text(encoding='utf-8').splitlines()]
    pen = [point for point in points if point['tool_type'] in PEN_TOOLS]
    strokes = split_strokes(points)
    gaps = sum(max(0, b['seq'] - a['seq'] - 1) for a, b in zip(points, points[1:]))
    reorders = sum(b['seq'] <= a['seq'] for a, b in zip(points, points[1:]))
    intervals = [b['action_time'] - a['action_time'] for stroke in strokes
                 for a, b in zip(stroke, stroke[1:]) if b['action_time'] > a['action_time']]
    pressures = [point['pressure'] for point in pen]
    report = {
        'source': path.as_posix(), 'synthetic': any(p.get('data_source', '').startswith('synthetic') for p in points),
        'point_count': len(points), 'pen_point_count': len(pen),
        'pen_actions': dict(Counter(p['action'] for p in pen)),
        'stroke_count': len(strokes), 'stroke_lengths': [len(stroke) for stroke in strokes],
        'pen_window_ids': sorted(set(p['window_id'] for p in pen)),
        'sequence_gaps': gaps, 'sequence_reorders': reorders,
        'pen_pressure_min': min(pressures) if pressures else None,
        'pen_pressure_max': max(pressures) if pressures else None,
        'pen_pressure_distinct_values': len(set(pressures)),
        'median_point_interval_microseconds': statistics.median(intervals) if intervals else None,
        'active_stroke_duration_seconds': sum((s[-1]['action_time'] - s[0]['action_time']) / 1e6 for s in strokes),
        'note': 'Coordinates only. Page scrolling, zoom, erase and undo are not represented.'
    }
    destination = path.with_suffix('.analysis.json')
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if pen:
        from PIL import Image, ImageDraw
        left, top = min(p['x'] for p in pen), min(p['y'] for p in pen)
        width = max(p['x'] for p in pen) - left
        height = max(p['y'] for p in pen) - top
        scale = min(1.0, 1400 / max(width, 1), 900 / max(height, 1))
        image = Image.new('RGB', (int(width * scale) + 80, int(height * scale) + 80), 'white')
        draw = ImageDraw.Draw(image)
        for stroke in strokes:
            coordinates = [(int((p['x'] - left) * scale) + 40, int((p['y'] - top) * scale) + 40) for p in stroke]
            if len(coordinates) > 1:
                draw.line(coordinates, fill='#13334a', width=3, joint='curve')
            elif coordinates:
                x, y = coordinates[0]
                draw.ellipse((x-2, y-2, x+2, y+2), fill='#13334a')
        image.save(path.with_suffix('.png'))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('records', type=Path)
    print(json.dumps(analyze(parser.parse_args().records), ensure_ascii=False, indent=2))
