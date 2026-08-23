"""Periodic LM Studio reachability probe.

LM Studio is a silent dependency: when it is down the text/vision fallback
parsers no-op without any log line. bot.py probes GET {base}/models every
LLM_PROBE_INTERVAL_MIN minutes and logs a WARNING on every up->down and
down->up transition (plus an alert on down). The state object is pure so the
transition logic is unit-testable; the HTTP call lives in probe_models().
"""

from __future__ import annotations

import os


def probe_models(base_url: str, timeout: float = 6.0) -> bool:
    """Cheap GET to the OpenAI-compatible /models endpoint. True if 200 + JSON."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return False
    try:
        import requests

        resp = requests.get(base + "/models", timeout=timeout)
        if resp.status_code != 200:
            return False
        resp.json()
        return True
    except Exception:
        return False


def probe_interval_sec(default_min: float = 30.0) -> float:
    """LLM_PROBE_INTERVAL_MIN in seconds; 0 disables the periodic probe."""
    try:
        return max(0.0, float(os.getenv("LLM_PROBE_INTERVAL_MIN", str(default_min)))) * 60.0
    except (TypeError, ValueError):
        return default_min * 60.0


class LlmProbeState:
    """Tracks up/down and reports transitions only."""

    def __init__(self):
        self.up: bool | None = None  # None = never probed

    def seed(self, up: bool) -> None:
        """Set the initial state from the startup probe without a transition."""
        self.up = bool(up)

    def update(self, up: bool) -> str | None:
        """Return "up" / "down" when the state flips, else None.

        The first observation after construction (never seeded) only reports
        "down" so a dead LM Studio is never silent, but a healthy first probe
        stays quiet.
        """
        up = bool(up)
        prev = self.up
        self.up = up
        if prev is None:
            return None if up else "down"
        if up and not prev:
            return "up"
        if prev and not up:
            return "down"
        return None
