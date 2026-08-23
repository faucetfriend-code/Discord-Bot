"""force_ipv4() must leave urllib3 resolving AF_INET only."""

import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import net_prefs  # noqa: E402


def test_force_ipv4_pins_urllib3_to_af_inet(monkeypatch):
    import urllib3.util.connection as conn

    monkeypatch.setattr(net_prefs, "_applied", None)
    monkeypatch.setenv("FORCE_IPV4", "true")
    assert net_prefs.force_ipv4() is True
    assert conn.HAS_IPV6 is False
    assert conn.allowed_gai_family() == socket.AF_INET
    # idempotent
    assert net_prefs.force_ipv4() is True


def test_force_ipv4_disabled_by_env(monkeypatch):
    monkeypatch.setattr(net_prefs, "_applied", None)
    monkeypatch.setenv("FORCE_IPV4", "false")
    assert net_prefs.force_ipv4() is False
    assert net_prefs.force_ipv4_enabled() is False
