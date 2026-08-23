"""
Outbound network preferences.

BloFin API keys are IP-whitelisted. On a dual-stack Windows host the OS prefers
IPv6 whenever a hostname has AAAA records, so requests to openapi.blofin.com
leave from an IPv6 address that is NOT on the whitelist (error 152406). The
whitelist holds the public IPv4, so outbound HTTPS must resolve AF_INET only.

The installed `blofin` SDK (and everything else in this repo that talks HTTP)
uses `requests` -> urllib3, so the fix is a urllib3-level switch:
`urllib3.util.connection.HAS_IPV6 = False` makes `allowed_gai_family()` return
AF_INET, and we also patch that function directly in case a different urllib3
build evaluates HAS_IPV6 at import time.

Gated on env FORCE_IPV4 (default "true"). Idempotent - safe to call from
several modules at import time.
"""

import os
import socket
from typing import Optional

_applied: Optional[bool] = None


def _truthy(val: Optional[str], default: bool) -> bool:
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def force_ipv4_enabled() -> bool:
    """Return True when FORCE_IPV4 is on (default)."""
    return _truthy(os.getenv("FORCE_IPV4"), True)


def force_ipv4() -> bool:
    """
    Make urllib3 / requests resolve hostnames to IPv4 only.

    Returns True if the patch is active (now or from an earlier call), False if
    FORCE_IPV4 is disabled or urllib3 is unavailable. Logs one INFO line the
    first time it is applied.
    """
    global _applied
    if _applied is not None:
        return _applied

    if not force_ipv4_enabled():
        _applied = False
        return False

    try:
        import urllib3.util.connection as _conn
    except Exception:  # urllib3 not installed - nothing to patch
        _applied = False
        return False

    _conn.HAS_IPV6 = False

    def _ipv4_only_family() -> int:
        return socket.AF_INET

    _conn.allowed_gai_family = _ipv4_only_family
    _applied = True

    try:
        from logger import log

        log.info("FORCE_IPV4=true: urllib3/requests pinned to IPv4 (AF_INET) for outbound HTTPS")
    except Exception:
        pass
    return True
