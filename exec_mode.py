"""
exec_mode.py - one place that decides HOW the bot executes: PAPER, DEMO or LIVE.

The mode is derived from the environment, never set directly:

    PAPER_MODE=true                                   -> PAPER
    BLOFIN_BASE_URL = demo-trading-openapi.blofin.com -> DEMO
    anything else (https://openapi.blofin.com)        -> LIVE

  PAPER  never calls the exchange for orders: positions are simulated in-process
         and carry a DRYRUN-<symbol>-<ts> order id (the old DRY_RUN=true path).
  DEMO   places REAL orders on the BloFin demo account with the Demo-* keys and
         no brokerId. Every code path is the live one - fills, amends, closes,
         reconciliation - only the endpoint and keys differ.
  LIVE   real money on https://openapi.blofin.com with the live keys.

`DRY_RUN` is now DERIVED (dry_run = mode != LIVE) and only used to TAG records
(trades.dry_run, dashboard badge). A DRY_RUN value in .env that disagrees with
the derived one is ignored; `dry_run_warning()` returns the one-line warning
the caller should log once at startup.

Flipping DEMO -> LIVE is therefore a single change: BLOFIN_BASE_URL.

Pure module: reads os.environ (or a supplied mapping), no logging, no I/O, so
it is safe to import from tests, preflight and the dashboard.
"""

import os
from enum import Enum
from typing import Mapping, Optional

DEMO_BASE_URL = "https://demo-trading-openapi.blofin.com"
LIVE_BASE_URL = "https://openapi.blofin.com"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ExecMode(str, Enum):
    """How orders are executed. `str` mixin so it serialises cleanly."""

    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE = "LIVE"


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def _parse_bool(raw: Optional[str]) -> Optional[bool]:
    """True/False for a recognised boolean string, None when unset/unparseable."""
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return None


def base_url(env: Optional[Mapping[str, str]] = None) -> str:
    """BLOFIN_BASE_URL as configured (default: the demo endpoint, matching
    blofin_client._get_client)."""
    raw = _env(env).get("BLOFIN_BASE_URL", "") or DEMO_BASE_URL
    return raw.strip().rstrip("/")


def is_demo_url(url: str) -> bool:
    """Same rule blofin_client._select_credentials uses to pick Demo-* keys."""
    return "demo" in url.lower()


def resolve_exec_mode(env: Optional[Mapping[str, str]] = None) -> ExecMode:
    """Derive the execution mode from PAPER_MODE and BLOFIN_BASE_URL."""
    if _parse_bool(_env(env).get("PAPER_MODE")) is True:
        return ExecMode.PAPER
    if is_demo_url(base_url(env)):
        return ExecMode.DEMO
    return ExecMode.LIVE


# Mode pinned at startup by bot.main() via freeze(). The BloFin client, keys and
# price stream are built once for that endpoint, so a later .env edit must not
# flip the trading path hot; mode() keeps answering with the frozen value.
_frozen: Optional[ExecMode] = None


def freeze(env: Optional[Mapping[str, str]] = None) -> ExecMode:
    """Resolve the mode once and pin it for the life of the process."""
    global _frozen
    _frozen = resolve_exec_mode(env)
    return _frozen


def unfreeze() -> None:
    """Drop the pin (tests)."""
    global _frozen
    _frozen = None


def mode(env: Optional[Mapping[str, str]] = None) -> ExecMode:
    """The effective mode: the frozen one when bot.main() pinned it, else the
    value resolved live from `env`/os.environ (preflight, dashboard, tests)."""
    if env is None and _frozen is not None:
        return _frozen
    return resolve_exec_mode(env)


def is_live(env: Optional[Mapping[str, str]] = None) -> bool:
    return mode(env) is ExecMode.LIVE


def is_paper(env: Optional[Mapping[str, str]] = None) -> bool:
    return mode(env) is ExecMode.PAPER


def calls_exchange(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when orders really go to BloFin (DEMO or LIVE)."""
    return mode(env) is not ExecMode.PAPER


def is_dry_run(env: Optional[Mapping[str, str]] = None) -> bool:
    """Derived record-tagging flag: everything that is not LIVE is dry_run=1."""
    return not is_live(env)


def dry_run_warning(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """One-line warning when a legacy DRY_RUN value disagrees with the derived
    flag (or is unparseable); None when DRY_RUN is unset or agrees."""
    raw = _env(env).get("DRY_RUN")
    if raw is None or raw.strip() == "":
        return None
    parsed = _parse_bool(raw)
    derived = is_dry_run(env)
    if parsed is not None and parsed == derived:
        return None
    return (
        "DRY_RUN is derived from BLOFIN_BASE_URL now; "
        f"ignoring DRY_RUN={raw.strip()} (derived dry_run={derived}, "
        f"mode={mode(env).value})"
    )


def label(env: Optional[Mapping[str, str]] = None) -> str:
    """Short banner/log label, e.g. "DEMO (real orders on demo-trading-openapi.blofin.com, Demo-* keys)"."""
    m = mode(env)
    url = base_url(env)
    host = url.split("//", 1)[-1]
    if m is ExecMode.PAPER:
        return "PAPER (no exchange calls; simulated fills)"
    if m is ExecMode.DEMO:
        return f"DEMO (real orders on {host}, Demo-* keys)"
    return f"LIVE (real money on {host}, live keys)"


def banner(env: Optional[Mapping[str, str]] = None) -> str:
    """The startup banner line."""
    return f"EXEC MODE: {label(env)}"


def dashboard_badge(env: Optional[Mapping[str, str]] = None) -> str:
    """Badge text for the dashboard header."""
    return {ExecMode.PAPER: "PAPER", ExecMode.DEMO: "DEMO",
            ExecMode.LIVE: "LIVE"}[mode(env)]
