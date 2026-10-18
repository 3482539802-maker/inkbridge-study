"""Narrow read-only PENCILENGINE 52-byte-style inspector for independent audits.

Format reference: huwangpeng/Hinoter, commit
65ac2e26bf783b0b7095d179e055341d481399b0, docs/HINOTE_FORMAT.md.
This implementation rejects unsupported or ambiguous table/style layouts.
It does not import or execute upstream code and does not modify hinote files.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct
import zipfile

from inspect_hinote import inspect


def decode(data):
    if not data.startswith(b'PENCILENGINE') or not 116 <= len(data) <= 32*1024*1024:
        raise ValueError('Unsupported PENCILENGINE header or size')
    expected = struct.unpack_from('>I', data, 112)[0]
    if not 0 <= expected <= 5000:
        raise ValueError('Unsupported table count')
    layouts = {52: [], 60: []}
    for offset in range(0, len(data)-16, 4):
        prefix, count, stride, reserved = struct.unpack_from('>4I', data, offset)
        if prefix not in (0, 2) or not 2 <= count <= 16384 or stride != 36 or reserved or offset+16+count*36 > len(data):
            continue
        for size in layouts:
            style = offset-size
            if style < (16 if size == 52 else 0):
                continue
            pen = struct.unpack_from('>I', data, style+(0 if size == 52 else 12))[0]
            width = struct.unpack_from('>f', data, style+(28 if size == 52 else 40))[0]
            if pen not in (1, 2, 3, 5) or not math.isfinite(width) or not 0 < width <= 100:
                continue
            points = [list(struct.unpack_from('>2f', data, offset+20+i*36)) for i in range(count)]
            if any(not math.isfinite(v) or not -100000 < v < 100000 for p in points for v in p):
                continue
            layouts[size].append({'table_offset': offset, 'points_native': points, 'width_native': width, 'pen_type': pen,
                'degenerate_point_path':all(p == points[0] for p in points),
                'style_sha256':hashlib.sha256(data[style:offset]).hexdigest(),
                'layer_id':data[style-16:style].hex() if size==52 else None})
    if expected==0:
        # Only the observed empty 1.0.6 envelope, not an arbitrary zeroed count.
        if (len(data)==208 and struct.unpack_from('>I',data,12)[0]==76 and
            all(struct.unpack_from('>I',data,k)[0]==v for k,v in
                ((16,65537),(24,1),(76,48),(80,2),(84,65536),(88,84),(108,12),(116,12))) and
            not any(layouts.values())):
            return []
        raise ValueError('Unsupported or inconsistent empty native payload')
    supported = [size for size, tables in layouts.items() if len(tables) == expected]
    if supported != [52]:
        raise ValueError('No unique supported 52-byte-style layout: '+str({k: len(v) for k,v in layouts.items()}))
    strokes = layouts[52]
    if sum(len(s['points_native']) for s in strokes) > 500000:
        raise ValueError('Excessive native points')
    for a, b in zip(strokes, strokes[1:]):
        if a['table_offset']+16+36*len(a['points_native']) > b['table_offset']-68:
            raise ValueError('Overlapping native point tables')
    return strokes


def native_pen(hinote):
    native = inspect(hinote)
    if native['warnings'] or not all(a['verified'] for a in native['attachment_verification']):
        raise ValueError('Unverified native export')
    pages = []
    with zipfile.ZipFile(hinote) as archive:
        for page in native['pages']:
            strokes = []
            for a in page.get('attachment') or []:
                if a.get('isDelete', 0) or a.get('attachType') != 0:
                    continue
                name = 'files/'+Path(a['filePath']).name
                if archive.getinfo(name).file_size > 32*1024*1024:
                    raise ValueError('Oversized native pen payload')
                data = archive.read(name)
                if not data.startswith(b'PENCILENGINE'):
                    raise ValueError('Unsupported native ink encoding')
                for stroke in decode(data):
                    stroke['attachment_entry'] = name
                    strokes.append(stroke)
            ratio = page['page_ratio']
            if not isinstance(ratio, (int, float)) or not 0 < ratio <= 10:
                raise ValueError('Unsupported native page ratio')
            height = 1000*ratio if page['page_orientation'] == 1 else 1000/ratio
            for index, stroke in enumerate(strokes):
                stroke['index'] = index
                xs, ys = zip(*stroke['points_native'])
                stroke['bounds_native'] = [min(xs), min(ys), max(xs), max(ys)]
            raw = native['raw_metadata'][page['source_entry']]['customNotePageContent']
            pages.append({'page_uuid': page['page_id'], 'background_index': page['background_attachment_index'],
                          'native_size_candidate': [1000., height], 'thumbnail_entry': 'files/'+Path(raw.get('thumbnail', '')).name,
                          'strokes': strokes})
    return {'source_hinote_sha256': native['sha256'], 'pages': pages,
            'format': 'observed_hinote_1.0.6_52_byte_style', 'physical_accuracy_verified': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('hinote', type=Path)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    result = native_pen(args.hinote)
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps({'source_hinote_sha256': result['source_hinote_sha256'],
        'pages': [{'page_uuid': page['page_uuid'], 'background_index': page['background_index'],
                   'native_size_candidate': page['native_size_candidate'],
                   'strokes': [{k:v for k,v in s.items() if k != 'points_native'} for s in page['strokes']]} for page in result['pages']]}, indent=2))


if __name__ == '__main__':
    main()
