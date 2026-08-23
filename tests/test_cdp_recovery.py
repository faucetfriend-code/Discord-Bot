"""CDP tab-loss recovery state machine: thresholds, cooldown, escalation,
alert-once semantics."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdp_recovery import (  # noqa: E402
    DEGRADED,
    EXHAUSTED,
    HEALTHY,
    CdpRecovery,
    TransitionAlert,
)


def _machine(after_n=3, cooldown=600.0, max_attempts=2):
    return CdpRecovery(after_n=after_n, cooldown_sec=cooldown, max_attempts=max_attempts)


def test_silent_below_threshold():
    m = _machine(after_n=3)
    for t in (0, 10):
        d = m.observe(False, now=t)
        assert not d.reattach and d.alert is None
    assert m.state == HEALTHY


def test_first_recovery_fires_alert_once_and_reattaches():
    m = _machine(after_n=3)
    m.observe(False, now=0)
    m.observe(False, now=10)
    d = m.observe(False, now=20, tab_present=False)
    assert d.reattach and d.open_tab
    assert d.alert and "Lost the Discord tab" in d.alert
    assert d.alert_key == "cdp_no_tab"
    assert m.state == DEGRADED
    # Next failed tick inside the cooldown: no alert, no action.
    d2 = m.observe(False, now=30)
    assert not d2.reattach and d2.alert is None


def test_open_tab_only_when_tab_missing():
    m = _machine(after_n=1)
    d = m.observe(False, now=0, tab_present=True)
    assert d.reattach and not d.open_tab


def test_cooldown_spaces_attempts_then_escalates():
    m = _machine(after_n=1, cooldown=600.0, max_attempts=2)
    assert m.observe(False, now=0).reattach          # attempt 1
    assert not m.observe(False, now=100).reattach    # cooling
    d = m.observe(False, now=600)                    # attempt 2
    assert d.reattach and d.alert is None            # no second alert
    assert m.attempts == 2
    assert not m.observe(False, now=700).reattach    # cooling after final attempt
    d = m.observe(False, now=1200)                   # cooldown elapsed, out of attempts
    assert d.alert and "MANUAL RESTART NEEDED" in d.alert
    assert d.alert_key == "cdp_manual_restart"
    assert m.state == EXHAUSTED
    # Exhausted: no more retries, no more alerts until a success.
    for t in (1300, 5000, 99999):
        d = m.observe(False, now=t)
        assert not d.reattach and d.alert is None


def test_success_resets_and_alerts_recovery_once():
    m = _machine(after_n=1, cooldown=0.0, max_attempts=1)
    m.observe(False, now=0)
    m.observe(False, now=1)  # escalates to exhausted
    assert m.state == EXHAUSTED
    d = m.observe(True, now=2)
    assert d.alert and "recovered" in d.alert and d.alert_level == "info"
    assert m.state == HEALTHY and m.consec_failures == 0 and m.attempts == 0
    # Healthy -> healthy is silent.
    assert m.observe(True, now=3).alert is None
    # A fresh outage restarts the attempt budget.
    d = m.observe(False, now=4)
    assert d.reattach and d.alert is not None


def test_from_env_tolerates_garbage(monkeypatch):
    monkeypatch.setenv("CDP_RECOVER_AFTER_N", "abc")
    monkeypatch.setenv("CDP_RECOVER_COOLDOWN_MIN", "2")
    monkeypatch.setenv("CDP_RECOVER_MAX_ATTEMPTS", "")
    m = CdpRecovery.from_env()
    assert m.after_n == 5 and m.cooldown_sec == 120.0 and m.max_attempts == 3


def test_transition_alert_edges_only():
    t = TransitionAlert()
    assert t.update(False) is None
    assert t.update(True) == "down"
    assert t.update(True) is None
    assert t.update(True) is None
    assert t.update(False) == "up"
    assert t.update(False) is None
