"""Timing, incomplete capture and persisted learning-annotation checks."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from study_timeline import ROOT, StudyStore, analyze_records, make_server, save_json


def point(seq, seconds, action, **values):
    return dict(type="point", session_id="test-session", seq=seq, action=action,
                pointer_id=0, x=seq * 3, y=25, action_time=seconds * 1e6,
                down_time=0, pressure=.5, tool_type=1, window_id=156, display_id=0,
                **values)


def journal(path, points):
    path.write_bytes(b"".join((json.dumps(p) + "\n").encode() for p in points))


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ink-study-")
        self.root = Path(self.temp.name)
        self.raw = self.root / "capture.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def test_contact_time_and_unclassified_pause(self):
        journal(self.raw, [point(1, 0, 1), point(2, 2, 3),
                           point(3, 5, 1), point(4, 6, 3),
                           point(5, 20, 1), point(6, 21, 3)])
        candidates = analyze_records(self.raw)["candidates"]
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["elapsed_seconds"], 6)
        self.assertEqual(candidates[0]["observed_writing_seconds"], 3)
        self.assertEqual(candidates[0]["other_seconds"], 3)
        self.assertEqual(candidates[1]["gap_before_seconds"], 14)
        self.assertNotIn("thinking_seconds", candidates[0])

    def test_delayed_backfill_uses_device_time(self):
        points = [point(1, 100, 1), point(2, 102, 3)]
        journal(self.raw, [dict(p, received_pc="2026-10-14T00:00:00Z") for p in points])
        before = analyze_records(self.raw)
        journal(self.raw, [dict(p, received_pc="2026-10-16T00:00:00Z") for p in points])
        self.assertEqual(before, analyze_records(self.raw))

    def test_no_time_counted_across_missing_event(self):
        journal(self.raw, [point(1, 0, 1), point(2, 1, 2), point(4, 30, 3)])
        report = analyze_records(self.raw)
        self.assertEqual(report["discontinuities"], 1)
        self.assertEqual(sum(c["observed_writing_seconds"] for c in report["candidates"]), 1)
        self.assertIn("sequence_or_clock_discontinuity", report["candidates"][0]["issues"])
        self.assertIn("missing_down", report["candidates"][1]["issues"])

    def test_clock_reset_and_sessions_are_separate(self):
        journal(self.raw, [point(1, 100, 1), point(2, 102, 3),
                           point(3, 0, 1), point(4, 1, 3),
                           dict(point(1, 0, 1), session_id="another"),
                           dict(point(2, 2, 3), session_id="another")])
        report = analyze_records(self.raw)
        self.assertEqual(len(report["candidates"]), 3)
        self.assertEqual(sum(c["observed_writing_seconds"] for c in report["candidates"]), 5)

    def test_incomplete_tail_is_not_read_or_truncated(self):
        journal(self.raw, [point(1, 0, 1)])
        with self.raw.open("ab") as stream:
            stream.write(json.dumps(point(2, 2, 3)).encode())
        original = self.raw.read_bytes()
        before = analyze_records(self.raw)
        self.assertEqual(before["incomplete_tail_records"], 1)
        self.assertEqual(before["candidates"][0]["observed_writing_seconds"], 0)
        self.assertIn("open_stroke", before["candidates"][0]["issues"])
        self.assertEqual(self.raw.read_bytes(), original)
        with self.raw.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(analyze_records(self.raw)["candidates"][0]["observed_writing_seconds"], 2)

    def test_overlapping_pointer_intervals_do_not_double_count(self):
        journal(self.raw, [point(1, 0, 1), dict(point(2, 1, 1), pointer_id=1),
                           point(3, 2, 3), dict(point(4, 3, 3), pointer_id=1)])
        candidate = analyze_records(self.raw)["candidates"][0]
        self.assertEqual(candidate["stroke_count"], 2)
        self.assertEqual(candidate["observed_writing_seconds"], 3)

    def test_eraser_is_separate_from_writing(self):
        journal(self.raw, [point(1, 0, 1), point(2, 1, 3),
                           dict(point(3, 2, 1), tool_type=2), dict(point(4, 4, 3), tool_type=2)])
        candidate = analyze_records(self.raw)["candidates"][0]
        self.assertEqual(candidate["observed_writing_seconds"], 1)
        self.assertEqual(candidate["eraser_stroke_count"], 1)
        self.assertEqual(candidate["other_seconds"], 3)

    def test_target_switch_does_not_mix_candidates(self):
        journal(self.raw, [point(1, 0, 1), point(2, 1, 3),
                           dict(point(3, 2, 1), window_id=25), dict(point(4, 3, 3), window_id=25),
                           point(5, 4, 1), point(6, 5, 3)])
        report = analyze_records(self.raw)
        self.assertEqual([c["window_id"] for c in report["candidates"]], [156, 25, 156])
        filtered = analyze_records(self.raw, window_id=156)
        self.assertEqual(len(filtered["candidates"]), 2)

    def test_invalid_input_breaks_instead_of_bridging(self):
        journal(self.raw, [point(1, 0, 1)])
        with self.raw.open("ab") as stream:
            stream.write(b'{"type":"point"}\n')
            stream.write((json.dumps(point(3, 100, 3)) + "\n").encode())
        report = analyze_records(self.raw)
        self.assertEqual(report["invalid_records"], 1)
        self.assertEqual(sum(c["observed_writing_seconds"] for c in report["candidates"]), 0)

    def test_live_append_and_annotation_restart_preserve_raw(self):
        journal(self.raw, [point(1, 0, 1), point(2, 2, 3)])
        store = StudyStore(self.raw, None, self.root / "study")
        candidate_id = store.snapshot()["candidates"][0]["id"]
        with self.raw.open("ab") as stream:
            stream.write((json.dumps(point(3, 4, 1)) + "\n").encode())
            stream.write((json.dumps(point(4, 6, 3)) + "\n").encode())
        digest = hashlib.sha256(self.raw.read_bytes()).hexdigest()
        report = store.annotate({"id": candidate_id, "annotation": {"subject": "机械设计基础", "step": "列方程"}})
        self.assertEqual(report["candidates"][0]["observed_writing_seconds"], 4)
        self.assertFalse(report["semantic_steps_verified"])
        restarted = StudyStore(self.raw, None, self.root / "study").snapshot()
        self.assertEqual(restarted["candidates"][0]["annotation"]["step"], "列方程")
        self.assertEqual(restarted["candidates"][0]["annotation"]["evidence"], "user_report")
        self.assertEqual(hashlib.sha256(self.raw.read_bytes()).hexdigest(), digest)
        context = json.loads((self.root / "study/assistant-context.json").read_text(encoding="utf-8"))
        self.assertNotIn("strokes", context["candidates"][0])
        self.assertEqual(context["confirmed_label_groups"][0]["observed_writing_seconds"], 4)
        self.assertFalse(context["automatic_model_calls"])

    def test_http_annotation_validation_and_origin(self):
        journal(self.raw, [point(1, 0, 1), point(2, 2, 3)])
        store = StudyStore(self.raw, None, self.root / "study")
        server = make_server(store, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/") as response:
                self.assertIn("学习时间线", response.read().decode())
            with urlopen(base + "/api/study") as response:
                candidate_id = json.load(response)["candidates"][0]["id"]
            payload = json.dumps({"id": candidate_id, "annotation": {"step": "建立边界条件"}}).encode()
            request = Request(base + "/api/annotation", data=payload, headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                self.assertEqual(json.load(response)["summary"]["annotated_candidate_count"], 1)
            request.add_header("Origin", "https://unrelated.example")
            with self.assertRaises(HTTPError) as context:
                urlopen(request)
            self.assertEqual(context.exception.code, 403)
            bad = Request(base + "/api/annotation", data=b'{"id":"missing","annotation":{}}', headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as context:
                urlopen(bad)
            self.assertEqual(context.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    if not result.wasSuccessful():
        raise SystemExit(1)
    evidence = {"passed": True, "synthetic_checks": result.testsRun,
                "new_tablet_capture_verified": False,
                "checks": ["device-time durations independent of packet arrival",
                           "sequence gaps and clock reset", "partial tails stay read-only",
                           "pointer overlap", "eraser vs writing", "window separation",
                           "live journal append", "annotation persistence", "HTTP and origin validation"]}
    historical = ROOT / "output/wireless/final-device.jsonl"
    if historical.exists():
        replay = analyze_records(historical)
        candidates = replay["candidates"]
        assert replay["record_count"] == 3228
        assert sum(c["stroke_count"] for c in candidates) == 55
        assert not replay["invalid_records"] and not replay["discontinuities"]
        evidence["historical_device_replay"] = {
            "sha256": hashlib.sha256(historical.read_bytes()).hexdigest(),
            "record_count": replay["record_count"], "stroke_count": 55,
            "candidate_count": len(candidates),
            "observed_writing_seconds": round(sum(c["observed_writing_seconds"] for c in candidates), 6)}
    save_json(ROOT / "output/tests/study-timeline-report.json", evidence)
