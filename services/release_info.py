"""Small, dependency-free helpers for reporting image build provenance."""

from __future__ import annotations

import os
import re


_SAFE_RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")


def release_version() -> str:
    """Return a bounded image version without trusting arbitrary HTML text."""
    value = (os.environ.get("AI_FILTER_VERSION") or "").strip()
    if not _SAFE_RELEASE_RE.fullmatch(value):
        return "development"
    return value
