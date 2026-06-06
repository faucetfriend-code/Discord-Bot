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
    soft_stop: bool = False  # SL triggers only on a candle CLOSE beyond it, not a wick
    realized_pnl: float = 0.0  # net PnL banked on already-closed slices of this position
    fees_accum: float = 0.0  # trading fees banked on those closed slices
    leverage: int = 0        # leverage applied at open (for the trade record)
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
    if "soft_stop" not in cols:
        con.execute("ALTER TABLE positions ADD COLUMN soft_stop INTEGER DEFAULT 0")
    # realized_pnl: net PnL accumulated across this position's closed slices (USDT).
    # fees_accum:   trading fees accumulated across those slices (USDT).
    if "realized_pnl" not in cols:
        con.execute("ALTER TABLE positions ADD COLUMN realized_pnl REAL DEFAULT 0")
    if "fees_accum" not in cols:
        con.execute("ALTER TABLE positions ADD COLUMN fees_accum REAL DEFAULT 0")
    if "leverage" not in cols:
        con.execute("ALTER TABLE positions ADD COLUMN leverage INTEGER DEFAULT 0")
    con.execute("""
        CREATE TABLE IF NOT EXISTS seen_messages (
            msg_id TEXT PRIMARY KEY
        )
    """)
    # One row per fully-closed position — the durable trade blotter / equity-curve
    # source. gross_pnl = price PnL before costs; net_pnl = gross − fees − funding.
    con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT,
            side        TEXT,
            analyst     TEXT,
            entry       REAL,
            exit_price  REAL,
            size        REAL,
            leverage    INTEGER,
            soft_stop   INTEGER DEFAULT 0,
            opened_at   TEXT,
            closed_at   TEXT,
            duration_s  INTEGER,
            reason      TEXT,
            won         INTEGER,
            gross_pnl   REAL,
            fees        REAL,
            funding     REAL,
            net_pnl     REAL,
            dry_run     INTEGER DEFAULT 1
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
    # POI watchlist: an analyst flags a future area of interest. We monitor price
    # to the level, "arm" when it's reached, then take the analyst's confirming
    # entry. status: watching → armed → triggered/expired.
    con.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol     TEXT,
            analyst    TEXT,
            direction  TEXT,
            level      REAL,
            status     TEXT DEFAULT 'watching',
            note       TEXT,
            created_at TEXT,
            armed_at   TEXT
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
                               status, analyst, tps, tps_hit, orig_size, soft_stop, leverage)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (pos.symbol, pos.side, pos.entry, pos.sl, pos.tp,
          pos.size, pos.order_id, pos.opened_at, pos.status, pos.analyst,
          tps_json, pos.tps_hit, orig, 1 if pos.soft_stop else 0, pos.leverage or 0))
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


def accumulate_costs(order_id: str, pnl_delta: float, fee_delta: float):
    """Add a just-closed slice's net PnL and fee onto a position's running totals,
    so the final trade record reflects every scale-out leg, not just the last."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE positions SET realized_pnl = COALESCE(realized_pnl,0) + ?, "
        "fees_accum = COALESCE(fees_accum,0) + ? WHERE order_id=?",
        (pnl_delta, fee_delta, order_id),
    )
    con.commit()
    con.close()


def close_position(order_id: str):
    """Mark a position closed (status='closed') by its order id."""
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE positions SET status='closed' WHERE order_id=?", (order_id,))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Trade blotter — one durable row per fully-closed position
# ---------------------------------------------------------------------------

def record_trade(*, symbol, side, analyst, entry, exit_price, size, leverage,
                 soft_stop, opened_at, closed_at, duration_s, reason, won,
                 gross_pnl, fees, funding, net_pnl, dry_run) -> int:
    """Insert one closed-trade row; returns its id."""
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
        INSERT INTO trades (symbol, side, analyst, entry, exit_price, size, leverage,
                            soft_stop, opened_at, closed_at, duration_s, reason, won,
                            gross_pnl, fees, funding, net_pnl, dry_run)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (symbol, side, analyst, entry, exit_price, size, int(leverage or 0),
          1 if soft_stop else 0, opened_at, closed_at, duration_s, reason,
          1 if won else 0, gross_pnl, fees, funding, net_pnl, 1 if dry_run else 0))
    row_id = cur.lastrowid
    con.commit()
    con.close()
    return row_id


def get_trades(limit: int = 50) -> list[dict]:
    """Most recent closed trades, newest first (for the dashboard blotter)."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT symbol, side, analyst, entry, exit_price, size, leverage, soft_stop, "
        "opened_at, closed_at, duration_s, reason, won, gross_pnl, fees, funding, net_pnl "
        "FROM trades ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    con.close()
    keys = ("symbol", "side", "analyst", "entry", "exit_price", "size", "leverage",
            "soft_stop", "opened_at", "closed_at", "duration_s", "reason", "won",
            "gross_pnl", "fees", "funding", "net_pnl")
    return [dict(zip(keys, r)) for r in rows]


