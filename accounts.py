"""
accounts.py - virtual bot accounts, users, and subscriptions.

Presents each signal source (analyst or strategy) as its own "bot" with an
independent paper balance, and lets users register, follow bots, and run a
paper portfolio that mirrors the bots they follow.

Accounting model (R-multiples):
  Every closed engine trade is normalized to an R-multiple:
      r_mult = net_pnl / (engine_balance * RISK_PCT)
  i.e. how many units of risk the trade returned. Each virtual account then
  compounds independently:
      balance += balance * risk_pct * r_mult
  so a bot or user with a different balance or risk setting gets the same
  trade expressed proportionally. Ledger rows make every balance auditable.

Tables (in bot.db, alongside position_tracker's):
  bots          - one row per signal source: balance, risk, enabled
  bot_ledger    - per-bot applied trades (r_mult, pnl, balance_after)
  users         - auth (PBKDF2 password hash, token hash) + paper balance
  subscriptions - user_id -> bot_key follows
  user_ledger   - per-user applied trades

Auth model: register/login returns an opaque API token; only its SHA-256 is
stored. Pass the token as "Authorization: Bearer <token>".
"""

import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from logger import log, now_local

DB_PATH = Path(__file__).parent / "bot.db"

STRATEGY_SOURCES = ["RSI Extreme", "OracleAlgo"]
_PBKDF2_ITERS = 200_000


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db():
    """Create the virtual-account tables if missing. Safe on every startup."""
    con = _con()
    con.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            key           TEXT PRIMARY KEY,
            display_name  TEXT,
            kind          TEXT DEFAULT 'analyst',
            start_balance REAL DEFAULT 1000,
            balance       REAL DEFAULT 1000,
            risk_pct      REAL DEFAULT 0.01,
            enabled       INTEGER DEFAULT 1,
            created_at    TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS bot_ledger (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_key       TEXT,
            trade_id      INTEGER,
            symbol        TEXT,
            r_mult        REAL,
            pnl           REAL,
            balance_after REAL,
            ts            TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            pass_hash     TEXT NOT NULL,
            salt          TEXT NOT NULL,
            token_hash    TEXT,
            role          TEXT DEFAULT 'user',
            risk_pct      REAL DEFAULT 0.01,
            start_balance REAL DEFAULT 1000,
            balance       REAL DEFAULT 1000,
            created_at    TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id    INTEGER,
            bot_key    TEXT,
            created_at TEXT,
            UNIQUE(user_id, bot_key)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_ledger (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER,
            bot_key       TEXT,
            trade_id      INTEGER,
            symbol        TEXT,
            r_mult        REAL,
            pnl           REAL,
            balance_after REAL,
            ts            TEXT
        )
    """)
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Bots
# ---------------------------------------------------------------------------

def ensure_bots(sources: list[str]):
    """Register any missing bots for the given source names and backfill each
    new bot's ledger from its historical trades so equity curves start full."""
    start_bal = float(os.getenv("BOT_START_BALANCE", "1000"))
    risk_pct = float(os.getenv("RISK_PCT", "0.01"))
    con = _con()
    existing = {r["key"] for r in con.execute("SELECT key FROM bots")}
    created = []
    for name in sources:
        if name in existing:
            continue
        kind = "strategy" if name in STRATEGY_SOURCES else "analyst"
        con.execute(
            "INSERT INTO bots (key, display_name, kind, start_balance, balance, "
            "risk_pct, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (name, name, kind, start_bal, start_bal, risk_pct,
             now_local().isoformat()),
        )
        created.append(name)
    con.commit()
    con.close()
    for name in created:
        _backfill_bot(name)
    if created:
        log.info(f"Registered {len(created)} virtual bots: {', '.join(created)}")


def _backfill_bot(bot_key: str):
    """Replay the bot's historical closed trades into its ledger so a newly
    registered bot starts with its real track record."""
    engine_bal = float(os.getenv("ENGINE_BASELINE_BALANCE", "1000"))
    risk_pct = float(os.getenv("RISK_PCT", "0.01"))
    risk_amount = engine_bal * risk_pct
    if risk_amount <= 0:
        return
    con = _con()
    bot = con.execute("SELECT * FROM bots WHERE key=?", (bot_key,)).fetchone()
    if not bot:
        con.close()
        return
    trades = con.execute(
        "SELECT id, symbol, net_pnl, closed_at FROM trades "
        "WHERE analyst=? ORDER BY closed_at", (bot_key,)
    ).fetchall()
    balance = bot["start_balance"]
    for t in trades:
        r_mult = (t["net_pnl"] or 0.0) / risk_amount
        pnl = balance * bot["risk_pct"] * r_mult
        balance += pnl
        con.execute(
            "INSERT INTO bot_ledger (bot_key, trade_id, symbol, r_mult, pnl, "
            "balance_after, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bot_key, t["id"], t["symbol"], round(r_mult, 6), round(pnl, 6),
             round(balance, 6), t["closed_at"]),
        )
    con.execute("UPDATE bots SET balance=? WHERE key=?",
                (round(balance, 6), bot_key))
    con.commit()
    con.close()


