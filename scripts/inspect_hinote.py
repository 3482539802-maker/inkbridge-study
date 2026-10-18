"""Read a user-exported hinote ZIP/gzip JSON without screenshots or modifying it.

Observed format: Huawei Notes 15.0.14.348, extendFields hinote_1.0.6.
This is an experimental read-only inspector, not a stable vendor format API.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import zipfile

MAX_JSON_BYTES = 32 * 1024 * 1024


def inspect(path):
    path = Path(path).resolve()
    documents, pages, raw, entries, pdfs = [], [], {}, [], []
    warnings = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 20000:
            raise ValueError('Duplicate archive names or excessive entry count')
        for info in archive.infolist():
            entries.append({'name': info.filename, 'bytes': info.file_size,
                            'compressed_bytes': info.compress_size})
            if not info.is_dir() and info.filename.startswith('files/'):
                with archive.open(info) as source:
                    if source.read(5) == b'%PDF-':
                        pdfs.append({'entry': info.filename, 'bytes': info.file_size})
            if not info.filename.endswith('.jhinote'):
                continue
            if info.file_size > MAX_JSON_BYTES:
                raise ValueError('Oversized metadata entry')
            with archive.open(info) as source:
                with gzip.GzipFile(fileobj=source) as compressed:
                    data = compressed.read(MAX_JSON_BYTES + 1)
            if len(data) > MAX_JSON_BYTES:
                raise ValueError('Oversized uncompressed metadata')
            record = json.loads(data)
            raw[info.filename] = record
            if isinstance(record.get('customNoteContent'), dict):
                content = record['customNoteContent']
                documents.append({'note_id': content.get('id'), 'title': content.get('noteTitle'),
                                  'format_version': content.get('extendFields'),
                                  'attachment': content.get('attachment'), 'source_entry': info.filename})
            if isinstance(record.get('customNotePageContent'), dict):
                content = record['customNotePageContent']
                pages.append({'note_id': content.get('notesId'), 'page_id': content.get('id'),
                              'page_number': content.get('pageNumber'), 'page_ratio': content.get('pageRatio'),
                              'page_orientation': content.get('pageOrientation'),
                              'background': content.get('background'),
                              'background_attachment_id': content.get('bkgAttachmentId'),
                              'background_attachment_index': content.get('bkgAttachmentIndex'),
                              'elements': content.get('pageElement'), 'attachment': content.get('attachment'),
                              'source_entry': info.filename})
        if len(documents) != 1:
            warnings.append('Document count is not one; do not assume a unique note.')
        orders = [(p['note_id'], p['page_number']) for p in pages]
        if len(set(orders)) != len(orders):
            warnings.append('Duplicate page numbers exist; do not assume a unique page order.')
        if not all(type(p['page_number']) is int and p['page_number'] > 0 for p in pages):
            warnings.append('Some pages have invalid/missing pageNumber.')
        referenced = []
        for name, record in raw.items():
            for item in record.get('fileList', []):
                filename, expected = item.get('name'), item.get('hash')
                target = f'files/{filename}'
                if target not in names:
                    referenced.append({'source_entry': name, 'name': filename, 'verified': False, 'reason': 'missing'})
                    continue
                digest = hashlib.sha256()
                with archive.open(target) as source:
                    for chunk in iter(lambda: source.read(256 * 1024), b''):
                        digest.update(chunk)
                actual = digest.hexdigest()
                referenced.append({'source_entry': name, 'name': filename, 'sha256': actual,
                                   'verified': isinstance(expected, str) and actual.upper() == expected.upper()})
        if any(not r['verified'] for r in referenced):
            warnings.append('Some referenced attachment hashes were not verified.')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'type': 'hinote_inspection', 'source_path': str(path), 'sha256': digest.hexdigest(),
            'bytes': path.stat().st_size, 'documents': documents,
            'inspected_at_pc': datetime.now(timezone.utc).isoformat(),
            'pages': sorted(pages, key=lambda p: (str(p['note_id']), p['page_number'] if type(p['page_number']) is int else 0)),
            'archive_entries': entries, 'attachment_verification': referenced, 'embedded_pdfs': pdfs,
            'raw_metadata': raw, 'warnings': warnings,
            'limits': ['Exported snapshot is not a live private database connection.',
                       'Archive page count may reflect exported subset, not entire note.',
                       'pageElement and attachments are preserved; binary pen payload decoding is not implemented.',
                       'No native file generation, modification or import is performed.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('hinote', type=Path)
    parser.add_argument('--output', type=Path, default=Path('output/context/native-note.json'))
    parser.add_argument('--extract-pdf', type=Path, help='Copy the single embedded original PDF to this PC file, byte for byte')
    args = parser.parse_args()
    result = inspect(args.hinote)
    if args.extract_pdf:
        if len(result['embedded_pdfs']) != 1:
            parser.error('--extract-pdf requires exactly one embedded PDF')
        if args.extract_pdf.resolve() == args.hinote.resolve():
            parser.error('PDF destination must differ from hinote source')
        entry = result['embedded_pdfs'][0]
        if entry['bytes'] > 512 * 1024 * 1024:
            parser.error('Embedded PDF exceeds 512 MiB')
        args.extract_pdf.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        # Exclusive creation prevents replacing an existing original document.
        with zipfile.ZipFile(args.hinote) as archive, archive.open(entry['entry']) as source, args.extract_pdf.open('xb') as destination:
            for chunk in iter(lambda: source.read(256 * 1024), b''):
                destination.write(chunk)
                digest.update(chunk)
        result['extracted_pdf'] = {'path': str(args.extract_pdf.resolve()), 'sha256': digest.hexdigest(), **entry}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('raw_metadata', 'archive_entries')}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
