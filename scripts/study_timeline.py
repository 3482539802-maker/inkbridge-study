"""Local study timeline from existing raw pen journals; never reads Notes private data.

Uses device event time, not packet arrival time, so offline backfill does not
change measured durations. A pause creates a candidate group, not a semantic
solution step. Annotations are explicit user reports, stored separately.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from ink_protocol import PEN_TOOLS, validate_point

ROOT = Path(__file__).resolve().parents[1]
ANNOTATION_FIELDS = {"subject", "material", "problem", "step", "concept", "reflection", "pause_reason"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, allow_nan=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def union_duration(intervals):
    total = 0
    end = None
    for start, stop in sorted(intervals):
        total += max(0, stop - max(start, end if end is not None else start))
        end = max(stop, end if end is not None else stop)
    return total


def assistant_context(report):
    """Compact evidence for a future chat/API, without thousands of raw points."""
    fields = ("id", "session_id", "window_id", "first_seq", "capture_state",
              "start_us", "end_us", "elapsed_seconds", "observed_writing_seconds",
              "other_seconds", "gap_before_seconds", "stroke_count", "eraser_stroke_count",
              "issues", "annotation")
    compact = [{key: c[key] for key in fields} for c in report["candidates"]]
    groups = {}
    for candidate in compact:
        annotation = candidate["annotation"]
        if not any(annotation.get(k) for k in ("subject", "problem", "step", "concept")):
            continue
        key = tuple(annotation.get(k, "") for k in ("subject", "material", "problem", "step", "concept"))
        group = groups.setdefault(key, dict(zip(("subject", "material", "problem", "step", "concept"), key),
                                            candidate_ids=[], observed_writing_seconds=0,
                                            observed_fragment_span_seconds=0, reflections=[]))
        group["candidate_ids"].append(candidate["id"])
        group["observed_writing_seconds"] = round(group["observed_writing_seconds"] + candidate["observed_writing_seconds"], 6)
        group["observed_fragment_span_seconds"] = round(group["observed_fragment_span_seconds"] + candidate["elapsed_seconds"], 6)
        if annotation.get("reflection"):
            group["reflections"].append(annotation["reflection"])
    return {"schema": "inkbridge-study-context-v1", "generated_at_pc": report["generated_at_pc"],
            "timing_basis": report["timing_basis"], "limits": report["limits"],
            "recognition_available": False, "automatic_model_calls": False,
            "total_problem_solving_time_known": False,
            "summary": report["summary"], "sources": report["sources"],
            "candidates": compact, "confirmed_label_groups": list(groups.values())}


def analyze_records(path, gap_seconds=8, window_id=None):
    """Read only newline-terminated records; a growing tail is retried next scan."""
    source_key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
    streams = {}
    strokes = []
    invalid = tail = 0
    records = selected = 0
    synthetic = False
    windows = set()
    discontinuities = 0

    def flush(stream, reason):
        for stroke in stream["active"].values():
            stroke["issues"].add(reason)
            strokes.append(stroke)
        stream["active"].clear()

    with path.open("rb") as source:
        for line in source:
            if not line.endswith(b"\n"):
                tail += 1
                break
            try:
                point = validate_point(json.loads(line))
            except (ValueError, UnicodeDecodeError):
                invalid += 1
                for stream in streams.values():
                    flush(stream, "invalid_record")
                    stream["epoch"] += 1
                continue
            records += 1
            synthetic |= point.get("data_source") == "synthetic-demo"
            session = point.get("session_id", "legacy")
            stream = streams.setdefault(session, {"previous": None, "time": None,
                                                   "active": {}, "epoch": 0})
            previous = stream["previous"]
            if previous is not None and (point["seq"] != previous + 1 or point["action_time"] < stream["time"]):
                flush(stream, "sequence_or_clock_discontinuity")
                stream["epoch"] += 1
                discontinuities += 1
            stream["previous"], stream["time"] = point["seq"], point["action_time"]
            if point["tool_type"] not in PEN_TOOLS | {2}:
                continue
            windows.add(point["window_id"])
            if window_id is not None and point["window_id"] != window_id:
                flush(stream, "window_filter_boundary")
                stream["epoch"] += 1
                continue
            selected += 1
            key = (point["display_id"], point["pointer_id"])
            current = stream["active"].get(key)
            if current and (point["window_id"] != current["window_id"] or point["tool_type"] != current["tool_type"]):
                current["issues"].add("target_or_tool_changed")
                strokes.append(stream["active"].pop(key))
                current = None
            if point["action"] == 1 and current:
                current["issues"].add("missing_up")
                strokes.append(stream["active"].pop(key))
                current = None
            if current is None:
                current = {"session_id": session, "epoch": stream["epoch"],
                           "window_id": point["window_id"], "display_id": point["display_id"],
                           "tool_type": point["tool_type"], "first_seq": point["seq"],
                           "start_us": point["action_time"], "end_us": point["action_time"],
                           "points": [], "issues": set(), "closed": False}
                if point["action"] != 1:
                    current["issues"].add("missing_down")
                stream["active"][key] = current
            current["points"].append([point["x"], point["y"], point["pressure"], point["action_time"]])
            current["end_us"] = point["action_time"]
            if point["action"] in (0, 3):
                current["closed"] = True
                if point["action"] == 0:
                    current["issues"].add("cancelled")
                strokes.append(stream["active"].pop(key))
    for stream in streams.values():
        flush(stream, "open_stroke")

    # Session clocks cannot be compared across captures or a device reboot.
    grouped = {}
    for stroke in strokes:
        grouped.setdefault((stroke["session_id"], stroke["epoch"]), []).append(stroke)
    candidates = []
    for group in grouped.values():
        group.sort(key=lambda s: (s["start_us"], s["first_seq"]))
        current = None
        previous_end = None
        previous_target = None
        for stroke in group:
            target = (stroke["window_id"], stroke["display_id"])
            same_target = previous_target == target
            gap = max(0, stroke["start_us"] - previous_end) if previous_end is not None and same_target else None
            if current is None or not same_target or gap > gap_seconds * 1e6:
                current = {"id": f'{source_key}:{stroke["session_id"]}:{stroke["epoch"]}:{stroke["first_seq"]}',
                           "session_id": stroke["session_id"], "window_id": stroke["window_id"],
                           "display_id": stroke["display_id"], "start_us": stroke["start_us"],
                           "end_us": stroke["end_us"], "first_seq": stroke["first_seq"],
                           "gap_before_seconds": round(gap / 1e6, 6) if gap is not None else None,
                           "strokes": []}
                candidates.append(current)
            current["strokes"].append(stroke)
            current["end_us"] = max(current["end_us"], stroke["end_us"])
            previous_end = current["end_us"]
            previous_target = target
    for candidate in candidates:
        ink = [s for s in candidate["strokes"] if s["tool_type"] in PEN_TOOLS]
        intervals = [(s["start_us"], s["end_us"]) for s in ink]
        writing = union_duration(intervals)
        span = candidate["end_us"] - candidate["start_us"]
        candidate.update(elapsed_seconds=round(span / 1e6, 6),
                         observed_writing_seconds=round(writing / 1e6, 6),
                         other_seconds=round(max(0, span - writing) / 1e6, 6),
                         stroke_count=len(ink),
                         eraser_stroke_count=len(candidate["strokes"]) - len(ink),
                         issues=sorted({issue for s in candidate["strokes"] for issue in s["issues"]}))
        for stroke in candidate["strokes"]:
            stroke["issues"] = sorted(stroke["issues"])
    return {"path": str(path.resolve()), "record_count": records,
            "selected_pen_or_eraser_points": selected, "invalid_records": invalid,
            "incomplete_tail_records": tail, "discontinuities": discontinuities,
            "synthetic": synthetic, "window_ids": sorted(windows), "candidates": candidates}


class StudyStore:
    def __init__(self, records, directory, output, gap_seconds=8, window_id=None):
        self.records = records
        self.directory = directory
        self.output = output
        self.gap_seconds = gap_seconds
        self.window_id = window_id
        self.lock = threading.RLock()
        self.cache = {}
        self.report = None
        self.annotation_path = output / "annotations.json"
        self.annotations = json.loads(self.annotation_path.read_text(encoding="utf-8")) if self.annotation_path.exists() else {}
        self.last_scan = 0

    def snapshot(self, force=False):
        with self.lock:
            if self.report is not None and not force and time.monotonic() - self.last_scan < 1:
                return self.report
            paths = [self.records] if self.records else sorted(self.directory.glob("*.jsonl"))
            sources = []
            for path in paths:
                try:
                    stat = path.stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                    cached = self.cache.get(path)
                    if not cached or cached[0] != signature:
                        cached = (signature, analyze_records(path, self.gap_seconds, self.window_id))
                        self.cache[path] = cached
                    source = dict(cached[1])
                    meta_path = path.with_suffix(".session.json")
                    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
                    source["capture_state"] = meta.get("state", "unknown")
                    source["loss_accounting_complete"] = meta.get("loss_accounting_complete")
                    source["dropped_count"] = meta.get("dropped_count")
                    source["device_started_at_ms"] = meta.get("started_at")
                    source["source_modified_at_pc"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                    sources.append(source)
                except (OSError, ValueError) as error:
                    sources.append({"path": str(path), "error": str(error), "candidates": []})
            candidates = [dict(c, source_path=s["path"], capture_state=s.get("capture_state", "unknown"),
                               annotation=self.annotations.get(c["id"], {})) for s in sources for c in s["candidates"]]
            report = {"schema": "inkbridge-study-timeline-v1", "generated_at_pc": utc_now(),
                      "timing_basis": "device_action_time_microseconds",
                      "candidate_rule": {"pen_gap_seconds": self.gap_seconds, "window_id": self.window_id},
                      "semantic_steps_verified": False, "recognition_available": False,
                      "notes_final_state_available": False,
                      "limits": ["候选片段按停笔间隔切分，不代表已经识别解题步骤。",
                                 "未书写时间不等于思考时间；读题、切页和离开均可能。",
                                 "轨迹为屏幕坐标；华为笔记缩放、撤销及最终擦除状态未同步。",
                                 "缺失/未闭合笔画的时长仅是已观察部分；回补不代表此刻仍在书写。"],
                      "sources": [{k: v for k, v in s.items() if k != "candidates"} for s in sources],
                      "candidates": candidates,
                      "summary": {"candidate_count": len(candidates),
                                  "observed_writing_seconds": round(sum(c["observed_writing_seconds"] for c in candidates), 6),
                                  "eraser_stroke_count": sum(c["eraser_stroke_count"] for c in candidates),
                                  "annotated_candidate_count": sum(bool(c["annotation"]) for c in candidates)}}
            save_json(self.output / "latest.json", report)
            save_json(self.output / "assistant-context.json", assistant_context(report))
            self.report = report
            self.last_scan = time.monotonic()
            return report

    def annotate(self, value):
        if not isinstance(value, dict) or set(value) != {"id", "annotation"}:
            raise ValueError("Expected id and annotation")
        candidate_id, annotation = value["id"], value["annotation"]
        if not isinstance(candidate_id, str) or not isinstance(annotation, dict):
            raise ValueError("Invalid annotation")
        if set(annotation) - ANNOTATION_FIELDS or any(not isinstance(v, str) or len(v) > 2000 for v in annotation.values()):
            raise ValueError("Invalid annotation fields")
        with self.lock:
            report = self.snapshot(force=True)
            if candidate_id not in {c["id"] for c in report["candidates"]}:
                raise ValueError("Unknown candidate")
            clean = {k: v.strip() for k, v in annotation.items() if v.strip()}
            if clean:
                clean["evidence"] = "user_report"
                clean["updated_at_pc"] = utc_now()
                self.annotations[candidate_id] = clean
            else:
                self.annotations.pop(candidate_id, None)
            save_json(self.annotation_path, self.annotations)
            return self.snapshot(force=True)


def make_server(store, port):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, value, kind="application/json; charset=utf-8", status=200):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed(self):
            # Accept only loopback host and same-origin browser writes.
            host = self.headers.get("Host", "")
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if host not in allowed:
                return False
            origin = self.headers.get("Origin")
            return not origin or origin == "http://" + host

        def do_GET(self):
            if not self.allowed():
                self.respond({"error": "Invalid origin"}, status=403)
            elif urlsplit(self.path).path == "/":
                self.respond((ROOT / "viewer/study.html").read_bytes(), "text/html; charset=utf-8")
            elif urlsplit(self.path).path == "/api/study":
                self.respond(store.snapshot())
            else:
                self.respond({"error": "Not found"}, status=404)

        def do_POST(self):
            if not self.allowed():
                self.respond({"error": "Invalid origin"}, status=403)
                return
            if self.path != "/api/annotation":
                self.respond({"error": "Not found"}, status=404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32768 or self.headers.get_content_type() != "application/json":
                    raise ValueError("Expected JSON up to 32KiB")
                self.respond(store.annotate(json.loads(self.rfile.read(size))))
            except (ValueError, UnicodeDecodeError) as error:
                self.respond({"error": str(error)}, status=400)

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--records", type=Path)
    inputs.add_argument("--sessions-dir", type=Path, default=ROOT / "output/ink/sessions")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/study")
    parser.add_argument("--gap-seconds", type=float, default=8)
    parser.add_argument("--window-id", type=int, help="Only verified Notes window ID; IDs can change")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=18767)
    args = parser.parse_args()
    if not math.isfinite(args.gap_seconds) or args.gap_seconds <= 0 or not 1 <= args.port <= 65535:
        parser.error("Invalid gap or port")
    if args.records and not args.records.is_file():
        parser.error("Records file does not exist")
    store = StudyStore(args.records, args.sessions_dir, args.output_dir, args.gap_seconds, args.window_id)
    report = store.snapshot()
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    print(f"Study evidence: {(args.output_dir / 'latest.json').resolve()}", flush=True)
    if args.serve:
        server = make_server(store, args.port)
        print(f"Study timeline: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever(poll_interval=.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
