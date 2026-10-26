"""Beijing wall-clock helpers that do not depend on the process ``TZ``.

Production Compose sets ``TZ=Asia/Shanghai``, but CI, local scripts and other
hosts may not.  These helpers keep the established *naive* Beijing wall-clock
shape (no ``+08:00`` suffix) so rendered strings and stored day keys stay
byte-identical to what production emits today, while never reading the host
zone.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """Current Beijing wall-clock time as a naive datetime."""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def beijing_fromtimestamp(timestamp: float) -> datetime:
    """Beijing wall-clock time of a Unix timestamp as a naive datetime."""
    return datetime.fromtimestamp(float(timestamp), BEIJING_TZ).replace(tzinfo=None)


def utc_naive_fromtimestamp(timestamp: float) -> datetime:
    """Non-deprecated replacement for ``datetime.utcfromtimestamp``."""
    return datetime.fromtimestamp(float(timestamp), timezone.utc).replace(tzinfo=None)
