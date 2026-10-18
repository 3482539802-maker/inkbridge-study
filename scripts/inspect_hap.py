"""Check an unsigned capture HAP and save a report without accessing credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('hap', type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    with zipfile.ZipFile(args.hap) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f'Invalid ZIP CRC: {bad}')
        module = json.loads(archive.read('module.json'))
        native = archive.read('libs/arm64-v8a/libink_native.so')
        if native[:4] != b'\x7fELF' or native[4:6] != bytes([2, 1]):
            raise ValueError('Expected a little-endian 64-bit ELF library')
        if struct.unpack_from('<H', native, 18)[0] != 183:
            raise ValueError('Native library must be AArch64')
        permissions = module['module'].get('requestPermissions', [])
        if not any(p['name'] == 'ohos.permission.INPUT_MONITORING' for p in permissions):
            raise ValueError('Input-monitoring permission is missing from the manifest')
        for required in ('ets/modules.abc', 'resources.index'):
            if not archive.read(required):
                raise ValueError(f'Empty required entry: {required}')
        report = {
            'hap_path': str(args.hap.resolve()),
            'hap_bytes': args.hap.stat().st_size,
            'hap_sha256': hashlib.sha256(args.hap.read_bytes()).hexdigest(),
            'zip_crc_verified': True,
            'native_architecture': 'AArch64',
            'manifest': module,
            'entries': archive.namelist(),
            'device_tested': False,
            'permission_granted_verified': False,
            'note': 'Package structure inspection does not verify signing or runtime permission.',
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in ('manifest', 'entries')}, indent=2))


if __name__ == '__main__':
    main()