def settle_trade(analyst: str, net_pnl: float, risk_amount: float,
                 trade_id: Optional[int] = None, symbol: str = ""):
    """Apply one closed engine trade to the owning bot's virtual balance and to
    every subscribed user's paper balance. Called from bot._settle_position."""
    if risk_amount <= 0:
        return
    r_mult = net_pnl / risk_amount
    ts = now_local().isoformat()
    con = _con()

    bot = con.execute("SELECT * FROM bots WHERE key=?", (analyst,)).fetchone()
    if bot is None:
        con.close()
        ensure_bots([analyst])  # auto-register and backfill (includes this trade)
        con = _con()
        bot = con.execute("SELECT * FROM bots WHERE key=?", (analyst,)).fetchone()
        if bot is None:
            con.close()
            return
    else:
        pnl = bot["balance"] * bot["risk_pct"] * r_mult
        new_bal = bot["balance"] + pnl
        con.execute("UPDATE bots SET balance=? WHERE key=?",
                    (round(new_bal, 6), analyst))
        con.execute(
            "INSERT INTO bot_ledger (bot_key, trade_id, symbol, r_mult, pnl, "
            "balance_after, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (analyst, trade_id, symbol, round(r_mult, 6), round(pnl, 6),
             round(new_bal, 6), ts),
        )

    subs = con.execute(
        "SELECT u.id, u.balance, u.risk_pct FROM users u "
        "JOIN subscriptions s ON s.user_id = u.id WHERE s.bot_key=?",
        (analyst,),
    ).fetchall()
    for u in subs:
        pnl = u["balance"] * u["risk_pct"] * r_mult
        new_bal = u["balance"] + pnl
        con.execute("UPDATE users SET balance=? WHERE id=?",
                    (round(new_bal, 6), u["id"]))
        con.execute(
            "INSERT INTO user_ledger (user_id, bot_key, trade_id, symbol, "
            "r_mult, pnl, balance_after, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (u["id"], analyst, trade_id, symbol, round(r_mult, 6),
             round(pnl, 6), round(new_bal, 6), ts),
        )
    con.commit()
    con.close()
    if subs:
        log.info(f"[{analyst}] settled r={r_mult:+.2f} to bot + {len(subs)} subscriber(s)")


def get_bots() -> list[dict]:
    """All bots with balance, return %, win/loss stats and last activity."""
    con = _con()
    bots = [dict(r) for r in con.execute("SELECT * FROM bots ORDER BY balance DESC")]
    for b in bots:
        stats = con.execute(
            "SELECT COUNT(*) n, SUM(won) w, MAX(closed_at) last_close, "
            "SUM(net_pnl) pnl FROM trades WHERE analyst=?", (b["key"],)
        ).fetchone()
        n = stats["n"] or 0
        w = stats["w"] or 0
        b["trades"] = n
        b["wins"] = w
        b["losses"] = n - w
        b["win_rate"] = round(w / n * 100) if n else None
        b["last_trade_at"] = stats["last_close"]
        b["return_pct"] = round(
            (b["balance"] - b["start_balance"]) / b["start_balance"] * 100, 2
        ) if b["start_balance"] else 0.0
        b["open_positions"] = con.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open' AND analyst=?",
            (b["key"],),
        ).fetchone()[0]
    con.close()
    return bots


def get_bot_detail(bot_key: str, curve_limit: int = 200) -> Optional[dict]:
    """One bot with its equity curve, recent ledger and open positions."""
    bots = [b for b in get_bots() if b["key"] == bot_key]
    if not bots:
        return None
    bot = bots[0]
    con = _con()
    ledger = [dict(r) for r in con.execute(
        "SELECT * FROM bot_ledger WHERE bot_key=? ORDER BY id DESC LIMIT ?",
        (bot_key, curve_limit),
    )]
    bot["ledger"] = ledger
    curve = [bot["start_balance"]] + [
        row["balance_after"] for row in reversed(ledger)
    ]
    bot["equity_curve"] = curve
    bot["positions"] = [dict(r) for r in con.execute(
        "SELECT symbol, side, entry, sl, tp, size, opened_at, last_price, "
        "unrealized_pnl, leverage FROM positions "
        "WHERE status='open' AND analyst=?", (bot_key,),
    )]
    con.close()
    return bot


