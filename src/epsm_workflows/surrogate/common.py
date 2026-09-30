"""Compatibility imports for the study-specific design modules."""

import json
from pathlib import Path

from ..campaign import atomic_json, canonical, digest, file_sha256


def read_json(path):
    return json.loads(Path(path).read_text())

__all__ = ["atomic_json", "canonical", "digest", "file_sha256", "read_json"]
