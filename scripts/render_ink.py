"""Render received public Input Kit points to SVG; never uses screen pixels."""
from __future__ import annotations

import argparse
from collections import Counter
import html
import json
from pathlib import Path
from ink_protocol import PEN_TOOLS, validate_point

def read_points(path):
    points = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            point = validate_point(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
        points.append(point)
    return points

def split_strokes(points):
    # Official public enum: cancel=0, down=1, move=2, up=3; finger tool=0.
    current = {}
    strokes = []
    previous = None
    previous_session = None
    for point in points:
        if previous is not None and (point["seq"] != previous + 1 or point.get("session_id") != previous_session):
            strokes.extend(current.values())
            current.clear()
        previous = point["seq"]
        previous_session = point.get("session_id")
        if point.get("tool_type") not in PEN_TOOLS:
            continue
        key = (point.get("display_id", 0), point.get("pointer_id", 0))
        if point["action"] == 1:
            if current.get(key):
                strokes.append(current.pop(key))
            current[key] = [point]
        elif point["action"] == 2:
            current.setdefault(key, []).append(point)
        elif point["action"] in (0, 3):
            if key in current:
                if point["action"] == 3:
                    current[key].append(point)
                strokes.append(current.pop(key))
    strokes.extend(current.values())
    return strokes

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    points = read_points(args.records)
    strokes = split_strokes(points)
    if not strokes:
        raise SystemExit("No pen strokes available; no reconstruction was generated")
    all_points = [point for stroke in strokes for point in stroke]
    xs = [p["x"] for p in all_points]
    ys = [p["y"] for p in all_points]
    left, top = min(xs)-30, min(ys)-30
    width, height = max(xs)-left+30, max(ys)-top+30
    shapes = []
    for stroke in strokes:
        if len(stroke) == 1:
            p = stroke[0]
            shapes.append(f'<circle cx="{p["x"]}" cy="{p["y"]}" r="2" fill="#13334a"/>')
        else:
            coordinates = " ".join(f'{p["x"]},{p["y"]}' for p in stroke)
            shapes.append(f'<polyline points="{coordinates}"/>')
    metadata = {"point_count": len(points), "pen_point_count": len(all_points), "stroke_count": len(strokes),
                "actions": dict(Counter(p["action"] for p in points)),
                "tools": dict(Counter(p.get("tool_type", -1) for p in points)),
                "source": str(args.records.resolve()),
                "synthetic": any(p.get("data_source") == "synthetic-demo" for p in points),
                "note": "Geometry reconstructed from captured input points; document erasing/undo/zoom is not tracked."}
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="750" viewBox="{left} {top} {max(width,1)} {max(height,1)}">
<title>Captured pen input</title><desc>{html.escape(json.dumps(metadata))}</desc>
<rect x="{left}" y="{top}" width="{max(width,1)}" height="{max(height,1)}" fill="white"/>
<g fill="none" stroke="#13334a" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">{"".join(shapes)}</g>
</svg>'''
    destination = args.output or args.records.with_suffix(".svg")
    destination.write_text(svg, encoding="utf-8")
    destination.with_suffix(".geometry.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata))
    print(f"Saved {destination.resolve()}")

if __name__ == "__main__":
    main()
