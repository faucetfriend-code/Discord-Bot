import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from logger import now_local

DB_PATH = Path(__file__).parent / "bot.db"


@dataclass
class Position:
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
    id: Optional[int] = None


def init_db():
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
    # Migration: add analyst column to existing DBs that predate it.
    cols = [r[1] for r in con.execute("PRAGMA table_info(positions)").fetchall()]
    if "analyst" not in cols:
        con.execute("ALTER TABLE positions ADD COLUMN analyst TEXT DEFAULT ''")
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
    con.commit()
    con.close()


def open_position(pos: Position) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
        INSERT INTO positions (symbol, side, entry, sl, tp, size, order_id, opened_at, status, analyst)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (pos.symbol, pos.side, pos.entry, pos.sl, pos.tp,
          pos.size, pos.order_id, pos.opened_at, pos.status, pos.analyst))
    row_id = cur.lastrowid
    con.commit()
    con.close()
    return row_id


def close_position(order_id: str):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE positions SET status='closed' WHERE order_id=?", (order_id,))
    con.commit()
    con.close()


def get_open_positions() -> list[Position]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, symbol, side, entry, sl, tp, size, order_id, opened_at, status, analyst "
        "FROM positions WHERE status='open'"
    ).fetchall()
    con.close()
    return [
        Position(id=r[0], symbol=r[1], side=r[2], entry=r[3], sl=r[4],
                 tp=r[5], size=r[6], order_id=r[7], opened_at=r[8], status=r[9],
                 analyst=r[10] or "")
        for r in rows
    ]


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


def find_open_by_symbol(symbol: str) -> Optional["Position"]:
    """Return the first open position matching the symbol, or None."""
    for pos in get_open_positions():
        if pos.symbol == symbol:
            return pos
    return None


def mark_seen(msg_id: str):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR IGNORE INTO seen_messages (msg_id) VALUES (?)", (msg_id,))
    con.commit()
    con.close()


def get_seen_ids() -> set[str]:
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
    """Return every analyst's leverage and win/loss record (for the dashboard)."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT analyst, leverage, wins, losses, updated_at FROM analyst_stats "
        "ORDER BY leverage DESC, analyst"
    ).fetchall()
    con.close()
    return [
        {"analyst": r[0], "leverage": r[1], "wins": r[2], "losses": r[3], "updated_at": r[4]}
        for r in rows
    ]
