"""Compare an exported tablet session with its PC raw journal and final metadata."""
import argparse
import json
from pathlib import Path

from raw_journal_audit import audit,same_bytes


def verify(device_path, pc_path, device_metadata, pc_metadata):
    tablet = json.loads(device_metadata.read_text(encoding='utf-8'))
    computer = json.loads(pc_metadata.read_text(encoding='utf-8'))
    session_id = tablet['session_id']
    raw=audit(device_path,session_id)
    checks = {
        'raw_bytes_equal': same_bytes(device_path,pc_path),
        'complete_jsonl_boundary': raw['complete_jsonl_boundary'],
        'all_points_in_session': raw['all_points_in_session'],
        'session_ids_equal': computer['session_id'] == session_id,
        'metadata_equal': all(tablet.get(key) == computer.get(key) for key in (
            'state', 'started_at', 'ended_at', 'stop_reason', 'point_count',
            'dropped_count', 'byte_length', 'loss_accounting_complete')),
        'pc_confirmed_on_tablet': tablet.get('synced') is True,
        'final_state': tablet.get('state') in ('completed', 'interrupted'),
        'byte_counts_equal': tablet['byte_length'] == computer['byte_length'] == raw['byte_length'],
        'point_counts_equal': tablet['point_count'] == computer['point_count'] == raw['point_count'],
        'native_loss_accounting_complete': tablet.get('loss_accounting_complete') is True,
        'sequence_monotonic': raw['sequence_monotonic'],
        'native_sequence_accounted': raw['last_sequence'] == raw['point_count'] + tablet['dropped_count'],
        'zero_native_drops': tablet['dropped_count'] == 0,
    }
    return {
        'data_source': 'real-device', 'passed': all(checks.values()),
        'session_id': session_id, 'checks': checks,
        **{k:raw[k] for k in ('point_count','pen_point_count','pen_actions','pen_window_ids','sequence_gaps')},
        'dropped_count': tablet['dropped_count'],
        'duration_seconds': round((tablet['ended_at'] - tablet['started_at']) / 1000, 3),
        'byte_length': raw['byte_length'], 'sha256': raw['sha256'],
        'device_file': str(device_path.resolve()), 'pc_file': str(pc_path.resolve()),
        'stop_reason': tablet['stop_reason'],
        'note': 'Final byte equality proves this session only; it does not measure end-to-end latency or long-term stability.'
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('device', type=Path)
    parser.add_argument('pc', type=Path)
    parser.add_argument('--device-metadata', type=Path)
    parser.add_argument('--pc-metadata', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.device, args.pc,
                    args.device_metadata or args.device.with_suffix('.session.json'),
                    args.pc_metadata or args.pc.with_suffix('.session.json'))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
