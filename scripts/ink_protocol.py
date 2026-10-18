"""Validation for the public Input Kit JSONL point protocol."""
import math
import re

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def validate_session_id(value):
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise ValueError("Invalid session_id")
    return value

INTEGER_FIELDS = ("seq", "action", "pointer_id", "tool_type", "window_id", "display_id")
NUMBER_FIELDS = ("x", "y", "action_time", "down_time", "pressure")
PEN_TOOLS = frozenset((1, 3, 4, 5))


def validate_point(event):
    if not isinstance(event, dict) or event.get("type") != "point":
        raise ValueError("Expected a point object")
    if "session_id" in event:
        validate_session_id(event["session_id"])
    for key in INTEGER_FIELDS + NUMBER_FIELDS:
        value = event.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Missing numeric field: {key}")
        if not math.isfinite(value):
            raise ValueError(f"Non-finite field: {key}")
        if key in INTEGER_FIELDS and int(value) != value:
            raise ValueError(f"Expected integer field: {key}")
    if event["seq"] < 1 or event["action"] not in (0, 1, 2, 3):
        raise ValueError("Invalid sequence or action")
    # This tablet reports finger pressure above 1 (up to 6.41 in a real
    # recording). Keep finite non-negative device values without normalizing.
    if event["tool_type"] not in range(8) or event["pressure"] < 0:
        raise ValueError("Invalid tool or pressure")
    return event
