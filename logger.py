import csv
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot.db"
CSV_PATH = Path(__file__).parent / "signals_log.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("discord_bot")


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            analyst     TEXT,
            raw_text    TEXT,
            symbol      TEXT,
            side        TEXT,
            entry       REAL,
            sl          REAL,
            tp          REAL,
            parsed      INTEGER,
            outcome     TEXT,
            order_id    TEXT
        )
    """)
    con.commit()
    con.close()


def log_signal(analyst, raw_text, signal=None, outcome="seen", order_id=None):
    ts = datetime.now(timezone.utc).isoformat()
    row = {
        "ts": ts,
        "analyst": analyst,
        "raw_text": raw_text[:500],
        "symbol": signal.symbol if signal else "",
        "side": signal.side if signal else "",
        "entry": signal.entry if signal else None,
        "sl": signal.sl if signal else None,
        "tp": signal.tp if signal else None,
        "parsed": 1 if signal else 0,
        "outcome": outcome,
        "order_id": order_id or "",
    }

    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO signals (ts, analyst, raw_text, symbol, side, entry, sl, tp, parsed, outcome, order_id)
        VALUES (:ts, :analyst, :raw_text, :symbol, :side, :entry, :sl, :tp, :parsed, :outcome, :order_id)
    """, row)
    con.commit()
    con.close()

    write_header = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)
