"""
position_tracker.py — all SQLite persistence for the bot (the only module that
touches `bot.db` for state).

Stores three things, each in its own table:
  • positions      — open/closed trades (entry, SL, TP ladder, scale-out progress,
                     mark-to-market PnL, the analyst it's attributed to)
  • analyst_stats  — per-analyst/strategy adaptive leverage, win/loss tally, realised PnL
  • seen_messages  — Discord message IDs already processed (dedupe across restarts)
  • strategy_state — small key→value store (e.g. OracleAlgo's current BTC bias)

Public interface (everything below is safe to call from anywhere):
  init_db(), open_position(), close_position(), apply_partial(), get_open_positions(),
  find_open_by_symbol(), update_position_sl_tp(), update_unrealized(),
  get_analyst_leverage(), record_outcome(), add_realized_pnl(), get_all_analyst_stats(),
  mark_seen(), get_seen_ids(), set_state(), get_state()

Reusable standalone: yes. Only dependency is the stdlib `sqlite3` plus
`logger.now_local` for timestamps. Drop this file into any project that needs
simple trade/position persistence; swap the `now_local` import for `datetime.now`
if you don't want the logger.
"""

import json
import sqlite3
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from logger import now_local

DB_PATH = Path(__file__).parent / "bot.db"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """One trade the bot is tracking. `tps` is the take-profit ladder (nearest
    target first); `tps_hit` counts how many rungs have been scaled out so far;
    `analyst` is the canonical source key used for performance attribution."""
    symbol: str
    side: str
    entry: float
    sl: float
    tp: float
    size: float
    order_id: str
    opened_at: str
    status: str = "open"
    analyst: str = ""        # canonical analyst key, for performance attribution
    tps: list = field(default_factory=list)  # full TP ladder (nearest target first)
    tps_hit: int = 0         # how many ladder targets have been taken so far
    orig_size: float = 0.0   # original size at open (for computing scale-out chunks)
    last_price: float = 0.0  # most recent market price seen (for unrealized PnL)
    unrealized_pnl: float = 0.0  # mark-to-market PnL on the remaining size, in USDT
    id: Optional[int] = None


# ---------------------------------------------------------------------------
# Schema — create tables and run idempotent column migrations
# ---------------------------------------------------------------------------

def init_db():
    """Create all tables if missing and add any columns introduced by later
    versions (safe to call on every startup; never drops or clears data)."""
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT NOT NULL,
            side        TEXT NOT NULL,
            entry       REAL,
            sl          REAL,
            tp          REAL,
            size        REAL,
            order_id    TEXT,
            opened_at   TEXT,
            status      TEXT DEFAULT 'open'
        )
    """)
    # Migrations: add columns to existing DBs that predate them.
    cols = [r[1] for r in con.execute("PRAGMA table_info(positions)").fetchall()]
    if "analyst" not in cols:
        con.execute("ALTER TABLE positions ADD COLUMN analyst TEXT DEFAULT ''")
    if "tps" not in cols:
        con.execute("ALTER TABLE positions ADD COLUMN tps TEXT DEFAULT ''")
    if "tps_hit" not in cols:
        con.execute("ALTER TABLE positions ADD COLUMN tps_hit INTEGER DEFAULT 0")
    if "orig_size" not in cols:
        con.execute("ALTER TABLE positions ADD COLUMN orig_size REAL DEFAULT 0")
    if "last_price" not in cols:
        con.execute("ALTER TABLE positions ADD COLUMN last_price REAL DEFAULT 0")
    if "unrealized_pnl" not in cols:
        con.execute("ALTER TABLE positions ADD COLUMN unrealized_pnl REAL DEFAULT 0")
    con.execute("""
        CREATE TABLE IF NOT EXISTS seen_messages (
            msg_id TEXT PRIMARY KEY
        )
    """)
    # Adaptive per-analyst leverage, adjusted by realised trade outcomes.
    con.execute("""
        CREATE TABLE IF NOT EXISTS analyst_stats (
            analyst    TEXT PRIMARY KEY,
            leverage   INTEGER,
            wins       INTEGER DEFAULT 0,
            losses     INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    # realized_pnl: cumulative closed PnL per analyst/strategy (USDT).
    stat_cols = [r[1] for r in con.execute("PRAGMA table_info(analyst_stats)").fetchall()]
    if stat_cols and "realized_pnl" not in stat_cols:
        con.execute("ALTER TABLE analyst_stats ADD COLUMN realized_pnl REAL DEFAULT 0")
    # Persisted strategy state (e.g. OracleAlgo's current BTC 4H bias).
    con.execute("""
        CREATE TABLE IF NOT EXISTS strategy_state (
            k TEXT PRIMARY KEY,
            v TEXT
        )
    """)
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Strategy state (small key→value store)
# ---------------------------------------------------------------------------

def set_state(key: str, value: str):
    """Persist a strategy value (e.g. the OracleAlgo BTC bias), upserting by key."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO strategy_state (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, str(value)),
    )
    con.commit()
    con.close()


def get_state(key: str, default=None):
    """Return a persisted strategy value, or `default` if the key isn't set."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT v FROM strategy_state WHERE k=?", (key,)).fetchone()
    con.close()
    return row[0] if row else default


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def open_position(pos: Position) -> int:
    """Insert a new open position; returns its row id."""
    orig = pos.orig_size or pos.size
    tps_json = json.dumps(pos.tps or [])
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
        INSERT INTO positions (symbol, side, entry, sl, tp, size, order_id, opened_at,
                               status, analyst, tps, tps_hit, orig_size)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (pos.symbol, pos.side, pos.entry, pos.sl, pos.tp,
          pos.size, pos.order_id, pos.opened_at, pos.status, pos.analyst,
          tps_json, pos.tps_hit, orig))
    row_id = cur.lastrowid
    con.commit()
    con.close()
    return row_id


