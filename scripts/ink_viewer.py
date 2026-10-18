"""Loopback-only live view of received raw points. No third-party dependencies."""
from collections import deque
from itertools import islice
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from ink_protocol import PEN_TOOLS
from ink_protocol import validate_session_id


class LiveState:
    def __init__(self, destination):
        self.lock = threading.Lock()
        self.events = deque(maxlen=50000)
        self.total = self.pen = self.errors = self.truncated = 0
        self.connections = self.gaps = self.reorders = 0
        self.connected = self.stopped = False
        self.synthetic = False
        self.sessions = {}
        self.replays = 0
        self.destination = str(destination.resolve())

    def append(self, event):
        with self.lock:
            self.total += 1
            self.pen += event["tool_type"] in PEN_TOOLS
            self.synthetic = self.synthetic or event.get("data_source") == "synthetic-demo"
            self.events.append({"cursor": self.total, "point": event})

    def snapshot(self, after, session_id=None, include_events=True):
        with self.lock:
            oldest = self.events[0]["cursor"] if self.events else self.total + 1
            restarted = after > self.total
            if restarted:
                after = 0
            page = list(islice((event for event in self.events if event["cursor"] > after),1000)) if include_events else []
            return {
                "points": self.total, "pen_points": self.pen, "invalid_records": self.errors,
                "truncated_records": self.truncated, "sequence_gaps": self.gaps,
                "sequence_reorders": self.reorders, "connected": self.connected,
                "stopped": self.stopped, "records_path": self.destination,
                "synthetic": self.synthetic,
                "sessions": [dict(item) for item in self.sessions.values()],
                "replayed_batches": self.replays,
                "reset": restarted or after < oldest - 1,
                "next_cursor": page[-1]["cursor"] if page else self.total,
                "events": [event for event in page if session_id is None or event["point"].get("session_id") == session_id],
            }


def start_viewer(port, state):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                body = (Path(__file__).resolve().parents[1] / "viewer/index.html").read_bytes()
                kind = "text/html; charset=utf-8"
            elif parsed.path in ("/events", "/status"):
                try:
                    query = parse_qs(parsed.query)
                    after = max(0, int(query.get("after", ["0"])[0]))
                    session_id = query.get("session_id", [None])[0]
                    if session_id is not None:
                        validate_session_id(session_id)
                except ValueError:
                    self.send_error(400)
                    return
                body = json.dumps(state.snapshot(after, session_id,include_events=parsed.path=="/events"),
                                  ensure_ascii=False, allow_nan=False).encode("utf-8")
                kind = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
