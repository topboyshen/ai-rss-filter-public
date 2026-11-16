"""Explicit browser origins, separate from internal feed/service addresses.

Rendering only: never infer an origin from Host, CORS or WEB_BASE_URL, and
never fetch the configured address. Invalid or absent origins disable links.
"""

import ipaddress
import os
import re
from urllib.parse import urlsplit


def _origin(variable: str) -> str | None:
    value = os.environ.get(variable, "")
    if not value or len(value) > 2048 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if any(char in value for char in "\\\"'<>%"):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or "?" in value or "#" in value
                or parsed.netloc.endswith(":") or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or host.rstrip(".").lower().endswith(".invalid") or host.lower() == "invalid"):
            return None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                       for label in host.split(".")):
                return None
        authority = "[" + host + "]" if ":" in host else host
        if parsed.port is not None:
            authority += ":" + str(parsed.port)
        if parsed.netloc.lower() != authority.lower():
            return None
        return parsed.scheme + "://" + authority.lower()
    except ValueError:
        return None


def dashboard_origin() -> str | None:
    return _origin("AI_FILTER_PUBLIC_DASHBOARD_ORIGIN")


def news_home_origin() -> str | None:
    return _origin("AI_FILTER_NEWS_HOME_ORIGIN")
