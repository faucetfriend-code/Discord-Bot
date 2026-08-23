"""Bounded self-recovery state machine for the Chrome/CDP Discord capture path.

bot.py feeds one health observation per main-loop tick (tab present and inbox
not stale). After CDP_RECOVER_AFTER_N consecutive failed observations the
machine asks the caller to re-discover the Discord tab and re-attach the
real-time listener; if the tab is genuinely gone it also asks for a new tab to
be opened (Target.createTarget). Attempts are spaced by
CDP_RECOVER_COOLDOWN_MIN and capped at CDP_RECOVER_MAX_ATTEMPTS, after which
the alert escalates to MANUAL RESTART NEEDED and retries stop until a healthy
observation arrives.

Alerts are emitted once per state transition, never once per tick. The class
is pure (no I/O, clock injected) so the transition logic is unit-testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

HEALTHY = "healthy"
DEGRADED = "degraded"
EXHAUSTED = "exhausted"

MANUAL_RESTART_TEXT = (
    "MANUAL RESTART NEEDED: Discord capture could not be recovered after "
    "{attempts} attempt(s) - signals are NOT being received. Restart Chrome "
    "(run_bot.bat) and the bot."
)
LOST_TAB_TEXT = (
    "Lost the Discord tab (Chrome/CDP issue) - signals not being received; "
    "auto-recovery starting (up to {max_attempts} attempts, "
    "{cooldown_min:.0f} min apart)"
)
RECOVERED_TEXT = "Discord capture recovered after {failures} failed check(s)"


@dataclass
class Decision:
    """What bot.py should do after one observation."""

    reattach: bool = False
    open_tab: bool = False
    alert: str | None = None
    alert_level: str = "error"
    alert_key: str | None = None
    info: str | None = None


class CdpRecovery:
    """Transition logic for tab-loss / stale-inbox self-recovery."""

    def __init__(self, after_n: int = 5, cooldown_sec: float = 900.0,
                 max_attempts: int = 3):
        self.after_n = max(1, int(after_n))
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self.max_attempts = max(1, int(max_attempts))
        self.state = HEALTHY
        self.consec_failures = 0
        self.attempts = 0
        self.last_attempt_ts: float | None = None

    @classmethod
    def from_env(cls) -> "CdpRecovery":
        """Build from CDP_RECOVER_* env keys, tolerating malformed values."""

        def _num(key: str, default: float, cast=float):
            try:
                return cast(os.getenv(key, str(default)))
            except (TypeError, ValueError):
                return cast(default)

        return cls(
            after_n=_num("CDP_RECOVER_AFTER_N", 5, int),
            cooldown_sec=_num("CDP_RECOVER_COOLDOWN_MIN", 15) * 60.0,
            max_attempts=_num("CDP_RECOVER_MAX_ATTEMPTS", 3, int),
        )

    def observe(self, ok: bool, now: float, tab_present: bool = True) -> Decision:
        """Record one health check and return the action to take."""
        if ok:
            return self._on_success()
        self.consec_failures += 1
        if self.consec_failures < self.after_n:
            return Decision()
        if self.state == EXHAUSTED:
            return Decision()  # stop retrying until a sweep succeeds
        entering = self.state == HEALTHY
        if entering:
            self.state = DEGRADED
            self.attempts = 0
            self.last_attempt_ts = None
        cooled = (
            self.last_attempt_ts is None
            or now - self.last_attempt_ts >= self.cooldown_sec
        )
        if not cooled:
            return Decision()
        if self.attempts >= self.max_attempts:
            self.state = EXHAUSTED
            return Decision(
                alert=MANUAL_RESTART_TEXT.format(attempts=self.attempts),
                alert_level="error",
                alert_key="cdp_manual_restart",
            )
        self.attempts += 1
        self.last_attempt_ts = now
        decision = Decision(reattach=True, open_tab=not tab_present)
        if entering:
            decision.alert = LOST_TAB_TEXT.format(
                max_attempts=self.max_attempts,
                cooldown_min=self.cooldown_sec / 60.0,
            )
            decision.alert_level = "error"
            decision.alert_key = "cdp_no_tab"
        decision.info = (
            f"CDP recovery attempt {self.attempts}/{self.max_attempts} "
            f"(open_tab={decision.open_tab})"
        )
        return decision

    def _on_success(self) -> Decision:
        failures = self.consec_failures
        was = self.state
        self.consec_failures = 0
        self.state = HEALTHY
        self.attempts = 0
        self.last_attempt_ts = None
        if was == HEALTHY:
            return Decision()
        return Decision(
            alert=RECOVERED_TEXT.format(failures=failures),
            alert_level="info",
            alert_key="cdp_recovered",
            info=f"Discord capture healthy again (was {was})",
        )


class TransitionAlert:
    """Emit an alert only on down->up / up->down edges, never per tick."""

    def __init__(self):
        self.down = False

    def update(self, is_down: bool) -> str | None:
        """Return "down" or "up" on a transition, None otherwise."""
        if is_down and not self.down:
            self.down = True
            return "down"
        if not is_down and self.down:
            self.down = False
            return "up"
        return None
