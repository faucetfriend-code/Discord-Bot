import csv
import logging
import logging.handlers
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Force UTF-8 on the console streams so log lines containing em-dashes / arrows
# ("→", "—") don't garble (or raise UnicodeEncodeError) on a Windows cp1252
# console. The file handler is already opened utf-8. errors="replace" guarantees
# logging never crashes on an unencodable glyph.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

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


# Rotating file handler so bot.log never grows unbounded over long unattended
# runs. Defaults: 10 MB per file, 5 backups (bot.log + bot.log.1..5 = ~60 MB cap).
# Override via LOG_MAX_MB / LOG_BACKUP_COUNT in .env.
try:
    _log_max_bytes = int(float(os.getenv("LOG_MAX_MB", "10")) * 1024 * 1024)
except (TypeError, ValueError):
    _log_max_bytes = 10 * 1024 * 1024
try:
    _log_backup_count = int(os.getenv("LOG_BACKUP_COUNT", "5"))
except (TypeError, ValueError):
    _log_backup_count = 5

# Root log level from LOG_LEVEL (DEBUG/INFO/WARNING/ERROR), default INFO.
_log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").strip().upper(), None)
if not isinstance(_log_level, int):
    _log_level = logging.INFO

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            Path(__file__).parent / "bot.log",
            maxBytes=_log_max_bytes,
            backupCount=_log_backup_count,
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("discord_bot")

# Apply local-timezone formatter to every handler on the root logger
_fmt = _LocalFormatter("%(asctime)s %(levelname)s %(message)s")
for _h in logging.root.handlers:
    _h.setFormatter(_fmt)


# Column order for signals_log.csv. `analyst` is the RESOLVED attribution key;
# `author` is the raw Discord author string it was resolved from. Keeping both
# matters: attribution overwrites the author, so without this column the
# original poster is unrecoverable and matching changes cannot be replayed
# against real data.
_SIGNAL_FIELDS = ["ts", "analyst", "author", "raw_text", "symbol", "side",
                  "entry", "sl", "tp", "parsed", "outcome", "order_id"]


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            analyst     TEXT,
            author      TEXT,
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
    # `author` was added after the table shipped; existing DBs need it grafted
    # on. Nullable, so historic rows simply carry NULL.
    cols = {r[1] for r in con.execute("PRAGMA table_info(signals)")}
    if "author" not in cols:
        con.execute("ALTER TABLE signals ADD COLUMN author TEXT")
    con.commit()
    con.close()
    _migrate_signals_header()


def _migrate_signals_header():
    """If signals_log.csv predates a column addition, rewrite it in the new
    layout (missing columns blank) so old and new rows stay aligned."""
    if not CSV_PATH.exists():
        return
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == _SIGNAL_FIELDS:
            return
        old_rows = list(reader)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_SIGNAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in old_rows:
            writer.writerow({k: r.get(k, "") for k in _SIGNAL_FIELDS})


def log_signal(analyst, raw_text, signal=None, outcome="seen", order_id=None,
               author=None):
    ts = now_local().isoformat()
    # signal.analyst is the RAW Discord author (signal_parser builds it from
    # msg["author"]), so it is the correct default whenever the caller has a
    # signal. Callers without one (chatter, POI watches) pass author=.
    if author is None:
        author = getattr(signal, "analyst", "") if signal is not None else ""
    row = {
        "ts": ts,
        "analyst": analyst,
        "author": author or "",
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
        INSERT INTO signals (ts, analyst, author, raw_text, symbol, side, entry, sl, tp, parsed, outcome, order_id)
        VALUES (:ts, :analyst, :author, :raw_text, :symbol, :side, :entry, :sl, :tp, :parsed, :outcome, :order_id)
    """, row)
    con.commit()
    con.close()

    write_header = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_SIGNAL_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ---------------------------------------------------------------------------
# Strategy shadow log — the "measure-then-tune" record for RSI / OracleAlgo.
# Every RSI/Oracle signal writes one row capturing the data the alert carried
# (RSI value, 24h change, signal type) plus what each *proposed* filter WOULD
# have decided — even while the filters are disabled. Run dry-run for a while,
# then tune thresholds from the real distribution before flipping the flags on.
# ---------------------------------------------------------------------------
SHADOW_CSV_PATH = Path(__file__).parent / "strategy_shadow.csv"

_SHADOW_FIELDS = [
    "ts", "source", "symbol", "side",
    "rsi_value", "change_24h", "oracle_type", "btc_bias", "entry_price",
    "f_strength", "f_chase", "f_regime", "confirm_would_fire",
    "confluence_count", "repeat_n", "adx_value", "adx_regime", "btc_adx_regime",
    "decision",
]


def _migrate_shadow_header():
    """If the on-disk CSV predates a column addition, rewrite it in the new
    layout (missing columns blank) so old and new rows stay aligned."""
    if not SHADOW_CSV_PATH.exists():
        return
    with open(SHADOW_CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == _SHADOW_FIELDS:
            return
        old_rows = list(reader)
    with open(SHADOW_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_SHADOW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in old_rows:
            writer.writerow({k: r.get(k, "") for k in _SHADOW_FIELDS})


def log_shadow(source, symbol, side, fields: dict):
    """Append one strategy-signal row to strategy_shadow.csv. `fields` may carry
    any subset of the shadow columns; missing keys are written blank."""
    row = {k: "" for k in _SHADOW_FIELDS}
    row["ts"] = now_local().isoformat()
    row["source"] = source
    row["symbol"] = symbol
    row["side"] = side
    for k, v in fields.items():
        if k in row and v is not None:
            row[k] = v

    _migrate_shadow_header()
    write_header = not SHADOW_CSV_PATH.exists()
    with open(SHADOW_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_SHADOW_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
