"""Reanalyze saved digital geometry with explicit native page links; no new frames."""
import argparse
import json
from pathlib import Path

from coordinate_alignment import align
from coordinate_layout_check import check, load_calibration
from coordinate_page_binding import bind_frame, load_binding
from multipage_geometry import REASON,rebind_pages


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--hinote', type=Path, required=True)
    p.add_argument('--pdf', type=Path, required=True)
    p.add_argument('--raw', type=Path, required=True)
    p.add_argument('--layout-calibration', type=Path)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    binding = load_binding(args.hinote, args.pdf)
    model = load_calibration(args.layout_calibration) if args.layout_calibration else None
    if (args.run/'geometry.jsonl').stat().st_size > 12*1024*1024:
        p.error('Geometry exceeds bounded size')
    frames = []
    for line in (args.run/'geometry.jsonl').read_text(encoding='utf-8').splitlines():
        if len(frames) >= 3600:
            p.error('At most 3600 frames')
        original=json.loads(line)
        if original.get('registration',{}).get('reason')==REASON:
            quality_check=(lambda frame:check(frame,model)) if model is not None else None
            frame=rebind_pages(original,binding,quality_check)
        else:frame=bind_frame(original,binding)
        r = frame['registration']
        if r.get('valid') and model is not None:
            quality = check(frame, model)
            r['layout_consistency'] = quality
            if quality['status'] == 'contradiction':
                r.update(valid=False, reason=quality['reason'], visual_fit_valid_before_layout_check=True)
        frames.append(frame)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output/'geometry.jsonl').write_text(''.join(json.dumps(f)+'\n' for f in frames), encoding='utf-8')
    (args.output/'page-binding.json').write_text(json.dumps(binding, indent=2), encoding='utf-8')
    from capture_clock_history import load_clock
    state = load_clock(args.run/'clock.json')
    (args.output/'clock.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    summary = align(args.raw, state, frames, args.output/'points.jsonl')
    summary['derivation'] = 'saved_geometry_with_exported_native_background_links'
    summary['new_screenshots'] = 0
    (args.output/'alignment-summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
