"""Atomic derived-file publication; Windows readers may briefly deny rename.

This module does not participate in raw journal ACKs. Durable evidence writes
raise on failure. Only disposable status views may use publish_status().
"""
import json
import logging
import os
from pathlib import Path
import tempfile
import time


def replace_file(temporary, target):
    for attempt in range(21):
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if attempt == 20:
                raise
            time.sleep(.05)


def atomic_bytes(path, payload):
    path = Path(path)
    # Unique same-directory files prevent two publishers from sharing a temp
    # inode. The destination always contains one complete generation.
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_last_status_warning = float('-inf')


def publish_status(path, value):
    """A status-file outage must not terminate the worker it describes."""
    global _last_status_warning
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode()
    try:
        atomic_bytes(path, payload)
        return True
    except OSError as error:
        now = time.monotonic()
        if now - _last_status_warning >= 30:
            logging.warning('Status publication unavailable (%s): %s', path, error)
            _last_status_warning = now
        return False
