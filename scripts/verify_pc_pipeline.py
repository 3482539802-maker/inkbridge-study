"""Meaningful offline transport/reconstruction verification using synthetic records."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

from ink_protocol import validate_point
from render_ink import split_strokes
from simulate_ink import demo_points

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def main():
    port, viewer = free_port(), free_port()
    while port == viewer:
        viewer = free_port()
    with tempfile.TemporaryDirectory(prefix="ink-transport-") as folder:
        records = Path(folder) / "synthetic.jsonl"
        process = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT/"scripts/ink_receiver.py"),
                                    "--seconds", "5", "--port", str(port), "--viewer-port", str(viewer),
                                    "--output", str(records)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        try:
            deadline = time.monotonic()+4
            while True:
                try:
                    with urlopen(f"http://127.0.0.1:{viewer}/events", timeout=0.5) as response:
                        json.load(response)
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError("Receiver did not start")
                    time.sleep(0.025)
            expected = list(demo_points())
            with socket.create_connection(("127.0.0.1", port)) as connection:
                payload = b"".join((json.dumps(p)+"\n").encode() for p in expected)
                for offset in range(0,len(payload),37):
                    connection.sendall(payload[offset:offset+37])
                invalid = dict(expected[0], seq=True)
                connection.sendall((json.dumps(invalid)+"\n").encode())
                invalid = dict(expected[0], x=float("nan"))
                connection.sendall((json.dumps(invalid)+"\n").encode())
                # A 2-point hole must be counted without joining the geometry across it.
                gap = dict(expected[-1], seq=expected[-1]["seq"]+3, action=2)
                connection.sendall((json.dumps(gap)+"\n").encode())
                connection.sendall(b'{"type":"point"')
            deadline = time.monotonic()+2
            while True:
                with urlopen(f"http://127.0.0.1:{viewer}/events?after=0") as response:
                    live = json.load(response)
                if live["points"]==len(expected)+1 and not live["connected"]:
                    break
                if time.monotonic()>deadline:
                    raise AssertionError("Live viewer did not receive all points")
                time.sleep(0.025)
            assert live["invalid_records"]==2, live
            assert live["sequence_gaps"]==2, live
            assert live["truncated_records"]==1, live
            assert live["synthetic"] is True
            assert [e["point"]["seq"] for e in live["events"]] == [p["seq"] for p in expected]+[gap["seq"]]
            with urlopen(f"http://127.0.0.1:{viewer}/") as response:
                assert '笔迹接收' in response.read().decode()
            # Reconnect and start a new capture; the service must keep accepting connections.
            with socket.create_connection(("127.0.0.1", port)) as connection:
                connection.sendall((json.dumps(expected[0])+"\n").encode())
            stdout, stderr = process.communicate(timeout=8)
            assert process.returncode == 0, stderr
            saved = [json.loads(line) for line in records.read_text(encoding="utf-8").splitlines()]
            assert len(saved)==len(expected)+2
            assert all(validate_point(point) for point in saved)
            summary=json.loads(records.with_suffix('.summary.json').read_text())
            assert summary['connections']==2, summary
            assert summary['point_count']==len(saved), summary
            assert len(split_strokes(expected))==5
            sample = [dict(expected[0],seq=1,action=1),dict(expected[1],seq=3,action=2),
                      dict(expected[2],seq=4,action=3)]
            assert len(split_strokes(sample))==2, 'Renderer joined a missing sequence'
            out=ROOT/'output/tests';out.mkdir(parents=True,exist_ok=True)
            (out/'pc-pipeline-report.json').write_text(json.dumps({
                'data_source':'synthetic-test','passed':True,'received_points':len(saved),
                'checks':['fragmented TCP JSONL','invalid boolean and NaN rejection','sequence gap detection',
                          'truncated EOF detection','live HTTP events','reconnection','5-stroke geometry',
                          'no line across lost points'],
                'tablet_verified':False},ensure_ascii=False,indent=2),encoding='utf-8')
            print(f'PASS: {len(saved)} synthetic points, fragmented TCP, live HTTP, reconnect and geometry.')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
    from verify_session_replay import verify
    verify()


if __name__=='__main__':
    main()
