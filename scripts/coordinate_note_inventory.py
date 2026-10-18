"""Inventory exported hinote page/background types without modifying originals."""
import argparse
import json
from pathlib import Path
from inspect_hinote import inspect


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        p.error('choose a new output path')
    rows = []
    for path in sorted(args.directory.glob('*.hinote')):
        data = inspect(path)
        rows.append({'file': str(path.resolve()), 'sha256': data['sha256'],
                     'document_count': len(data['documents']), 'pages': len(data['pages']),
                     'embedded_pdf_count': len(data['embedded_pdfs']),
                     'attachments_verified': all(x['verified'] for x in data['attachment_verification']),
                     'page_backgrounds': [{'page_uuid': page['page_id'], 'page_ratio': page['page_ratio'],
                                          'background': page['background'],
                                          'has_background_attachment': bool(page['background_attachment_id']),
                                          'background_index': page['background_attachment_index']}
                                         for page in data['pages']],
                     'live_export_subscription': False})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(rows, out, ensure_ascii=False, indent=2)
    print(json.dumps([{k:v for k,v in row.items() if k != 'page_backgrounds'} for row in rows],ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
