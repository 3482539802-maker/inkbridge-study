"""Durable protocol-v2 journals. ACK offsets refer to original UTF-8 bytes."""
import json
import os
import time
from pathlib import Path

from ink_protocol import validate_point, validate_session_id

MAX_BATCH = 32 * 1024


class MetadataPublishError(OSError):
    """No ACK may be sent until the durable metadata can be published."""


def publish_metadata(temporary, path):
    # Windows readers/AV can briefly deny replacement even after our fsync.
    # Keep the previous file intact, bound retries, and never turn a failed
    # rename into an ACK. A reconnect can safely retry the same metadata.
    for attempt in range(21):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as error:
            if attempt == 20:
                raise MetadataPublishError('Session metadata replacement remains blocked') from error
            time.sleep(.05)


def integer(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Expected a nonnegative integer")
    return value


class SessionStore:
    def __init__(self, directory, state):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state = state
        self.loaded = {}
        self.counts = {}

    def metadata(self, session_id, metadata):
        path = self.directory / (session_id + ".session.json")
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(metadata, output, ensure_ascii=False, allow_nan=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        publish_metadata(temporary, path)
        with self.state.lock:
            self.state.sessions[session_id] = dict(metadata)

    def hello(self, message):
        session_id = validate_session_id(message.get("session_id"))
        if message.get("protocol") != 2:
            raise ValueError("Unsupported session protocol")
        path = self.directory / (session_id + ".jsonl")
        meta_path = self.directory / (session_id + ".session.json")
        if session_id not in self.loaded:
            previous = 0
            count = 0
            # An unacknowledged torn tail can be discarded; complete lines are retained.
            with path.open("a+b") as journal:
                journal.seek(0)
                boundary = 0
                while line := journal.readline(MAX_BATCH + 1):
                    if len(line) > MAX_BATCH:
                        raise ValueError("Corrupt oversized journal record")
                    if not line.endswith(b"\n"):
                        journal.truncate(boundary)
                        journal.flush()
                        os.fsync(journal.fileno())
                        break
                    point = validate_point(json.loads(line))
                    if point.get("session_id") != session_id or point["seq"] <= previous:
                        raise ValueError("Corrupt session journal")
                    previous = point["seq"]
                    count += 1
                    boundary = journal.tell()
                # Validate first, then publish. A corrupt journal must not add a
                # valid prefix repeatedly to the viewer on each retry.
                journal.seek(0)
                published_previous = 0
                for line in journal:
                    point = json.loads(line)
                    self._publish(point, published_previous)
                    published_previous = point["seq"]
            self.loaded[session_id] = previous
            self.counts[session_id] = count
        metadata = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        metadata.update(session_id=session_id, protocol=2,
                        started_at=message.get("started_at"), raw_path=str(path.resolve()),
                        byte_length=path.stat().st_size, point_count=self.counts[session_id])
        metadata.setdefault("state", "recording")
        self.metadata(session_id, metadata)
        return self.ack(session_id)

    def ack(self, session_id, complete=False):
        return {"type": "ack", "session_id": session_id,
                "offset": (self.directory / (session_id + ".jsonl")).stat().st_size,
                "complete": complete}

    def _publish(self, point, previous):
        with self.state.lock:
            self.state.gaps += max(0, point["seq"] - previous - 1)
        self.state.append(point)

    def batch(self, session_id, message):
        if message.get("session_id") != session_id:
            raise ValueError("Session mismatch")
        offset = integer(message.get("offset"))
        data = message.get("data")
        if not isinstance(data, str):
            raise ValueError("Expected raw JSONL string")
        payload = data.encode("utf-8")
        if not payload or len(payload) > MAX_BATCH or not payload.endswith(b"\n"):
            raise ValueError("Invalid batch length or boundary")
        path = self.directory / (session_id + ".jsonl")
        size = path.stat().st_size
        if offset < size:
            with path.open("rb") as journal:
                journal.seek(offset)
                if offset + len(payload) > size or journal.read(len(payload)) != payload:
                    raise ValueError("Replay differs from persisted bytes")
            with self.state.lock:
                self.state.replays += 1
            return self.ack(session_id)
        if offset != size:
            raise ValueError("Batch offset must match persisted boundary")
        if self.state.sessions[session_id].get("state") in ("completed", "interrupted"):
            raise ValueError("Cannot append to a finished session")
        previous = self.loaded[session_id]
        points = []
        for line in payload.splitlines():
            point = validate_point(json.loads(line))
            if point.get("session_id") != session_id or point["seq"] <= previous:
                raise ValueError("Invalid session or sequence order")
            points.append(point)
            previous = point["seq"]
        with path.open("ab") as journal:
            journal.write(payload)
            journal.flush()
            os.fsync(journal.fileno())
        for point in points:
            self._publish(point, self.loaded[session_id])
            self.loaded[session_id] = point["seq"]
        self.counts[session_id] += len(points)
        with self.state.lock:
            self.state.sessions[session_id]["byte_length"] = size + len(payload)
            self.state.sessions[session_id]["point_count"] = self.counts[session_id]
        return self.ack(session_id)

    def finish(self, session_id, message):
        if message.get("session_id") != session_id:
            raise ValueError("Session mismatch")
        if integer(message.get("byte_length")) != self.ack(session_id)["offset"]:
            raise ValueError("Session is not fully persisted")
        status = message.get("state")
        if status not in ("completed", "interrupted"):
            raise ValueError("Invalid final state")
        if integer(message.get("point_count")) != self.counts[session_id]:
            raise ValueError("Final record count does not match the journal")
        if not isinstance(message.get("loss_accounting_complete", True), bool):
            raise ValueError("Invalid loss accounting status")
        metadata = dict(self.state.sessions[session_id])
        metadata.update(state=status, ended_at=message.get("ended_at"),
                        stop_reason=message.get("stop_reason"),
                        point_count=integer(message.get("point_count")),
                        dropped_count=integer(message.get("dropped_count")),
                        loss_accounting_complete=message.get("loss_accounting_complete", True),
                        byte_length=message["byte_length"])
        self.metadata(session_id, metadata)
        return self.ack(session_id, complete=True)