def apply_partial(order_id: str, remaining_size: float, new_sl: float, tps_hit: int):
    """Record a scale-out: update remaining size, ratcheted SL, and TPs-hit count."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE positions SET size=?, sl=?, tps_hit=? WHERE order_id=?",
        (remaining_size, new_sl, tps_hit, order_id),
    )
    con.commit()
    con.close()


def close_position(order_id: str):
    """Mark a position closed (status='closed') by its order id."""
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE positions SET status='closed' WHERE order_id=?", (order_id,))
    con.commit()
    con.close()


def _parse_tps(raw) -> list:
    """Decode the JSON-encoded TP ladder stored in the DB into a list of floats."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [float(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def get_open_positions() -> list[Position]:
    """Return every open position as a list of Position objects."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, symbol, side, entry, sl, tp, size, order_id, opened_at, status, "
        "analyst, tps, tps_hit, orig_size, last_price, unrealized_pnl "
        "FROM positions WHERE status='open'"
    ).fetchall()
    con.close()
    return [
        Position(id=r[0], symbol=r[1], side=r[2], entry=r[3], sl=r[4],
                 tp=r[5], size=r[6], order_id=r[7], opened_at=r[8], status=r[9],
                 analyst=r[10] or "", tps=_parse_tps(r[11]),
                 tps_hit=r[12] or 0, orig_size=r[13] or r[6],
                 last_price=r[14] or 0.0, unrealized_pnl=r[15] or 0.0)
        for r in rows
    ]


def update_position_entry(order_id: str, new_entry: float):
    """Shift a position's entry basis (used when a DCA/averaging analyst posts a
    new average entry). PnL and win/loss attribution are measured from here on."""
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE positions SET entry=? WHERE order_id=?", (new_entry, order_id))
    con.commit()
    con.close()


def update_unrealized(order_id: str, last_price: float, unrealized_pnl: float):
    """Store the latest mark-to-market price and unrealized PnL for an open position."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE positions SET last_price=?, unrealized_pnl=? WHERE order_id=?",
        (last_price, unrealized_pnl, order_id),
    )
    con.commit()
    con.close()


def add_realized_pnl(analyst: str, amount: float):
    """Accumulate realized (closed) PnL onto an analyst/strategy's running total."""
    analyst = analyst or "unknown"
    con = sqlite3.connect(DB_PATH)
    # Ensure the row exists (a close can precede any leverage lookup in edge cases).
    con.execute(
        "INSERT INTO analyst_stats (analyst, leverage, wins, losses, realized_pnl, updated_at) "
        "VALUES (?, 0, 0, 0, 0, ?) ON CONFLICT(analyst) DO NOTHING",
        (analyst, now_local().isoformat()),
    )
    con.execute(
        "UPDATE analyst_stats SET realized_pnl = COALESCE(realized_pnl, 0) + ? WHERE analyst=?",
        (amount, analyst),
    )
    con.commit()
    con.close()


