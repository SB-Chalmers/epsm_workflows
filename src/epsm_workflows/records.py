"""Portable identities and atomic records shared by study executors.

Records contain explicit scientific inputs. Runtime connections, callables and
mutable object representations are intentionally not serializable identities.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Mapping


def json_value(value):
    """Return finite JSON-compatible values, preserving explicit object contracts."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    if isinstance(value, (Path, date, datetime)):
        return str(value)
    if hasattr(value, "to_dict"):
        return json_value(value.to_dict())
    if hasattr(value, "model_dump"):
        return json_value(value.model_dump(mode="json"))
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, SimpleNamespace):
        return json_value(vars(value))
    if hasattr(value, "tolist"):
        return json_value(value.tolist())
    raise TypeError(f"{type(value).__name__} needs an explicit to_dict() contract")


def canonical(value):
    return json.dumps(json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic_json(path, value):
    """Replace a complete JSON record atomically after flushing its contents."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{os.getpid()}.pending")
    try:
        with pending.open("w", encoding="utf-8") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


@contextmanager
def exclusive_lock(path):
    """Prevent concurrent writers locally or on a filesystem supporting flock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"Already running: {path}") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
