"""LM Studio probe state: transitions only, seeded from the startup probe."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_probe import LlmProbeState, probe_interval_sec  # noqa: E402


def test_seeded_up_then_down_then_up():
    s = LlmProbeState()
    s.seed(True)
    assert s.update(True) is None
    assert s.update(False) == "down"
    assert s.update(False) is None
    assert s.update(True) == "up"
    assert s.update(True) is None


def test_unseeded_first_probe_reports_down_only():
    assert LlmProbeState().update(True) is None
    assert LlmProbeState().update(False) == "down"


def test_interval_parsing(monkeypatch):
    monkeypatch.setenv("LLM_PROBE_INTERVAL_MIN", "2")
    assert probe_interval_sec() == 120.0
    monkeypatch.setenv("LLM_PROBE_INTERVAL_MIN", "bogus")
    assert probe_interval_sec() == 30.0 * 60.0
    monkeypatch.setenv("LLM_PROBE_INTERVAL_MIN", "0")
    assert probe_interval_sec() == 0.0
