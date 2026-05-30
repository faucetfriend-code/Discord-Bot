import csv
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot.db"
CSV_PATH = Path(__file__).parent / "signals_log.csv"

# ---------------------------------------------------------------------------
# Local timezone — all log timestamps and DB entries use this offset.
# Default is America/Chicago (CDT = GMT-5 in summer, CST = GMT-6 in winter).
# Override with TZ_OFFSET_HOURS in .env  (e.g. -5 for CDT, -6 for CST, -4 for EDT).
# ---------------------------------------------------------------------------
try:
    _tz_hours = float(os.getenv("TZ_OFFSET_HOURS", "-5"))
except (TypeError, ValueError):
    _tz_hours = -5.0

LOCAL_TZ = timezone(timedelta(hours=_tz_hours))


def now_local() -> datetime:
    """Current time in the configured local timezone."""
    return datetime.now(LOCAL_TZ)


class _LocalFormatter(logging.Formatter):
    """Logging formatter that prints timestamps in LOCAL_TZ, not UTC."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=LOCAL_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("discord_bot")

# Apply local-timezone formatter to every handler on the root logger
_fmt = _LocalFormatter("%(asctime)s %(levelname)s %(message)s")
for _h in logging.root.handlers:
    _h.setFormatter(_fmt)


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
    ts = now_local().isoformat()
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
