"""Loopback JSONL receiver with durable sessions and byte-offset replay over HDC."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import socket
import time

from ink_protocol import validate_point
from ink_viewer import LiveState, start_viewer
from ink_sessions import SessionStore, MetadataPublishError

ROOT = Path(__file__).resolve().parents[1]
MAX_LINE = 128 * 1024


def receive_connection(connection, output, state, deadline, sessions=None):
    pending = bytearray()
    previous = None
    active_session = None
    session_protocol = False
    last_data = time.monotonic()
    connection.settimeout(0.25)
    with state.lock:
        state.connected = True
        state.connections += 1
    try:
        while time.monotonic() < deadline:
            try:
                data = connection.recv(65536)
            except socket.timeout:
                # A dead peer must not monopolize the receiver forever.
                if time.monotonic() - last_data > 15:
                    break
                continue
            except ConnectionError:
                break
            if not data:
                break
            last_data = time.monotonic()
            pending.extend(data)
            while b"\n" in pending:
                line, _, pending = pending.partition(b"\n")
                try:
                    if len(line) > MAX_LINE:
                        raise ValueError("Oversized record")
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("Expected an object")
                    kind = message.get("type")
                    if sessions and kind in ("session", "batch", "session_end", "ping"):
                        session_protocol = True
                        if kind == "session":
                            reply = sessions.hello(message)
                            active_session = reply["session_id"]
                        elif active_session is None:
                            raise ValueError("Session handshake required")
                        elif kind == "batch":
                            reply = sessions.batch(active_session, message)
                        elif kind == "session_end":
                            reply = sessions.finish(active_session, message)
                        else:
                            reply = sessions.ack(active_session)
                        connection.sendall((json.dumps(reply) + "\n").encode())
                        continue
                    if active_session is not None:
                        raise ValueError("Raw records cannot follow a v2 handshake")
                    event = validate_point(message)
                except MetadataPublishError:
                    # Drop only this connection without ACK. The tablet keeps
                    # its journal and retries; unrelated HTTP/ingest stay alive.
                    with state.lock:
                        state.errors += 1
                    return
                except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                    with state.lock:
                        state.errors += 1
                    if session_protocol:
                        connection.sendall(b'{"type":"error","message":"Invalid session frame"}\n')
                        return
                    continue
                seq = event["seq"]
                with state.lock:
                    if previous is not None:
                        if seq > previous + 1:
                            state.gaps += seq - previous - 1
                        elif seq <= previous and seq != 1:
                            # seq=1 starts a new capture on an existing TCP connection.
                            state.reorders += 1
                    previous = seq
                event["received_pc"] = datetime.now().astimezone().isoformat()
                output.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                state.append(event)
            output.flush()
            if len(pending) > MAX_LINE:
                with state.lock:
                    state.errors += 1
                pending.clear()
                break
    finally:
        with state.lock:
            state.connected = False
            if pending:
                state.truncated += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--viewer-port", type=int, default=18766, help="0 disables the browser viewer")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sessions-dir", type=Path, default=ROOT / "output/ink/sessions",
                        help="Persistent raw journals, reused after receiver restart")
    args = parser.parse_args()
    if (not 1 <= args.port <= 65535 or not 0 <= args.viewer_port <= 65535
            or args.port == args.viewer_port or not 0 <= args.seconds <= 604800):
        parser.error("Invalid ports or duration")
    destination = args.output or ROOT / "output/ink" / (
        "app-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jsonl")
    destination.parent.mkdir(parents=True, exist_ok=True)
    state = LiveState(destination)
    sessions = SessionStore(args.sessions_dir, state)
    started = time.monotonic()
    viewer = None
    created = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            server.bind(("127.0.0.1", args.port))
            server.listen(2)
            server.settimeout(0.25)
            if args.viewer_port:
                viewer = start_viewer(args.viewer_port, state)
            with destination.open("x", encoding="utf-8") as output:
                created = True
                print(f"Listening on 127.0.0.1:{args.port}; saving to {destination.resolve()}", flush=True)
                if viewer:
                    print(f"Live viewer: http://127.0.0.1:{args.viewer_port}", flush=True)
                deadline = started + args.seconds if args.seconds else float("inf")
                while time.monotonic() < deadline:
                    try:
                        connection, _ = server.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        try:
                            receive_connection(connection, output, state, deadline, sessions)
                        except (ConnectionError, socket.timeout):
                            pass  # An ACK can be lost; the next hello resumes from disk.
    except KeyboardInterrupt:
        print("Stopping receiver", flush=True)
    finally:
        with state.lock:
            state.stopped = True
        if viewer:
            viewer.shutdown()
            viewer.server_close()
        if created:
            snapshot = state.snapshot(state.total)
            summary = {"point_count": snapshot["points"], "pen_point_count": snapshot["pen_points"],
                       "invalid_records": snapshot["invalid_records"], "truncated_records": snapshot["truncated_records"],
                       "sequence_gaps": snapshot["sequence_gaps"], "sequence_reorders": snapshot["sequence_reorders"],
                       "connections": state.connections, "duration_seconds": round(time.monotonic()-started, 3),
                       "sessions": snapshot["sessions"], "replayed_batches": snapshot["replayed_batches"],
                       "records_path": str(destination.resolve())}
            destination.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
