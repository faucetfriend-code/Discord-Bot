"""Live epoch: the moment the bot switched from paper to real money.

Everything recorded before the epoch is dry-run test data; the dashboard shows
it as a "Paper archive" and reports live PnL/equity from the epoch onward.

Resolution order for the epoch timestamp:
  1. env LIVE_EPOCH_START (ISO-8601 with offset, e.g. 2026-08-23T11:00:36-05:00)
  2. bot.db  meta(key='live_epoch_start')
  3. none -> no epoch: the "live" view falls back to dry_run=0 only.

The live starting balance (meta 'live_start_balance', env LIVE_START_BALANCE)
anchors the live equity curve. Nothing here writes to the database; the
tools/set_live_epoch.py script owns the meta table writes.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

META_EPOCH_KEY = "live_epoch_start"
META_BALANCE_KEY = "live_start_balance"


def parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware datetime (naive -> UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def read_meta(con: sqlite3.Connection) -> dict[str, str]:
    """Return the meta key/value table as a dict ({} if the table is absent)."""
    try:
        rows = con.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error:
        return {}
    return {r[0]: r[1] for r in rows}


def get_epoch(con: sqlite3.Connection | None = None) -> datetime | None:
    """Resolve the live epoch from env first, then the meta table."""
    env_val = os.getenv("LIVE_EPOCH_START", "").strip()
    if env_val:
        return parse_ts(env_val)
    if con is None:
        return None
    return parse_ts(read_meta(con).get(META_EPOCH_KEY))


def get_start_balance(con: sqlite3.Connection | None = None) -> float | None:
    """Resolve the live starting balance from env first, then the meta table."""
    raw = os.getenv("LIVE_START_BALANCE", "").strip()
    if not raw and con is not None:
        raw = read_meta(con).get(META_BALANCE_KEY, "") or ""
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def is_live_trade(dry_run: int | None, closed_at: str | None,
                  epoch: datetime | None) -> bool:
    """True when a trades row belongs to the live book.

    A live trade has dry_run=0 and, when an epoch is configured, closed at or
    after the epoch. Unparseable closed_at values never count as live.
    """
    if dry_run is None or int(dry_run) != 0:
        return False
    if epoch is None:
        return True
    ts = parse_ts(closed_at)
    return ts is not None and ts >= epoch


def split_trades(rows: Iterable[dict], epoch: datetime | None,
                 view: str = "live") -> list[dict]:
    """Filter trade dicts (with dry_run + closed_at keys) to one view."""
    want_live = view != "paper"
    return [r for r in rows
            if is_live_trade(r.get("dry_run"), r.get("closed_at"), epoch) == want_live]


def is_live_position(order_id: str | None) -> bool:
    """Paper orders carry a DRYRUN- order id; everything else is exchange-backed."""
    return not str(order_id or "").startswith("DRYRUN-")


def normalize_view(value: str | None) -> str:
    """Coerce a ?view= query value to 'live' (default) or 'paper'."""
    return "paper" if (value or "").strip().lower() == "paper" else "live"
