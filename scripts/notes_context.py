"""Read Huawei Notes title/page controls over HDC; never captures screenshots."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = 'com.huawei.hmos.hinote'


def walk(node, parents=()):
    if not isinstance(node, dict):
        return
    yield node, parents
    for child in node.get('children', []):
        yield from walk(child, parents + (node,))


def rect(value):
    match = re.fullmatch(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', value or '')
    return list(map(int, match.groups())) if match else None


def visible(attributes):
    return attributes.get('visible') not in ('false', False)


def parse_layout(layout):
    entries = [(n, parents) for n, parents in walk(layout)
               if all(visible(p.get('attributes', {})) for p in parents)]
    tabs = []
    for node, _ in entries:
        attrs = node.get('attributes', {})
        if attrs.get('id') != 'FirstToolBarNoteTitleOuterLayer' or not visible(attrs):
            continue
        children = [child.get('attributes', {}) for child in node.get('children', [])]
        titles = [a.get('text') for a in children if a.get('id') == 'FirstToolBarNoteTitleText' and a.get('text')]
        if len(titles) != 1:
            continue
        # On tested 15.0.14.348, the active tab alone gains a leading SymbolGlyph.
        # This is an observed UI marker, not a public document-state API.
        tabs.append({'title': titles[0], 'bounds': rect(attrs.get('bounds')),
                     'active_marker': bool(children and children[0].get('type') == 'SymbolGlyph')})
    active = [tab for tab in tabs if tab['active_marker']]
    current = total = None
    counters = set()
    page_evidence = None
    for node, parents in entries:
        attrs = node.get('attributes', {})
        if attrs.get('id') != 'paginationInput' or not visible(attrs):
            continue
        candidate = int(attrs['text']) if attrs.get('text', '').isdigit() else None
        candidate_total = None
        if parents:
            siblings = [c.get('attributes', {}) for c in parents[-1].get('children', [])]
            text = [a.get('text', '') for a in siblings if visible(a)]
            if len(text) == 3 and text[1] == '/' and text[2].isdigit():
                candidate_total = int(text[2])
        if candidate is not None:
            counters.add((candidate, candidate_total))
    menu = {int(m.group(1)) for node, _ in entries
                if visible(node.get('attributes', {}))
                and (m := re.fullmatch(r'当前页\s*(\d+)', node.get('attributes', {}).get('text', '')))}
    if len(counters) == 1:
        current, total = next(iter(counters))
        page_evidence = 'paginationInput'
    elif not counters and len(menu) == 1:
        current = next(iter(menu))
        page_evidence = 'current_page_menu_text'
    conflicting = len(counters) > 1 or len(menu) > 1 or (current is not None and menu and menu != {current})
    pages = []
    for node, _ in entries:
        a = node.get('attributes', {})
        match = re.fullmatch(r'pageAllLayer_([A-Za-z0-9_-]+)', a.get('id', ''))
        if match and visible(a):
            pages.append({'page_id': match.group(1), 'visible_bounds': rect(a.get('bounds')),
                          'original_bounds': rect(a.get('origBounds'))})
    zoom = {node.get('attributes', {}).get('text') for node, _ in entries
            if visible(node.get('attributes', {})) and re.fullmatch(
                r'\d+(?:\.\d+)?%', node.get('attributes', {}).get('text', ''))}
    warnings = []
    if conflicting:
        warnings.append('Conflicting page controls; page reading rejected.')
        current = total = page_evidence = None
    if len(active) != 1:
        warnings.append('Active document marker is missing or ambiguous; do not select the first tab.')
    if current is None:
        warnings.append('Current page number is not exposed in this layout.')
    if len(pages) > 1:
        warnings.append('Multiple page canvases are visible; page UUID to PDF page mapping is not yet calibrated.')
    if current is not None and (current < 1 or (total is not None and (total < 1 or current > total))):
        warnings.append('Invalid page range; page reading rejected.')
        current = None
        page_evidence = None
    return {
        'type': 'notes_context', 'source': 'hdc-uitest-layout', 'bundle': BUNDLE,
        'captured_at_pc': None,
        'document_title': active[0]['title'] if len(active) == 1 else None,
        'title_evidence': 'tested_active_tab_leading_symbol' if len(active) == 1 else None,
        'current_page': current, 'total_pages': total, 'page_evidence': page_evidence,
        'open_tabs': tabs, 'visible_pages': pages,
        'zoom_percent': float(next(iter(zoom)).strip('%')) if len(zoom) == 1 else None,
        'warnings': warnings,
        'limits': ['Title is not a file hash or private file path.',
                   'Current page is the Notes UI page, not a printed book page label.',
                   'Canvas bounds may be clipped; screen-to-PDF coordinates are not calibrated.',
                   'stylusStage numeric suffixes are cache slots, not reliable page numbers.']
    }


def capture(hdc, target, output, reveal_page=False):
    prefix = [str(hdc)] + (['-t', target] if target else [])
    if not target:
        listing = subprocess.run([str(hdc), 'list', 'targets', '-v'], capture_output=True,
                                 text=True, encoding='utf-8', errors='replace', timeout=10, check=True).stdout
        targets = [cols[0] for line in listing.splitlines() if len(cols := line.split()) >= 3
                   and cols[1] in ('USB', 'TCP') and cols[2] == 'Connected']
        if len(targets) != 1:
            raise ValueError('Specify --target; expected one connected USB/TCP device')
        prefix = [str(hdc), '-t', targets[0]]
    output.parent.mkdir(parents=True, exist_ok=True)
    local = output.with_suffix('.layout.json')
    started = time.monotonic()
    capture_started = datetime.now(timezone.utc).isoformat()

    def run(command):
        result = subprocess.run(prefix + command, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=20)
        if result.returncode or '[Fail]' in result.stdout or '[Fail]' in result.stderr:
            raise RuntimeError((result.stdout + result.stderr).strip())
        return result

    def dump():
        destination = '/data/local/tmp/inkbridge-context-' + uuid.uuid4().hex + '.json'
        try:
            run(['shell', 'uitest', 'dumpLayout', '-b', BUNDLE, '-p', destination])
            run(['file', 'recv', destination, str(local.resolve())])
            return json.loads(local.read_bytes())
        finally:
            # Remove only our unique scratch file, including on transfer failure.
            subprocess.run(prefix + ['shell', 'rm', destination], capture_output=True, timeout=10)

    layout = dump()
    context = parse_layout(layout)
    context['captured_at_pc'] = datetime.now(timezone.utc).isoformat()
    context['page_reveal_performed'] = False
    if reveal_page and context['current_page'] is None and context['document_title']:
        buttons = [rect(n.get('attributes', {}).get('bounds')) for n, _ in walk(layout)
                   if n.get('attributes', {}).get('id') == 'FirstToolBarMoreOptionsButton'
                   and visible(n.get('attributes', {}))]
        if len(buttons) == 1 and buttons[0]:
            left, top, right, bottom = buttons[0]
            if right > left and bottom > top:
                original_title = context['document_title']
                run(['shell', 'uitest', 'uiInput', 'click', str((left + right) // 2), str((top + bottom) // 2)])
                try:
                    context = parse_layout(dump())
                    context['captured_at_pc'] = datetime.now(timezone.utc).isoformat()
                    context['page_reveal_performed'] = True
                    if context['document_title'] != original_title:
                        context['current_page'] = context['total_pages'] = context['page_evidence'] = None
                        context['warnings'].append('Document changed during page reveal; reading rejected.')
                finally:
                    run(['shell', 'uitest', 'uiInput', 'keyEvent', 'Back'])
                # Some firmware exposes the footer only after closing the menu.
                menu_local = output.with_suffix('.menu.layout.json')
                menu_local.write_bytes(local.read_bytes())
                closed = parse_layout(dump())
                closed['captured_at_pc'] = datetime.now(timezone.utc).isoformat()
                if (closed['document_title'] == original_title and closed['current_page'] is not None
                        and closed['current_page'] == context['current_page']):
                    context = closed
                    context['page_reveal_performed'] = True
                else:
                    # Keep the menu observation with its own time and matching evidence file.
                    context['layout_path'] = str(menu_local.resolve())
                    if closed['document_title'] != original_title:
                        context['current_page'] = context['total_pages'] = context['page_evidence'] = None
                        context['warnings'].append('Document changed after closing the page menu; reading rejected.')
    context['capture_duration_ms'] = round((time.monotonic() - started) * 1000)
    context['capture_started_at_pc'] = capture_started
    context.setdefault('layout_path', str(local.resolve()))
    return context


def apply_native_mapping(context, native):
    """Enrich live canvas UUIDs using an explicitly supplied exported snapshot.

    pageNumber may become stale after insertion/reordering. Keep this distinct
    from a live UI counter and never derive the entire note's page count.
    """
    title = context.get('document_title')
    documents = [d for d in native.get('documents', []) if d.get('title') == title]
    if not title or len(documents) != 1:
        context['warnings'].append('Native snapshot title binding missing or ambiguous.')
        return context
    document = documents[0]
    pages = [p for p in native.get('pages', []) if p.get('note_id') == document.get('note_id')]
    mapped = []
    for canvas in context.get('visible_pages', []):
        matches = [p for p in pages if p.get('page_id') == canvas['page_id']]
        if len(matches) == 1 and type(matches[0].get('page_number')) is int and matches[0]['page_number'] > 0:
            canvas['native_page_number'] = matches[0]['page_number']
            canvas['native_background_attachment_id'] = matches[0].get('background_attachment_id')
            canvas['native_background_attachment_index'] = matches[0].get('background_attachment_index')
            mapped.append(canvas)
    context['native_note_id'] = document.get('note_id') if mapped else None
    context['native_snapshot_sha256'] = native.get('sha256')
    context['warnings'].append('Native page order is an exported snapshot; refresh after inserting/deleting/reordering pages.')
    if context.get('current_page') is None and len(context.get('visible_pages', [])) == 1 and len(mapped) == 1:
        context['snapshot_page_candidate'] = mapped[0]['native_page_number']
        # Leave live current_page null. A saved page order cannot prove current order.
        context['snapshot_page_evidence'] = 'exact_canvas_uuid_in_exported_native_metadata'
    return context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--layout', type=Path, help='Parse an already acquired layout offline')
    parser.add_argument('--target')
    parser.add_argument('--native', type=Path, help='Enrich canvas UUIDs from inspect_hinote JSON; saved order remains a candidate')
    parser.add_argument('--hdc', type=Path, default=ROOT / 'tools/hdc/hdc.exe')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/context/current.json')
    parser.add_argument('--reveal-page', action='store_true', help='If hidden, briefly open/close More menu; use while not writing')
    parser.add_argument('--watch', action='store_true', help='Append passive snapshots to --history until Ctrl+C')
    parser.add_argument('--history', type=Path, default=ROOT / 'output/context/history.jsonl')
    parser.add_argument('--interval', type=float, default=5, help='Seconds between passive captures (minimum 2)')
    args = parser.parse_args()
    if args.watch and (args.layout or args.reveal_page):
        parser.error('--watch is passive; cannot combine with --layout or --reveal-page')
    if args.interval < 2:
        parser.error('--interval must be at least 2 seconds')
    try:
        while True:
            try:
                context = (parse_layout(json.loads(args.layout.read_bytes())) if args.layout
                           else capture(args.hdc, args.target, args.output, args.reveal_page))
                if args.layout:
                    context['source'] = 'saved-uitest-layout'
                    context['captured_at_pc'] = None  # Parsing time is not the original observation time.
                    context['layout_path'] = str(args.layout.resolve())
                if args.native:
                    context = apply_native_mapping(context, json.loads(args.native.read_bytes()))
            except Exception as error:
                context = {'type': 'notes_context', 'captured_at_pc': datetime.now(timezone.utc).isoformat(),
                           'document_title': None, 'current_page': None, 'total_pages': None, 'error': str(error)}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix('.tmp')
            temporary.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(args.output)
            if not args.watch:
                print(json.dumps(context, ensure_ascii=True, indent=2))
                if context.get('error'):
                    raise SystemExit(1)
                break
            args.history.parent.mkdir(parents=True, exist_ok=True)
            with args.history.open('a', encoding='utf-8') as history:
                history.write(json.dumps(context, ensure_ascii=False) + '\n')
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
