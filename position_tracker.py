import json
import sqlite3
from dataclasses import dataclass, asdict, field
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
    tps: list = field(default_factory=list)  # full TP ladder (nearest target first)
    tps_hit: int = 0         # how many ladder targets have been taken so far
    orig_size: float = 0.0   # original size at open (for computing scale-out chunks)
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
    # Persisted strategy state (e.g. OracleAlgo's current BTC 4H bias).
    con.execute("""
        CREATE TABLE IF NOT EXISTS strategy_state (
            k TEXT PRIMARY KEY,
            v TEXT
        )
    """)
    con.commit()
    con.close()


def set_state(key: str, value: str):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO strategy_state (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, str(value)),
    )
    con.commit()
    con.close()


def get_state(key: str, default=None):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT v FROM strategy_state WHERE k=?", (key,)).fetchone()
    con.close()
    return row[0] if row else default


def open_position(pos: Position) -> int:
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
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE positions SET status='closed' WHERE order_id=?", (order_id,))
    con.commit()
    con.close()


def _parse_tps(raw) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [float(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def get_open_positions() -> list[Position]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, symbol, side, entry, sl, tp, size, order_id, opened_at, status, "
        "analyst, tps, tps_hit, orig_size "
        "FROM positions WHERE status='open'"
    ).fetchall()
    con.close()
    return [
        Position(id=r[0], symbol=r[1], side=r[2], entry=r[3], sl=r[4],
                 tp=r[5], size=r[6], order_id=r[7], opened_at=r[8], status=r[9],
                 analyst=r[10] or "", tps=_parse_tps(r[11]),
                 tps_hit=r[12] or 0, orig_size=r[13] or r[6])
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
