"""Read Notes' local diagnostic trees, without launching or controlling any UI.

Diagnostic snapshots are observations, not an atomic document API. They contain
geometry and recent gesture decisions, not the native handwriting point stream.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = 'com.huawei.hmos.hinote'
NUMBER = r'-?\d+(?:\.\d+)?'
RECT = re.compile(r'\[\s*(' + NUMBER + r'),\s*(' + NUMBER + r')\s*\],'
                  r'\[\s*(' + NUMBER + r'),\s*(' + NUMBER + r')\s*\]')


def extract_tree(text):
    """Require both the Notes header and a known tree schema, not just any JSON."""
    if f'bundleName:{BUNDLE}' not in text:
        raise ValueError('Missing Notes bundle identity in dump')
    for match in re.finditer(r'\{', text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get('$type') == 'root' and '$children' in value:
            return value
    raise ValueError('No supported diagnostic root; permission/timeout/unsupported output')


def bounds(value):
    match = RECT.fullmatch(value or '')
    if not match:
        return None
    result = list(map(float, match.groups()))
    if not all(map(math.isfinite, result)) or result[2] <= result[0] or result[3] <= result[1]:
        return None
    return result


def visible(node):
    return node.get('visible') not in (False, 'false', 0) and node.get('active') not in (False, 'false', 0)


def nodes(tree, ancestors=()):
    if not isinstance(tree, dict) or not visible(tree):
        return
    yield tree, ancestors
    # Never walk $CachedItems, recycled roots, or arbitrary attribute dictionaries.
    for child in tree.get('$children', []):
        if isinstance(child, dict) and child.get('$type') != '__Recycle__':
            yield from nodes(child, ancestors + (tree,))


def summarize_tree(tree):
    rows = list(nodes(tree))
    tabs, pages, surfaces, canvases = [], [], [], []
    for node, parents in rows:
        identity = node.get('compid', '')
        attrs = node.get('$attrs', {})
        if identity == 'FirstToolBarNoteTitleOuterLayer':
            def tab_children(parent):
                for child in parent.get('$children', []):
                    if not visible(child):
                        continue
                    if child.get('$type') in ('IfElse', 'SyntaxItem'):
                        yield from tab_children(child)
                    else:
                        yield child
            children = list(tab_children(node))
            titles = [c.get('$attrs', {}).get('content') for c in children
                      if c.get('compid') == 'FirstToolBarNoteTitleText']
            if len(titles) == 1 and titles[0]:
                tabs.append({'title': titles[0], 'active_marker': bool(children and children[0].get('$type') == 'SymbolGlyph')})
        page = re.fullmatch(r'pageAllLayer_([a-fA-F0-9]{32})', identity)
        if page:
            pages.append({'page_id': page[1], 'node_id': node.get('$ID'),
                          'unclipped_window_bounds': bounds(node.get('$rect'))})
        if node.get('$type') == 'XComponent':
            enclosing = [p['compid'][13:] for p in parents if p.get('compid', '').startswith('pageAllLayer_')]
            declared = re.fullmatch(r'(?:allStylus|hightLightStylus)([a-fA-F0-9]{32})_.*', identity)
            surfaces.append({'node_id': node.get('$ID'), 'component_id': identity,
                'surface_id': attrs.get('$SurfaceId'), 'window_bounds': bounds(node.get('$rect')),
                'ancestor_page_ids': enclosing, 'component_uuid_candidate': declared[1] if declared else None,
                'uuid_conflicts_with_ancestor': bool(declared and enclosing and declared[1] != enclosing[-1]),
                'verified_page_binding': False})
        if node.get('$type') == 'Canvas':
            canvases.append({'node_id': node.get('$ID'), 'window_bounds': bounds(node.get('$rect')),
                             'paint_diagnostics': attrs.get('CanvasPaint'), 'contains_ink_points': False})
    active = [t for t in tabs if t['active_marker']]
    return {'document_title_candidate': active[0]['title'] if len(active) == 1 else None,
            'active_tab_marker_unique': len(active) == 1, 'page_layers': pages,
            'surfaces': surfaces, 'canvases': canvases, 'native_ink_points_available': False,
            'screen_to_document_calibrated': False,
            'limits': ['Component visibility does not prove foreground state or a fresh document.',
                       'Diagnostic rectangles are relative to the window; display conversion needs its transform.',
                       'XComponent UUID semantics are unknown and may differ from enclosing page UUIDs.',
                       'CanvasPaint Scale/Trans are not proven Notes document transforms.',
                       'Tree rectangles are bounding boxes; do not invert them as general page transforms.']}


def summarize_events(text):
    if f'bundleName:{BUNDLE}' not in text:
        raise ValueError('Missing Notes identity in event dump')
    histories = []
    for part in re.split(r'^\d+: event tree =>\s*$', text, flags=re.M)[1:]:
        points = [{'pointer_id': int(m[1]), 'action': m[2], 'device_wall_time_text': m[3],
                   'is_injected': bool(int(m[4]))}
                  for m in re.finditer(r'^\s+id: (\d+), type: (\w+), timestamp: ([\d :.\-]+), isInjected: ([01])', part, re.M)]
        decisions = []
        current = None
        for line in part.splitlines():
            match = re.search(r'frameNodeId: (\d+), type: (\w+)', line)
            if match:
                current = {'frame_node_id': int(match[1]), 'recognizer': match[2], 'accepted': False}
                decisions.append(current)
            elif current is not None and 'state: SUCCEED, disposal: ACCEPT' in line:
                current['accepted'] = True
        histories.append({'boundary_events': points, 'recognizers': decisions})
    return {'recent_event_trees': histories, 'complete_input_stream': False,
            'clock_aligned_to_raw': False, 'contains_pen_trajectory': False,
            'limit': 'Bounded diagnostic history can omit MOVE samples and whole gestures; recognizer acceptance is not a zoom factor.'}


def notes_window(text):
    matches = re.findall(r'^hinote0\s+(\d+)\s+(\d+)\s+(\d+)\s+', text, re.M)
    if len(matches) != 1:
        raise ValueError('Expected exactly one existing hinote0 window; no application will be launched')
    display, pid, window = map(int, matches[0])
    focus = re.search(r'^Focus window:\s*(\d+)', text, re.M)
    return {'display_id': display, 'pid': pid, 'window_id': window,
            'focused': int(focus[1]) == window if focus else None}


def collect(hdc, target, destination, include_events=False):
    destination.mkdir(parents=True, exist_ok=False)
    prefix = [str(hdc)] + (['-t', target] if target else [])
    if not target:
        listing = subprocess.run(prefix + ['list', 'targets', '-v'], capture_output=True,
                                 encoding='utf-8', timeout=10, check=True).stdout
        targets = [c[0] for line in listing.splitlines() if len(c := line.split()) >= 3
                   and c[1] in ('USB', 'TCP') and c[2] == 'Connected']
        if len(targets) != 1:
            raise ValueError('Expected one connected device; use --target')
        prefix += ['-t', targets[0]]

    def run(name, command):
        start = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        proc = subprocess.run(prefix + ['shell', command], capture_output=True, encoding='utf-8',
                              errors='replace', timeout=20)
        text = proc.stdout + proc.stderr
        (destination / name).write_text(text, encoding='utf-8')
        if proc.returncode or '[Fail]' in text:
            raise RuntimeError(f'HDC diagnostic failed; see {name}')
        return text, {'start_at_pc': start, 'end_at_pc': datetime.now(timezone.utc).isoformat(),
                      'elapsed_ms': round((time.monotonic()-t0)*1000, 3)}

    before_text, _ = run('windows-before.txt', 'hidumper -s WindowManagerService -a -a')
    before = notes_window(before_text)
    # All options here are static read-only commands. Never accept arbitrary dump options.
    base = f'hidumper -s WindowManagerService -a "-w {before["window_id"]} '
    raw, observed = run('allInfo.txt', base + '-allInfo"')
    tree = extract_tree(raw)
    (destination/'tree.json').write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = summarize_tree(tree)
    if include_events:
        events, interval = run('events.txt', base + '-event"')
        summary['recent_gestures'] = summarize_events(events)
        summary['events_observation_interval'] = interval
    after_text, _ = run('windows-after.txt', 'hidumper -s WindowManagerService -a -a')
    after = notes_window(after_text)
    identity_stable = all(before[k] == after[k] for k in ('pid', 'window_id', 'display_id'))
    header_pid = re.search(r'^Pid:\s*(\d+)', raw, re.M)
    identity_stable = identity_stable and header_pid is not None and int(header_pid[1]) == before['pid']
    focused = before['focused'] is True and after['focused'] is True
    summary.update(source='local_hidumper_allInfo', bundle=BUNDLE, observation_interval=observed,
                   window_before=before, window_after=after, window_identity_stable=identity_stable,
                   foreground_at_both_checks=focused, atomic_snapshot=False,
                   current_document_valid=identity_stable and focused and summary['active_tab_marker_unique'],
                   tablet_ui_actions=[], cloud_operations=[])
    (destination/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New experiment directory; never overwrite')
    parser.add_argument('--target')
    parser.add_argument('--hdc', type=Path, default=ROOT/'tools/hdc/hdc.exe')
    parser.add_argument('--events', action='store_true', help='Also read bounded recent gesture diagnostics')
    parser.add_argument('--count', type=int, default=1, help='Bounded number of sequential observations (1..60)')
    parser.add_argument('--interval', type=float, default=2, help='Idle interval between observations, at least 1 second')
    args = parser.parse_args()
    if not 1 <= args.count <= 60 or not math.isfinite(args.interval) or args.interval < 1:
        parser.error('count 1..60, finite interval >= 1 required')
    args.output.mkdir(parents=True, exist_ok=False)
    for index in range(args.count):
        summary = collect(args.hdc, args.target, args.output/f'{index:03d}', args.events)
        print(json.dumps({'sample': index, 'document_context_valid': summary['current_document_valid'],
                          'page_layers': len(summary['page_layers']), 'surfaces': len(summary['surfaces']),
                          'observation_ms': summary['observation_interval']['elapsed_ms'],
                          'native_ink_points_available': False}, ensure_ascii=False))
        if index+1 < args.count:
            time.sleep(args.interval)


if __name__ == '__main__':
    main()