def update_position_sl_tp(order_id: str,
                          new_sl: Optional[float],
                          new_tp: Optional[float]):
    """Update SL and/or TP on an open position in the DB."""
    fields, vals = [], []
    if new_sl is not None:
        fields.append("sl=?")
        vals.append(new_sl)
    if new_tp is not None:
        fields.append("tp=?")
        vals.append(new_tp)
    if not fields:
        return
    vals.append(order_id)
    con = sqlite3.connect(DB_PATH)
    con.execute(f"UPDATE positions SET {', '.join(fields)} WHERE order_id=?", vals)
    con.commit()
    con.close()


def find_open_by_symbol(symbol: str, analyst: Optional[str] = None) -> Optional["Position"]:
    """Return the first open position for `symbol`, or None. If `analyst` is given,
    only a position from that source/analyst matches (used by hedge mode so each
    source manages its own position on a symbol independently)."""
    for pos in get_open_positions():
        if pos.symbol == symbol and (analyst is None or pos.analyst == analyst):
            return pos
    return None


# ---------------------------------------------------------------------------
# Seen-message dedupe (so a restart never re-processes old notifications)
# ---------------------------------------------------------------------------

def mark_seen(msg_id: str):
    """Record a Discord message ID as processed (idempotent)."""
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR IGNORE INTO seen_messages (msg_id) VALUES (?)", (msg_id,))
    con.commit()
    con.close()


def get_seen_ids() -> set[str]:
    """Return the set of all message IDs already processed."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT msg_id FROM seen_messages").fetchall()
    con.close()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Adaptive per-analyst leverage
# ---------------------------------------------------------------------------

def get_analyst_leverage(analyst: str, start: int, lo: int, hi: int) -> int:
    """
    Return the current leverage for an analyst, clamped to [lo, hi].
    First-seen analysts are initialised at `start` (the middle of the band).
    """
    analyst = analyst or "unknown"
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT leverage FROM analyst_stats WHERE analyst=?", (analyst,)).fetchone()
    if row is None:
        lev = int(start)
        con.execute(
            "INSERT INTO analyst_stats (analyst, leverage, wins, losses, updated_at) "
            "VALUES (?, ?, 0, 0, ?)",
            (analyst, lev, now_local().isoformat()),
        )
        con.commit()
    else:
        lev = int(row[0])
    con.close()
    return max(int(lo), min(int(hi), lev))


def record_outcome(analyst: str, won: bool, step: int, lo: int, hi: int) -> int:
    """
    Adjust an analyst's leverage after a resolved trade: +step on a win,
    -step on a loss, clamped to [lo, hi]. Increments the win/loss tally.
    Returns the new leverage.
    """
    analyst = analyst or "unknown"
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT leverage, wins, losses FROM analyst_stats WHERE analyst=?", (analyst,)
    ).fetchone()
    cur_lev = int(row[0]) if row else int((lo + hi) // 2)
    wins = (row[1] if row else 0) + (1 if won else 0)
    losses = (row[2] if row else 0) + (0 if won else 1)
    new_lev = cur_lev + step if won else cur_lev - step
    new_lev = max(int(lo), min(int(hi), new_lev))
    con.execute(
        "INSERT INTO analyst_stats (analyst, leverage, wins, losses, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(analyst) DO UPDATE SET leverage=excluded.leverage, "
        "wins=excluded.wins, losses=excluded.losses, updated_at=excluded.updated_at",
        (analyst, new_lev, wins, losses, now_local().isoformat()),
    )
    con.commit()
    con.close()
    return new_lev


def get_all_analyst_stats() -> list[dict]:
    """Return every analyst's leverage, win/loss record and realized PnL (dashboard)."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT analyst, leverage, wins, losses, "
        "COALESCE(realized_pnl, 0), updated_at FROM analyst_stats "
        "ORDER BY realized_pnl DESC, leverage DESC, analyst"
    ).fetchall()
    con.close()
    return [
        {"analyst": r[0], "leverage": r[1], "wins": r[2], "losses": r[3],
         "realized_pnl": r[4], "updated_at": r[5]}
        for r in rows
    ]
