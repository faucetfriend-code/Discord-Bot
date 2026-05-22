import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
    con.execute("""
        CREATE TABLE IF NOT EXISTS seen_messages (
            msg_id TEXT PRIMARY KEY
        )
    """)
    con.commit()
    con.close()


def open_position(pos: Position) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
        INSERT INTO positions (symbol, side, entry, sl, tp, size, order_id, opened_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (pos.symbol, pos.side, pos.entry, pos.sl, pos.tp,
          pos.size, pos.order_id, pos.opened_at, pos.status))
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
        "SELECT id, symbol, side, entry, sl, tp, size, order_id, opened_at, status "
        "FROM positions WHERE status='open'"
    ).fetchall()
    con.close()
    return [
        Position(id=r[0], symbol=r[1], side=r[2], entry=r[3], sl=r[4],
                 tp=r[5], size=r[6], order_id=r[7], opened_at=r[8], status=r[9])
        for r in rows
    ]


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