def get_trade_stats() -> list[dict]:
    """Closed trades oldest→newest with a running cumulative net PnL — the
    equity-curve series for the dashboard."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT closed_at, net_pnl FROM trades ORDER BY id ASC"
    ).fetchall()
    con.close()
    out, cum = [], 0.0
    for closed_at, net in rows:
        cum += (net or 0.0)
        out.append({"closed_at": closed_at, "net_pnl": net or 0.0, "cumulative": cum})
    return out


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
        "analyst, tps, tps_hit, orig_size, last_price, unrealized_pnl, soft_stop, "
        "realized_pnl, fees_accum, leverage "
        "FROM positions WHERE status='open'"
    ).fetchall()
    con.close()
    return [
        Position(id=r[0], symbol=r[1], side=r[2], entry=r[3], sl=r[4],
                 tp=r[5], size=r[6], order_id=r[7], opened_at=r[8], status=r[9],
                 analyst=r[10] or "", tps=_parse_tps(r[11]),
                 tps_hit=r[12] or 0, orig_size=r[13] or r[6],
                 last_price=r[14] or 0.0, unrealized_pnl=r[15] or 0.0,
                 soft_stop=bool(r[16]) if len(r) > 16 else False,
                 realized_pnl=r[17] or 0.0, fees_accum=r[18] or 0.0,
                 leverage=r[19] or 0)
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


def prune_seen(keep: int = 5000):
    """Cap the seen-messages table at the `keep` most recent rows so it can't grow
    without bound. (rowid is monotonic, so highest rowids = most recently inserted.)"""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "DELETE FROM seen_messages WHERE rowid NOT IN "
        "(SELECT rowid FROM seen_messages ORDER BY rowid DESC LIMIT ?)",
        (keep,),
    )
    con.commit()
    con.close()


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


# ---------------------------------------------------------------------------
# POI watchlist
# ---------------------------------------------------------------------------

@dataclass
class Watch:
    symbol: str
    analyst: str
    direction: str       # "buy" / "sell" / "" (lean)
    level: float         # POI trigger price (0 = unknown / chart not read)
    status: str = "watching"
    note: str = ""
    created_at: str = ""
    armed_at: str = ""
    id: Optional[int] = None


def add_watch(w: Watch) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "INSERT INTO watches (symbol, analyst, direction, level, status, note, created_at) "
        "VALUES (?, ?, ?, ?, 'watching', ?, ?)",
        (w.symbol, w.analyst, w.direction, w.level, w.note, now_local().isoformat()),
    )
    row_id = cur.lastrowid
    con.commit()
    con.close()
    return row_id


def get_active_watches() -> list[Watch]:
    """Watches still in play (watching or armed)."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, symbol, analyst, direction, level, status, note, created_at, armed_at "
        "FROM watches WHERE status IN ('watching','armed') ORDER BY id DESC"
    ).fetchall()
    con.close()
    return [Watch(id=r[0], symbol=r[1], analyst=r[2], direction=r[3], level=r[4],
                  status=r[5], note=r[6] or "", created_at=r[7] or "", armed_at=r[8] or "")
            for r in rows]


def set_watch_status(watch_id: int, status: str):
    con = sqlite3.connect(DB_PATH)
    armed = now_local().isoformat() if status == "armed" else None
    if armed:
        con.execute("UPDATE watches SET status=?, armed_at=? WHERE id=?", (status, armed, watch_id))
    else:
        con.execute("UPDATE watches SET status=? WHERE id=?", (status, watch_id))
    con.commit()
    con.close()


def find_armed_watch(symbol: str, analyst: str) -> Optional[Watch]:
    """An armed watch for this symbol+analyst whose entry can now be taken."""
    for w in get_active_watches():
        if w.status == "armed" and w.symbol == symbol and w.analyst == analyst:
            return w
    return None