# ---------------------------------------------------------------------------
# Users and auth
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITERS
    ).hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register_user(username: str, password: str) -> tuple[Optional[str], str]:
    """Create a user; returns (token, error). Token is shown once."""
    username = (username or "").strip()
    if not (3 <= len(username) <= 32) or not username.replace("_", "").isalnum():
        return None, "Username must be 3-32 chars, letters/digits/underscore"
    if len(password or "") < 8:
        return None, "Password must be at least 8 characters"
    salt = secrets.token_hex(16)
    token = secrets.token_urlsafe(32)
    con = _con()
    try:
        con.execute(
            "INSERT INTO users (username, pass_hash, salt, token_hash, "
            "risk_pct, start_balance, balance, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (username, _hash_password(password, salt), salt, _hash_token(token),
             float(os.getenv("RISK_PCT", "0.01")),
             float(os.getenv("USER_START_BALANCE", "1000")),
             float(os.getenv("USER_START_BALANCE", "1000")),
             now_local().isoformat()),
        )
        con.commit()
    except sqlite3.IntegrityError:
        return None, "Username already taken"
    finally:
        con.close()
    return token, ""


def login_user(username: str, password: str) -> tuple[Optional[str], str]:
    """Verify credentials and rotate the API token; returns (token, error)."""
    con = _con()
    row = con.execute("SELECT * FROM users WHERE username=?",
                      ((username or "").strip(),)).fetchone()
    if not row or not secrets.compare_digest(
            row["pass_hash"], _hash_password(password or "", row["salt"])):
        con.close()
        return None, "Invalid username or password"
    token = secrets.token_urlsafe(32)
    con.execute("UPDATE users SET token_hash=? WHERE id=?",
                (_hash_token(token), row["id"]))
    con.commit()
    con.close()
    return token, ""


def get_user_by_token(token: str) -> Optional[dict]:
    if not token:
        return None
    con = _con()
    row = con.execute("SELECT * FROM users WHERE token_hash=?",
                      (_hash_token(token),)).fetchone()
    con.close()
    if not row:
        return None
    user = dict(row)
    user.pop("pass_hash", None)
    user.pop("salt", None)
    user.pop("token_hash", None)
    return user


# ---------------------------------------------------------------------------
# Subscriptions and portfolio
# ---------------------------------------------------------------------------

def follow(user_id: int, bot_key: str) -> tuple[bool, str]:
    con = _con()
    bot = con.execute("SELECT key FROM bots WHERE key=?", (bot_key,)).fetchone()
    if not bot:
        con.close()
        return False, f"Unknown bot: {bot_key}"
    con.execute(
        "INSERT OR IGNORE INTO subscriptions (user_id, bot_key, created_at) "
        "VALUES (?, ?, ?)", (user_id, bot_key, now_local().isoformat()),
    )
    con.commit()
    con.close()
    return True, ""


def unfollow(user_id: int, bot_key: str):
    con = _con()
    con.execute("DELETE FROM subscriptions WHERE user_id=? AND bot_key=?",
                (user_id, bot_key))
    con.commit()
    con.close()


def get_subscriptions(user_id: int) -> list[str]:
    con = _con()
    rows = con.execute(
        "SELECT bot_key FROM subscriptions WHERE user_id=? ORDER BY created_at",
        (user_id,),
    ).fetchall()
    con.close()
    return [r["bot_key"] for r in rows]


def get_portfolio(user_id: int, curve_limit: int = 200) -> dict:
    """User paper balance, equity curve, recent ledger, followed bots and the
    open engine positions belonging to those bots."""
    con = _con()
    user = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        con.close()
        return {}
    subs = get_subscriptions(user_id)
    ledger = [dict(r) for r in con.execute(
        "SELECT * FROM user_ledger WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, curve_limit),
    )]
    curve = [user["start_balance"]] + [
        row["balance_after"] for row in reversed(ledger)
    ]
    positions = []
    if subs:
        marks = ",".join("?" * len(subs))
        positions = [dict(r) for r in con.execute(
            f"SELECT symbol, side, entry, sl, tp, size, opened_at, last_price, "
            f"unrealized_pnl, leverage, analyst FROM positions "
            f"WHERE status='open' AND analyst IN ({marks})", subs,
        )]
    con.close()
    return {
        "balance": user["balance"],
        "start_balance": user["start_balance"],
        "risk_pct": user["risk_pct"],
        "return_pct": round(
            (user["balance"] - user["start_balance"]) / user["start_balance"] * 100, 2
        ) if user["start_balance"] else 0.0,
        "following": subs,
        "equity_curve": curve,
        "ledger": ledger,
        "open_positions": positions,
    }
