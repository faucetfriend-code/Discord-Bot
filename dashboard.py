"""
dashboard.py — the read-only web cockpit at http://localhost:5050.

Runs as a Flask app in a background daemon thread (started by bot.start()).
It reads live process metrics from a shared `bot_state` dict, and everything
else read-only from `bot.db` and `bot.log` — it never writes or trades. The page
auto-refreshes every 15s; `/api/status` returns the same data as JSON.

Layout (top to bottom): a card per signal source (roster), two status bars
(process health + PnL + strategy states), open positions with mark-to-market
PnL and scale-out progress, analyst/strategy performance, recent signals, log tail.

The `_read_*` helpers are the only DB-reading code here; each returns plain dicts
so the rendering stays trivial. All DB access is read-only (uri=…?mode=ro).
"""

import hmac
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, request,
                   send_from_directory)

import accounts  # virtual bot/user paper accounts
import live_epoch  # paper -> live cutover (epoch + start balance)

DB_PATH    = Path(__file__).parent / "bot.db"
LOG_PATH   = Path(__file__).parent / "bot.log"
# Static mobile UI bundle (Unity Oracle prototype) served at /mobile.
# Override with MOBILE_UI_DIR to point at a different build directory.
MOBILE_DIR = Path(os.getenv("MOBILE_UI_DIR", str(Path(__file__).parent / "mobile")))
LOG_LINES  = 80

app = Flask(__name__)
_state: dict = {}

# Suppress Flask/Werkzeug request logs from polluting bot.log
logging.getLogger("werkzeug").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Optional HTTP Basic Auth — gate the dashboard when it's exposed publicly
# (e.g. behind a Cloudflare tunnel). Set DASHBOARD_PASSWORD in .env to enable;
# leave it blank for no auth (local use only).
# ---------------------------------------------------------------------------

@app.before_request
def _require_auth():
    # The Oracle Aggregator JSON API (/api/oracle/*) authenticates with its own
    # bearer token (see _require_oracle_token), so skip Basic auth for those paths.
    # The /mobile prototype is intentionally open (no auth yet) — it's a static UI
    # with no secrets; revisit before wiring it to live/authenticated data.
    # /api/v1/* (user-facing bots/portfolio API) authenticates with per-user
    # bearer tokens (see _current_user), so Basic auth is skipped there too.
    # /bots and /portfolio are the user-facing pages driven by that API.
    if (request.path.startswith("/api/oracle/") or request.path.startswith("/mobile")
            or request.path.startswith("/api/v1/") or request.path == "/bots"
            or request.path == "/portfolio"):
        return
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not password:
        return  # auth disabled
    user = os.getenv("DASHBOARD_USER", "viewer").strip()
    auth = request.authorization
    ok = (auth and auth.username == user
          and hmac.compare_digest(auth.password or "", password))
    if not ok:
        return Response(
            "Authentication required", 401,
            {"WWW-Authenticate": 'Basic realm="Discord Signal Bot"'},
        )


# ---------------------------------------------------------------------------
# Oracle Aggregator JSON API — read-only feeds consumed by the
# unity-oracle-aggregator Next.js app. Gated by a shared bearer token
# (ORACLE_API_TOKEN). Fails closed: if the token isn't set, the API is off.
# ---------------------------------------------------------------------------

def _require_oracle_token() -> Response | None:
    """Return a 401/503 Response if the request is not a valid Oracle API call,
    else None to allow it. Accepts `Authorization: Bearer <token>` or `?token=`."""
    expected = os.getenv("ORACLE_API_TOKEN", "").strip()
    if not expected:
        # Fail closed — never expose trade data without an explicitly set token.
        return Response(
            json.dumps({"success": False, "error": "Oracle API disabled (ORACLE_API_TOKEN unset)"}),
            503, content_type="application/json",
        )
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else request.args.get("token", "")
    if not (token and hmac.compare_digest(token, expected)):
        return Response(
            json.dumps({"success": False, "error": "Unauthorized"}),
            401, content_type="application/json",
        )
    return None


def _oracle_json(payload: dict) -> Response:
    """Wrap a payload in the Oracle's standard envelope with a server timestamp."""
    payload.setdefault("success", True)
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    return jsonify(payload)


@app.route("/api/oracle/signals")
def api_oracle_signals():
    """Recent parsed analyst/OracleAlgo signals → powers the Oracle Alerts feed."""
    guard = _require_oracle_token()
    if guard:
        return guard
    limit = min(int(request.args.get("limit", 50) or 50), 200)
    show_all = request.args.get("all") == "1"
    return _oracle_json({"data": _read_signals(limit, show_all=show_all)})


@app.route("/api/oracle/positions")
def api_oracle_positions():
    """Open positions with mark-to-market PnL."""
    guard = _require_oracle_token()
    if guard:
        return guard
    return _oracle_json({"data": _read_positions(_view_arg(default=None))})


@app.route("/api/oracle/trades")
def api_oracle_trades():
    """Closed-trade blotter, newest first."""
    guard = _require_oracle_token()
    if guard:
        return guard
    limit = min(int(request.args.get("limit", 100) or 100), 500)
    view = _view_arg(default=None)
    return _oracle_json({"data": _read_trades(limit, view=view),
                         "equity_curve": _read_equity_curve(view=view)})


@app.route("/api/oracle/analyst-stats")
def api_oracle_analyst_stats():
    """Per-analyst win/loss tally and realized PnL (the leaderboard)."""
    guard = _require_oracle_token()
    if guard:
        return guard
    return _oracle_json({"data": _read_analyst_stats(), "roster": _read_roster()})


@app.route("/api/oracle/watchlist")
def api_oracle_watchlist():
    """Active POI watches (watching / armed)."""
    guard = _require_oracle_token()
    if guard:
        return guard
    return _oracle_json({"data": _read_watches()})


@app.route("/api/oracle/funding")
def api_oracle_funding():
    """Live BloFin funding rate(s). `?symbol=BTC-USDT` for one, or comma-separated.
    Defaults to the symbols of currently open positions if none given."""
    guard = _require_oracle_token()
    if guard:
        return guard
    raw = request.args.get("symbol", "").strip()
    if raw:
        symbols = [s.strip() for s in raw.split(",") if s.strip()]
    else:
        symbols = sorted({p["symbol"] for p in _read_positions()})
    out = []
    try:
        import blofin_client  # lazy: avoid coupling dashboard import to the exchange client
        for sym in symbols[:25]:
            try:
                rate = blofin_client.get_funding_rate(sym)
            except Exception:
                rate = None
            if rate is not None:
                out.append({"symbol": sym, "rate": rate})
    except Exception as e:
        return _oracle_json({"success": False, "error": f"funding unavailable: {e}", "data": []})
    return _oracle_json({"data": out})


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _read_log_tail() -> str:
    """Return the last LOG_LINES lines of bot.log as a single string."""
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-LOG_LINES:])
    except Exception:
        return "(log unavailable)"


# ---------------------------------------------------------------------------
# Live epoch: "live" view = dry_run=0 trades closed at/after the epoch and
# exchange-backed positions; "paper" view = everything else (the archive).
# A view of None means "no filter" (legacy behaviour for the Oracle feeds).
# ---------------------------------------------------------------------------

def _view_arg(default: str | None = "live") -> str | None:
    """Resolve ?view=live|paper; `default` applies when the param is absent."""
    raw = request.args.get("view")
    if raw is None or not raw.strip():
        return default
    return live_epoch.normalize_view(raw)


def _epoch_info() -> dict:
    """Epoch timestamp + live start balance (env first, then bot.db meta)."""
    con = None
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        epoch = live_epoch.get_epoch(con)
        start = live_epoch.get_start_balance(con)
    except Exception:
        epoch, start = live_epoch.get_epoch(None), live_epoch.get_start_balance(None)
    finally:
        if con is not None:
            con.close()
    return {
        "epoch": epoch,
        "epoch_iso": epoch.isoformat() if epoch else None,
        "start_balance": start,
    }


def _read_all_trades() -> list[dict]:
    """Every closed trade (oldest first) with the columns the views filter on."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT id, closed_at, symbol, side, analyst, entry, exit_price, size, "
            "leverage, soft_stop, duration_s, reason, won, net_pnl, fees, funding, "
            "dry_run FROM trades ORDER BY id ASC"
        ).fetchall()
        con.close()
    except Exception:
        return []
    keys = ("id", "closed_at", "symbol", "side", "analyst", "entry", "exit_price",
            "size", "leverage", "soft_stop", "duration_s", "reason", "won",
            "net_pnl", "fees", "funding", "dry_run")
    return [dict(zip(keys, r)) for r in rows]


def _trades_for_view(view: str | None) -> list[dict]:
    """Closed trades filtered to a view (oldest first); None = all trades."""
    rows = _read_all_trades()
    if view is None:
        return rows
    return live_epoch.split_trades(rows, _epoch_info()["epoch"], view)


def _read_ledger_r(trade_ids: set[int]) -> dict[int, float]:
    """trade_id -> R multiple from bot_ledger for the given trades."""
    if not trade_ids:
        return {}
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT trade_id, r_mult FROM bot_ledger WHERE trade_id IS NOT NULL"
        ).fetchall()
        con.close()
    except Exception:
        return {}
    return {r[0]: float(r[1] or 0.0) for r in rows if r[0] in trade_ids}


def _read_r_curve(view: str | None) -> list[float]:
    """Cumulative R (bot_ledger r_mult) over the view's trades, oldest first."""
    trades = _trades_for_view(view)
    r_by_id = _read_ledger_r({t["id"] for t in trades})
    cum, out = 0.0, []
    for t in trades:
        cum += r_by_id.get(t["id"], 0.0)
        out.append(round(cum, 3))
    return out


def _read_ladder_leverage() -> dict[str, int]:
    """analyst -> current leverage from the analyst_stats ladder."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute("SELECT analyst, leverage FROM analyst_stats").fetchall()
        con.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def _read_source_stats(view: str) -> dict[str, dict]:
    """Per-source stats computed from the view's trades (not analyst_stats):
    n, wins, losses, net PnL, cumulative R, plus the current ladder leverage."""
    trades = _trades_for_view(view)
    r_by_id = _read_ledger_r({t["id"] for t in trades})
    lev = _read_ladder_leverage()
    start_lev = int(os.getenv("LEVERAGE_START", "75"))
    out: dict[str, dict] = {}
    for t in trades:
        name = t["analyst"] or ""
        s = out.setdefault(name, {"analyst": name, "wins": 0, "losses": 0,
                                  "total": 0, "realized_pnl": 0.0, "r": 0.0,
                                  "leverage": lev.get(name, start_lev)})
        s["total"] += 1
        if t["won"]:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["realized_pnl"] = round(s["realized_pnl"] + float(t["net_pnl"] or 0.0), 4)
        s["r"] = round(s["r"] + r_by_id.get(t["id"], 0.0), 3)
    for s in out.values():
        s["win_rate"] = f"{(s['wins'] / s['total'] * 100):.0f}%" if s["total"] else "-"
    return out


def _read_open_counts(view: str | None = None) -> dict[str, int]:
    """Open position count per source, so a source holding live exposure is never
    hidden by the zero-closed-trades filter."""
    counts: dict[str, int] = {}
    for p in _read_positions(view):
        counts[p["analyst"] or ""] = counts.get(p["analyst"] or "", 0) + 1
    return counts


def _read_positions(view: str | None = None) -> list[dict]:
    """Open positions with their scale-out progress and mark-to-market PnL.
    view="live" keeps exchange-backed orders only, "paper" the DRYRUN- ones.
    While the bot runs in PAPER/DEMO (state dry_run=1) every open position is
    non-live - DEMO orders carry real demo orderIds but are not real money."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT symbol, side, entry, sl, tp, size, order_id, opened_at, "
            "analyst, tps, tps_hit, last_price, unrealized_pnl, soft_stop "
            "FROM positions WHERE status='open' ORDER BY id DESC"
        ).fetchall()
        con.close()
        out = []
        for r in rows:
            is_live = live_epoch.is_live_position(r[6]) and not _state.get("dry_run", False)
            if view is not None and is_live != (view == "live"):
                continue
            try:
                tps = json.loads(r[9]) if r[9] else []
            except (ValueError, TypeError):
                tps = []
            out.append({
                "symbol": r[0], "side": r[1], "entry": r[2], "sl": r[3], "tp": r[4],
                "size": r[5], "order_id": r[6], "opened_at": r[7], "analyst": r[8] or "",
                "tps": tps, "tps_hit": r[10] or 0, "last_price": r[11] or 0,
                "unrealized_pnl": r[12] or 0, "soft_stop": bool(r[13]) if len(r) > 13 else False,
            })
        return out
    except Exception:
        return []


def _read_trades(limit: int = 40, view: str | None = None) -> list[dict]:
    """Most recent closed trades for the blotter, newest first.
    view=None returns every trade (legacy); "live"/"paper" filters by epoch."""
    rows = _trades_for_view(view)
    return list(reversed(rows))[:limit]


def _read_equity_curve(view: str | None = None) -> list[float]:
    """Running cumulative net PnL over the view's closed trades (oldest first).
    The live curve is anchored at the live start balance (first point) when
    one is configured; paper/None curves start at 0 as before."""
    trades = _trades_for_view(view)
    cum, out = 0.0, []
    if view == "live":
        start = _epoch_info()["start_balance"]
        if start is not None:
            cum = float(start)
            out.append(round(cum, 2))
    for t in trades:
        cum += float(t["net_pnl"] or 0.0)
        out.append(round(cum, 2))
    return out


def _read_pending() -> list[dict]:
    """Queued pending entries (RSI confirmation / Oracle pullback / resting limits)."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        row = con.execute("SELECT v FROM strategy_state WHERE k='pending_entries'").fetchone()
        items = json.loads(row[0]) if row and row[0] else []
        items = items if isinstance(items, list) else []
        # Limits resting on the exchange (DEMO/LIVE) render as pending rows too:
        # they are not positions until they fill, so they never reach the
        # positions table, but the operator must see them.
        row = con.execute("SELECT v FROM strategy_state WHERE k='resting_orders'").fetchone()
        con.close()
        resting = json.loads(row[0]) if row and row[0] else []
        for r in resting if isinstance(resting, list) else []:
            filled = float(r.get("filled_size") or 0)
            cond = "resting on exchange"
            if filled > 0:
                cond += f" ({filled}/{r.get('size')} filled)"
            items.append({
                "symbol": r.get("symbol"), "side": r.get("side"),
                "analyst_key": r.get("analyst_key"), "source": r.get("source"),
                "condition": cond, "ref_price": r.get("entry"),
                "created_at": r.get("placed_at"), "order_id": r.get("order_id"),
            })
        return items
    except Exception:
        return []


def _read_shadow(limit: int = 30) -> list[dict]:
    """Tail of strategy_shadow.csv — the measure-then-tune filter verdicts."""
    path = Path(__file__).parent / "strategy_shadow.csv"
    try:
        import csv
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            rows = list(csv.DictReader(f))
        return rows[-limit:][::-1]   # newest first
    except Exception:
        return []


def _read_watches() -> list[dict]:
    """Active POI watches (areas of interest being monitored / armed)."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT symbol, analyst, direction, level, status, note, created_at "
            "FROM watches WHERE status IN ('watching','armed') ORDER BY id DESC"
        ).fetchall()
        con.close()
        return [{"symbol": r[0], "analyst": r[1], "direction": r[2], "level": r[3],
                 "status": r[4], "note": (r[5] or "")[:80], "created_at": r[6]}
                for r in rows]
    except Exception:
        return []


def _read_strategy_state() -> dict:
    """Return the persisted strategy key→value store (e.g. the OracleAlgo BTC bias)."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute("SELECT k, v FROM strategy_state").fetchall()
        con.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def _read_analyst_stats(view: str | None = None) -> list[dict]:
    """Per-analyst/strategy leverage, win/loss tally and realized PnL, best PnL first.

    view="live" computes the tally from live trades since the epoch (leverage
    still comes from the analyst_stats ladder); None/"paper" reads the stored
    analyst_stats rows, which are the paper-era totals.

    Sources with no activity at all (no closed trades and no open position) are
    suppressed - an empty row is pure noise. Nothing is hidden by name: a source
    that has traded always shows, so the display can never conceal real activity.
    """
    open_counts = _read_open_counts(view)
    if view == "live":
        stats = _read_source_stats("live")
        ladder = _read_ladder_leverage()
        start_lev = int(os.getenv("LEVERAGE_START", "75"))
        for name in open_counts:
            stats.setdefault(name, {"analyst": name, "wins": 0, "losses": 0, "total": 0,
                                    "realized_pnl": 0.0, "r": 0.0, "win_rate": "-",
                                    "leverage": ladder.get(name, start_lev)})
        out = []
        for s in stats.values():
            s = dict(s)
            s["open"] = open_counts.get(s["analyst"], 0)
            out.append(s)
        out.sort(key=lambda a: (a["realized_pnl"], a["leverage"]), reverse=True)
        return out
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        cols = [c[1] for c in con.execute("PRAGMA table_info(analyst_stats)").fetchall()]
        pnl_col = "COALESCE(realized_pnl, 0)" if "realized_pnl" in cols else "0"
        rows = con.execute(
            f"SELECT analyst, leverage, wins, losses, {pnl_col} FROM analyst_stats "
            "ORDER BY {pnl} DESC, leverage DESC, analyst".format(pnl=pnl_col)
        ).fetchall()
        con.close()
        out = []
        for r in rows:
            wins, losses = r[2] or 0, r[3] or 0
            total = wins + losses
            open_n = open_counts.get(r[0], 0)
            if not total and not open_n:
                continue
            wr = f"{(wins / total * 100):.0f}%" if total else "—"
            out.append({"analyst": r[0], "leverage": r[1], "wins": wins,
                        "losses": losses, "total": total, "win_rate": wr,
                        "realized_pnl": r[4] or 0, "open": open_n})
        return out
    except Exception:
        return []


def _read_roster(view: str | None = None) -> list[dict]:
    """
    One card per signal source: the strategy feeds (RSI Extreme, OracleAlgo) PLUS
    every whitelisted analyst that has actually traded. Analysts with no closed
    trades and no open position are dropped; the strategy feeds always show,
    even through a quiet stretch.
    """
    raw = os.getenv("ANALYST_WHITELIST", "")
    analysts = [n.strip() for n in raw.split(",") if n.strip()]
    strategies = ["RSI Extreme", "OracleAlgo"]
    start_lev = int(os.getenv("LEVERAGE_START", "75"))
    stats = {a["analyst"]: a for a in _read_analyst_stats(view)}
    open_counts = _read_open_counts(view)

    def card(name, kind):
        s = stats.get(name, {})
        wins, losses = s.get("wins", 0), s.get("losses", 0)
        total = wins + losses
        return {
            "name": name, "kind": kind, "closed": total, "wins": wins, "losses": losses,
            "r": s.get("r"),
            "win_rate": f"{(wins / total * 100):.0f}%" if total else "—",
            "leverage": s.get("leverage", start_lev),
            "realized_pnl": s.get("realized_pnl", 0),
            "open": open_counts.get(name, 0),
        }

    roster = [card(n, "strategy") for n in strategies] + [card(n, "analyst") for n in analysts]
    # include any one-off analysts that traded but aren't in the whitelist
    known = set(strategies) | set(analysts)
    for name in stats:
        if name not in known:
            roster.append(card(name, "other"))
    # drop dead weight: no closed trades and nothing live. Strategies are exempt.
    roster = [c for c in roster
              if c["kind"] == "strategy" or c["closed"] or c["open"]]
    # most active first, then by PnL
    roster.sort(key=lambda c: (c["closed"], c["realized_pnl"]), reverse=True)
    return roster


def _read_signals(limit: int = 40, show_all: bool = False) -> list[dict]:
    """The most recent `limit` parsed signals with their outcomes. By default the
    `no_signal` chatter is hidden (it dominates the feed); show_all includes it."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        where = "" if show_all else "WHERE outcome != 'no_signal' "
        rows = con.execute(
            "SELECT ts, analyst, symbol, side, outcome, raw_text "
            f"FROM signals {where}ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        con.close()
        return [
            {"ts": r[0], "analyst": r[1], "symbol": r[2], "side": r[3],
             "outcome": r[4], "raw_text": (r[5] or "")[:100]}
            for r in rows
        ]
    except Exception:
        return []


def _elapsed(iso: str | None) -> str:
    """Format an ISO timestamp as a human 'Ns/Nm/Nh ago' string."""
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        return f"{secs // 3600}h ago"
    except Exception:
        return iso


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/status")
def api_status():
    """JSON snapshot of process state, open positions, analyst stats and recent signals.
    ?view=live (default) restricts to the live book since the epoch; ?view=paper
    returns the paper archive. Extra keys: view, epoch, r_curve."""
    view = _view_arg()
    info = _epoch_info()
    return jsonify({
        "state": _state,
        "view": view,
        "epoch": {"start": info["epoch_iso"], "start_balance": info["start_balance"]},
        "positions": _read_positions(view),
        "analyst_stats": _read_analyst_stats(view),
        "recent_signals": _read_signals(20),
        "trades": _read_trades(40, view=view),
        "equity_curve": _read_equity_curve(view=view),
        "r_curve": _read_r_curve(view),
        "pending": _read_pending(),
        "shadow": _read_shadow(30),
    })


@app.route("/mobile")
def mobile_root():
    """Redirect to the trailing-slash form so the page's relative asset paths
    (css/app.css, js/app.js, *.html links) resolve under /mobile/."""
    return redirect("/mobile/", code=308)


@app.route("/mobile/")
def mobile_index():
    """Serve the Unity Oracle mobile/responsive UI landing page."""
    return send_from_directory(MOBILE_DIR, "index.html")


@app.route("/mobile/<path:filename>")
def mobile_asset(filename: str):
    """Serve any file in the mobile UI bundle (pages, css/, js/, images).
    send_from_directory blocks path traversal outside MOBILE_DIR."""
    return send_from_directory(MOBILE_DIR, filename)


# ---------------------------------------------------------------------------
# User-facing v1 API: bots-as-a-service. Register/login returns a bearer token;
# authenticated users follow bots and run a paper portfolio that mirrors them.
# ---------------------------------------------------------------------------

def _current_user() -> dict | None:
    """Resolve the user from 'Authorization: Bearer <token>', else None."""
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return accounts.get_user_by_token(header[7:].strip())


def _user_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _current_user()
        if not user:
            return jsonify({"success": False, "error": "Login required"}), 401
        return fn(user, *args, **kwargs)
    return wrapper


@app.route("/api/v1/register", methods=["POST"])
def api_v1_register():
    """Create an account. If USER_INVITE_CODE is set in .env, it is required."""
    data = request.get_json(silent=True) or {}
    invite_required = os.getenv("USER_INVITE_CODE", "").strip()
    if invite_required and not hmac.compare_digest(
            str(data.get("invite_code", "")), invite_required):
        return jsonify({"success": False, "error": "Invalid invite code"}), 403
    token, err = accounts.register_user(data.get("username", ""),
                                        data.get("password", ""))
    if err:
        return jsonify({"success": False, "error": err}), 400
    return jsonify({"success": True, "token": token})


@app.route("/api/v1/login", methods=["POST"])
def api_v1_login():
    data = request.get_json(silent=True) or {}
    token, err = accounts.login_user(data.get("username", ""),
                                     data.get("password", ""))
    if err:
        return jsonify({"success": False, "error": err}), 401
    return jsonify({"success": True, "token": token})


@app.route("/api/v1/bots")
def api_v1_bots():
    """All virtual bots with balances and stats. If logged in, marks follows."""
    bots = accounts.get_bots()
    user = _current_user()
    following = set(accounts.get_subscriptions(user["id"])) if user else set()
    for b in bots:
        b["following"] = b["key"] in following
    return jsonify({"success": True, "bots": bots,
                    "logged_in": bool(user)})


@app.route("/api/v1/bots/<path:bot_key>")
def api_v1_bot_detail(bot_key: str):
    bot = accounts.get_bot_detail(bot_key)
    if not bot:
        return jsonify({"success": False, "error": "Unknown bot"}), 404
    user = _current_user()
    bot["following"] = bool(
        user and bot_key in accounts.get_subscriptions(user["id"]))
    return jsonify({"success": True, "bot": bot})


@app.route("/api/v1/me")
@_user_required
def api_v1_me(user):
    return jsonify({"success": True, "user": {
        "username": user["username"], "balance": user["balance"],
        "start_balance": user["start_balance"], "risk_pct": user["risk_pct"],
        "created_at": user["created_at"],
        "following": accounts.get_subscriptions(user["id"]),
    }})


@app.route("/api/v1/me/follow", methods=["POST"])
@_user_required
def api_v1_follow(user):
    data = request.get_json(silent=True) or {}
    ok, err = accounts.follow(user["id"], str(data.get("bot", "")))
    if not ok:
        return jsonify({"success": False, "error": err}), 400
    return jsonify({"success": True,
                    "following": accounts.get_subscriptions(user["id"])})


@app.route("/api/v1/me/unfollow", methods=["POST"])
@_user_required
def api_v1_unfollow(user):
    data = request.get_json(silent=True) or {}
    accounts.unfollow(user["id"], str(data.get("bot", "")))
    return jsonify({"success": True,
                    "following": accounts.get_subscriptions(user["id"])})


@app.route("/api/v1/me/portfolio")
@_user_required
def api_v1_portfolio(user):
    return jsonify({"success": True,
                    "portfolio": accounts.get_portfolio(user["id"])})


@app.route("/portfolio")
def portfolio_page():
    return redirect("/bots", code=308)


@app.route("/bots")
def bots_page():
    """Self-contained user-facing page: bot marketplace + paper portfolio.
    Talks to /api/v1 with a bearer token kept in localStorage."""
    return Response(_BOTS_PAGE_HTML, content_type="text/html; charset=utf-8")


_BOTS_PAGE_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unity Bots</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#e6edf3;
          --muted:#8b949e; --green:#3fb950; --red:#f85149; --accent:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif; }
  .wrap { max-width:1100px; margin:0 auto; padding:20px 16px 60px; }
  header { display:flex; align-items:center; justify-content:space-between;
           margin-bottom:18px; flex-wrap:wrap; gap:10px; }
  h1 { font-size:20px; margin:0; }
  h2 { font-size:15px; color:var(--muted); margin:26px 0 10px;
       text-transform:uppercase; letter-spacing:.08em; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:12px; }
  .card { background:var(--card); border:1px solid var(--border);
          border-radius:10px; padding:14px; }
  .bot-head { display:flex; justify-content:space-between; align-items:baseline; gap:8px; }
  .bot-name { font-weight:600; }
  .tag { font-size:10px; padding:1px 7px; border-radius:10px;
         border:1px solid var(--border); color:var(--muted); white-space:nowrap; }
  .tag.strategy { color:var(--accent); border-color:var(--accent); }
  .bal { font-size:20px; font-weight:700; margin:6px 0 0; }
  .ret { font-size:12px; }
  .pos { color:var(--green); } .neg { color:var(--red); }
  .meta { color:var(--muted); font-size:12px; margin-top:4px; }
  .spark { width:100%; height:36px; margin-top:8px; }
  button { background:#21262d; color:var(--text); border:1px solid var(--border);
           border-radius:6px; padding:6px 14px; cursor:pointer; font-size:13px; }
  button:hover { border-color:var(--muted); }
  button.primary { background:#1f6feb; border-color:#1f6feb; }
  button.following { background:transparent; border-color:var(--green); color:var(--green); }
  .follow-btn { width:100%; margin-top:10px; }
  input { background:#0d1117; border:1px solid var(--border); color:var(--text);
          border-radius:6px; padding:7px 10px; font-size:13px; width:100%; }
  .auth-box { max-width:340px; }
  .auth-box .row { margin-bottom:8px; }
  .err { color:var(--red); font-size:12px; min-height:16px; }
  .userbar { display:flex; gap:10px; align-items:center; color:var(--muted); font-size:13px; }
  .stats-row { display:flex; gap:18px; flex-wrap:wrap; margin-top:6px; }
  .stat .label { color:var(--muted); font-size:11px; display:block; }
  .stat .value { font-size:16px; font-weight:600; }
  table { width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }
  th, td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--border); }
  th { color:var(--muted); font-weight:500; font-size:11px; text-transform:uppercase; }
  .hidden { display:none; }
  a { color:var(--accent); text-decoration:none; }
</style></head>
<body><div class="wrap">
<header>
  <h1>Unity Bots</h1>
  <div class="userbar" id="userbar"></div>
</header>

<div id="authPanel" class="card auth-box hidden">
  <div class="row"><input id="username" placeholder="username" autocomplete="username"></div>
  <div class="row"><input id="password" type="password" placeholder="password" autocomplete="current-password"></div>
  <div class="row hidden" id="inviteRow"><input id="invite" placeholder="invite code"></div>
  <div class="err" id="authErr"></div>
  <div style="display:flex; gap:8px">
    <button class="primary" onclick="auth('login')">Log in</button>
    <button onclick="auth('register')">Register</button>
  </div>
</div>

<div id="portfolio" class="hidden">
  <h2>My portfolio</h2>
  <div class="card">
    <div class="stats-row">
      <div class="stat"><span class="label">Balance</span><span class="value" id="pBal"></span></div>
      <div class="stat"><span class="label">Return</span><span class="value" id="pRet"></span></div>
      <div class="stat"><span class="label">Following</span><span class="value" id="pFollow"></span></div>
      <div class="stat"><span class="label">Open positions</span><span class="value" id="pOpen"></span></div>
    </div>
    <svg class="spark" id="pSpark" preserveAspectRatio="none" viewBox="0 0 100 30"></svg>
    <div id="pPositions"></div>
  </div>
</div>

<h2>Available bots</h2>
<div class="grid" id="botGrid"></div>

</div>
<script>
const tok = () => localStorage.getItem('ub_token') || '';
const hdrs = () => tok() ? {'Authorization':'Bearer '+tok(),'Content-Type':'application/json'}
                         : {'Content-Type':'application/json'};

function spark(el, pts, color) {
  if (!pts || pts.length < 2) { el.innerHTML=''; return; }
  const min = Math.min(...pts), max = Math.max(...pts), span = (max-min) || 1;
  const step = 100 / (pts.length - 1);
  const d = pts.map((p,i) =>
    (i?'L':'M') + (i*step).toFixed(2) + ',' + (28 - (p-min)/span*26).toFixed(2)
  ).join(' ');
  el.innerHTML = '<path d="'+d+'" fill="none" stroke="'+color+'" stroke-width="1.5"/>';
}

function fmt(v) { return '$' + Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }

async function loadBots() {
  const r = await fetch('/api/v1/bots', {headers: hdrs()});
  const j = await r.json();
  const grid = document.getElementById('botGrid');
  grid.innerHTML = '';
  for (const b of j.bots) {
    const retCls = b.return_pct >= 0 ? 'pos' : 'neg';
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML =
      '<div class="bot-head"><span class="bot-name">'+b.display_name+'</span>'+
      '<span class="tag '+b.kind+'">'+b.kind.toUpperCase()+'</span></div>'+
      '<div class="bal">'+fmt(b.balance)+' <span class="ret '+retCls+'">'+
      (b.return_pct>=0?'+':'')+b.return_pct+'%</span></div>'+
      '<div class="meta">'+b.trades+' trades'+
      (b.win_rate!==null ? ' / '+b.win_rate+'% win rate' : '')+
      ' / '+b.open_positions+' open</div>'+
      '<svg class="spark" preserveAspectRatio="none" viewBox="0 0 100 30"></svg>'+
      (j.logged_in
        ? '<button class="follow-btn '+(b.following?'following':'')+'" data-bot="'+b.key+'">'+
          (b.following?'Following':'Follow')+'</button>'
        : '');
    grid.appendChild(card);
    fetch('/api/v1/bots/'+encodeURIComponent(b.key)).then(r=>r.json()).then(d=>{
      if (d.success) spark(card.querySelector('svg'), d.bot.equity_curve,
        b.return_pct >= 0 ? '#3fb950' : '#f85149');
    });
    const btn = card.querySelector('.follow-btn');
    if (btn) btn.onclick = async () => {
      const action = btn.classList.contains('following') ? 'unfollow' : 'follow';
      await fetch('/api/v1/me/'+action, {method:'POST', headers:hdrs(),
        body: JSON.stringify({bot: btn.dataset.bot})});
      loadBots(); loadPortfolio();
    };
  }
}

async function loadPortfolio() {
  const panel = document.getElementById('portfolio');
  if (!tok()) { panel.classList.add('hidden'); return; }
  const r = await fetch('/api/v1/me/portfolio', {headers: hdrs()});
  if (r.status === 401) { localStorage.removeItem('ub_token'); refreshUI(); return; }
  const p = (await r.json()).portfolio;
  panel.classList.remove('hidden');
  document.getElementById('pBal').textContent = fmt(p.balance);
  const ret = document.getElementById('pRet');
  ret.textContent = (p.return_pct>=0?'+':'')+p.return_pct+'%';
  ret.className = 'value ' + (p.return_pct>=0?'pos':'neg');
  document.getElementById('pFollow').textContent = p.following.length;
  document.getElementById('pOpen').textContent = p.open_positions.length;
  spark(document.getElementById('pSpark'), p.equity_curve,
        p.return_pct>=0 ? '#3fb950' : '#f85149');
  const pos = document.getElementById('pPositions');
  pos.innerHTML = p.open_positions.length
    ? '<table><tr><th>Bot</th><th>Symbol</th><th>Side</th><th>Entry</th><th>Last</th></tr>'+
      p.open_positions.map(x=>'<tr><td>'+x.analyst+'</td><td>'+x.symbol+'</td><td>'+
        x.side.toUpperCase()+'</td><td>'+x.entry+'</td><td>'+(x.last_price||'-')+
        '</td></tr>').join('')+'</table>'
    : '';
}

async function auth(mode) {
  const body = {username: document.getElementById('username').value,
                password: document.getElementById('password').value,
                invite_code: document.getElementById('invite').value};
  const r = await fetch('/api/v1/'+mode, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!j.success) {
    document.getElementById('authErr').textContent = j.error || 'Failed';
    if ((j.error||'').toLowerCase().includes('invite'))
      document.getElementById('inviteRow').classList.remove('hidden');
    return;
  }
  localStorage.setItem('ub_token', j.token);
  refreshUI();
}

function logout() { localStorage.removeItem('ub_token'); refreshUI(); }

async function refreshUI() {
  const bar = document.getElementById('userbar');
  const authPanel = document.getElementById('authPanel');
  if (tok()) {
    const r = await fetch('/api/v1/me', {headers: hdrs()});
    if (r.ok) {
      const u = (await r.json()).user;
      bar.innerHTML = 'Signed in as <b>'+u.username+'</b>&nbsp;'+
                      '<button onclick="logout()">Log out</button>';
      authPanel.classList.add('hidden');
    } else { localStorage.removeItem('ub_token'); return refreshUI(); }
  } else {
    bar.innerHTML = '';
    authPanel.classList.remove('hidden');
  }
  loadBots(); loadPortfolio();
}

refreshUI();
setInterval(() => { loadBots(); loadPortfolio(); }, 30000);
</script>
</body></html>"""


@app.route("/")
def index():
    """Render the full dashboard page (self-contained HTML + CSS, no external assets)."""
    s        = _state
    show_all = request.args.get("all") == "1"
    view     = _view_arg() or "live"
    info     = _epoch_info()
    positions = _read_positions(view)
    signals  = _read_signals(40, show_all=show_all)
    analysts = _read_analyst_stats(view)
    roster   = _read_roster(view)
    watches  = _read_watches()
    state    = _read_strategy_state()
    trades   = _read_trades(40, view=view)
    r_curve  = _read_r_curve(view)
    # Live BTC 4H ADX regime — same cache as the bot; cache hit = free
    try:
        import market_regime as mr
        _btc_htf_dir, _btc_htf_adx, _btc_htf_regime = mr.get_btc_htf_regime("4h")
        _btc_adx_label = (f"{_btc_htf_dir.upper()} · {(_btc_htf_regime or '').replace('_',' ')}"
                          if _btc_htf_dir else "—")
        _btc_adx_color = ("#3fb950" if _btc_htf_dir == "bull" else
                          "#f85149" if _btc_htf_dir == "bear" else "#8b949e")
    except Exception:
        _btc_adx_label, _btc_adx_color = "—", "#8b949e"
    equity   = _read_equity_curve(view=view)
    pending  = _read_pending()
    shadow   = _read_shadow(30)
    log_tail = _read_log_tail()

    exec_label  = s.get("exec_mode") or ("DRY RUN" if s.get("dry_run") else "LIVE")
    mode_badge  = (f'<span class="badge dry">{exec_label}</span>' if s.get("dry_run")
                   else '<span class="badge live">LIVE</span>')
    chrome_icon = "✅" if s.get("chrome_connected") else "❌"
    discord_icon= "✅" if s.get("discord_tab") else "❌"
    running     = s.get("poll_count", 0) > 0

    # Aggregate PnL across strategies (live view: live trades only)
    total_realized = sum(a.get("realized_pnl", 0) for a in analysts)
    total_r = r_curve[-1] if r_curve else 0.0
    n_view_trades = len(_trades_for_view(view))

    def _qs(**overrides):
        """Rebuild the query string keeping the other toggle intact."""
        params = {"view": view, "all": "1" if show_all else ""}
        params.update(overrides)
        return "?" + "&".join(f"{k}={v}" for k, v in params.items() if v)

    if info["epoch"] is not None:
        ep_txt = info["epoch"].strftime("%Y-%m-%d %H:%M")
        bal_txt = (f", start balance ${info['start_balance']:,.2f}"
                   if info["start_balance"] is not None else "")
        banner_txt = f"Live since {ep_txt}{bal_txt}"
    else:
        banner_txt = "No live epoch set (LIVE_EPOCH_START / tools/set_live_epoch.py)"
    live_cls = "view-on" if view == "live" else ""
    paper_cls = "view-on" if view == "paper" else ""
    view_bar = (
        f'<div class="epoch-bar"><span class="epoch-txt">{banner_txt}</span>'
        f'<span class="view-toggle"><a class="{live_cls}" href="{_qs(view="live")}">Live (since epoch)</a>'
        f'<a class="{paper_cls}" href="{_qs(view="paper")}">Paper/Demo archive</a></span>'
        f'<span class="small">{n_view_trades} closed trade{"" if n_view_trades == 1 else "s"} in this view</span></div>'
    )
    total_unreal = sum(p.get("unrealized_pnl", 0) for p in positions)

    def _pnl_html(v):
        cls = "buy" if v >= 0 else "sell"
        return f'<span class="{cls}">${v:+,.2f}</span>'

    def _card_html(c):
        tag = ('<span class="tcard-tag strat">SIGNAL</span>' if c["kind"] == "strategy"
               else '<span class="tcard-tag">ANALYST</span>')
        pnl_cls = "buy" if c["realized_pnl"] >= 0 else "sell"
        # A card with 0 closed trades only survives the roster filter because it
        # holds live exposure - say so, or it reads as an empty card.
        open_n = c.get("open", 0)
        closed_txt = f"{c['closed']} closed trade{'' if c['closed'] == 1 else 's'}"
        if open_n:
            closed_txt += f" - {open_n} open"
        return f"""<div class="tcard">
          <div class="tcard-name">{c['name']} {tag}</div>
          <div class="tcard-closed">{closed_txt}</div>
          <div class="tcard-stats">
            <span title="win rate">{c['win_rate']}</span>
            <span class="sep">·</span>
            <span title="leverage">{c['leverage']}x</span>
            <span class="sep">·</span>
            <span class="{pnl_cls}" title="realized PnL">${c['realized_pnl']:+,.2f}</span>
            {f'<span class="sep">&middot;</span><span title="cumulative R">{c["r"]:+.2f}R</span>' if c.get("r") is not None else ""}
          </div>
        </div>"""

    def roster_rows():
        """Row 1: strategy signal cards. Rows 2 & 3: the traders split in half."""
        if not roster:
            return ''
        signals = [c for c in roster if c["kind"] == "strategy"]
        traders = [c for c in roster if c["kind"] != "strategy"]
        mid = (len(traders) + 1) // 2          # first half gets the extra when odd
        row1, row2 = traders[:mid], traders[mid:]

        def row(cards, extra=""):
            if not cards:
                return ""
            return f'<div class="roster-row {extra}">' + "".join(_card_html(c) for c in cards) + "</div>"

        return row(signals, "signals") + row(row1) + row(row2)

    def _strat(env, default="true"):
        return ("on", "#3fb950") if os.getenv(env, default).lower() == "true" else ("off", "#8b949e")

    def _flag(env):
        """Filters default OFF (shadow-only) — green when enforced, grey when shadowing."""
        return ("on", "#3fb950") if os.getenv(env, "false").lower() == "true" else ("shadow", "#8b949e")

    # The OracleAlgo bias is stored as JSON ({"bias","ts"}) since the TTL change; tolerate
    # the legacy bare-string form too.
    bias_raw = state.get("btc_oracle_bias", "") or ""
    try:
        bias = json.loads(bias_raw).get("bias", "") if bias_raw.startswith("{") else bias_raw
    except (ValueError, TypeError):
        bias = bias_raw
    bias_html = ("—" if not bias else
                 f'<span class="buy">BULL</span>' if bias == "bull" else '<span class="sell">BEAR</span>')

    def pos_rows():
        if not positions:
            return '<tr><td colspan="9" class="empty">No open positions</td></tr>'
        html = ""
        for p in positions:
            side_cls = "buy" if p["side"] == "buy" else "sell"
            n = len(p["tps"]) or 1
            prog = f"TP {p['tps_hit']}/{n}" if n > 1 else ("hit" if p["tps_hit"] else "open")
            soft = ' <span class="tcard-tag" title="soft stop — triggers on a 1h candle close">SOFT</span>' if p.get("soft_stop") else ""
            html += f"""<tr>
              <td><b>{p['symbol']}</b><div class="small">{p['analyst']}</div></td>
              <td class="{side_cls}">{p['side'].upper()}</td>
              <td>{p['entry']}</td>
              <td>{p['sl']}{soft}</td>
              <td class="small">{', '.join(str(t) for t in p['tps']) if p['tps'] else p['tp']}</td>
              <td>{p['size']}</td>
              <td>{p['last_price'] or '—'}</td>
              <td>{_pnl_html(p['unrealized_pnl'])}</td>
              <td class="small">{prog}</td>
            </tr>"""
        return html

    def _dur(secs):
        secs = int(secs or 0)
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h{(secs % 3600) // 60}m"
        return f"{secs // 86400}d{(secs % 86400) // 3600}h"

    def trade_rows():
        if not trades:
            return '<tr><td colspan="9" class="empty">No closed trades yet</td></tr>'
        html = ""
        for t in trades:
            side_cls = "buy" if t["side"] == "buy" else "sell"
            wl = '<span class="buy">WIN</span>' if t["won"] else '<span class="sell">LOSS</span>'
            soft = ' <span class="tcard-tag">S</span>' if t.get("soft_stop") else ""
            html += f"""<tr>
              <td class="small">{(t['closed_at'] or '')[:19]}</td>
              <td><b>{t['symbol']}</b><div class="small">{t['analyst']}</div></td>
              <td class="{side_cls}">{t['side'].upper()}</td>
              <td class="small">{t['entry']} → {t['exit_price']}</td>
              <td class="small">{_dur(t['duration_s'])}</td>
              <td class="small">{t['reason']}{soft}</td>
              <td>{wl}</td>
              <td class="small">{t['leverage']}x</td>
              <td>{_pnl_html(t['net_pnl'])}</td>
            </tr>"""
        return html

    def pending_rows():
        if not pending:
            return '<tr><td colspan="5" class="empty">No pending entries</td></tr>'
        html = ""
        for it in pending:
            side_cls = "buy" if it.get("side") == "buy" else "sell"
            cond = it.get("condition", "")
            src = it.get("analyst_key") or it.get("source") or "—"
            html += f"""<tr>
              <td><b>{it.get('symbol','')}</b><div class="small">{src}</div></td>
              <td class="{side_cls}">{(it.get('side') or '').upper()}</td>
              <td>{cond}</td>
              <td>{it.get('ref_price','')}</td>
              <td class="small mono">{_elapsed(it.get('created_at'))}</td>
            </tr>"""
        return html

    def shadow_rows():
        if not shadow:
            return '<tr><td colspan="9" class="empty">No strategy signals logged yet</td></tr>'
        html = ""
        for r in shadow:
            dec = r.get("decision", "")
            dec_cls = ("outcome-ok" if dec == "enter" else
                       "outcome-err" if "block" in dec or "counter" in dec or "repeat" in dec else
                       "outcome-info")
            # btc_bias + btc_adx_regime stacked in one cell
            bias = r.get("btc_bias", "") or "—"
            btc_adx = r.get("btc_adx_regime", "")
            bias_cell = (f'{bias}<div class="small" style="color:#6e7681">{btc_adx}</div>'
                         if btc_adx else bias)
            # Symbol's own ADX (RSI rows) + value
            adx_r = r.get("adx_regime", "")
            adx_v = r.get("adx_value", "")
            if adx_r:
                adx_color = ("#f85149" if "strong" in adx_r else
                             "#d29922" if "moderate" in adx_r or "indecisive" in adx_r else
                             "#8b949e")
                adx_cell = f'<span style="color:{adx_color}">{adx_r.replace("_"," ")}</span>'
                if adx_v:
                    adx_cell += f'<div class="small">{adx_v}</div>'
            else:
                adx_cell = "—"
            # repeat_n badge on the symbol when > 1
            rn = r.get("repeat_n", "")
            rn_badge = (f' <span class="tcard-tag" style="color:#d29922">#{rn}</span>'
                        if rn and str(rn) not in ("", "1") else "")
            html += f"""<tr>
              <td class="small">{(r.get('ts') or '')[11:19]}</td>
              <td><b>{r.get('symbol','')}</b>{rn_badge}</td>
              <td>{(r.get('side') or '').upper()}</td>
              <td class="small">{r.get('rsi_value','') or r.get('oracle_type','')}</td>
              <td class="small">{r.get('change_24h','')}</td>
              <td class="small">{bias_cell}</td>
              <td class="small">{adx_cell}</td>
              <td class="small">s:{r.get('f_strength','')} c:{r.get('f_chase','')} r:{r.get('f_regime','')}</td>
              <td class="{dec_cls}">{dec}</td>
            </tr>"""
        return html

    def equity_svg():
        if len(equity) < 2:
            if view == "live" and info["start_balance"] is not None:
                return (f'<div class="small" style="padding:14px;">Equity '
                        f'<span class="buy">${info["start_balance"]:,.2f}</span> - no live trades closed yet.</div>')
            return '<div class="small" style="padding:14px;">Need at least 2 closed trades to chart.</div>'
        w, h, pad = 720, 120, 8
        lo, hi = min(equity), max(equity)
        rng = (hi - lo) or 1
        n = len(equity)
        pts = []
        for i, v in enumerate(equity):
            x = pad + i * (w - 2 * pad) / (n - 1)
            y = h - pad - (v - lo) / rng * (h - 2 * pad)
            pts.append(f"{x:.1f},{y:.1f}")
        last = equity[-1]
        base = equity[0] if (view == "live" and info["start_balance"] is not None) else 0.0
        colour = "#3fb950" if last >= base else "#f85149"
        zero_y = h - pad - (base - lo) / rng * (h - 2 * pad) if lo <= base <= hi else None
        zero_line = (f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{w-pad}" y2="{zero_y:.1f}" '
                     'stroke="#30363d" stroke-dasharray="3,3"/>') if zero_y is not None else ""
        if base:
            foot = (f'Equity: <span class="{"buy" if last>=base else "sell"}">${last:,.2f}</span> '
                    f'(net <span class="{"buy" if last>=base else "sell"}">${last-base:+,.2f}</span>, '
                    f'{total_r:+.2f}R) over {n-1} live trades from ${base:,.2f}')
        else:
            foot = (f'Cumulative net PnL: <span class="{"buy" if last>=0 else "sell"}">${last:+,.2f}</span> '
                    f'({total_r:+.2f}R) over {n} trades')
        return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
                f'style="width:100%;height:{h}px;">{zero_line}'
                f'<polyline fill="none" stroke="{colour}" stroke-width="2" points="{" ".join(pts)}"/>'
                f'</svg><div class="small" style="padding:0 14px 12px;">{foot}</div>')

    def watch_rows():
        if not watches:
            return '<tr><td colspan="5" class="empty">No active areas of interest</td></tr>'
        html = ""
        for w in watches:
            dir_cls = "buy" if w["direction"] == "buy" else "sell" if w["direction"] == "sell" else "small"
            dir_txt = (w["direction"] or "?").upper()
            armed = w["status"] == "armed"
            status = ('<span class="buy">● ARMED</span>' if armed
                      else '<span class="small">watching</span>')
            html += f"""<tr>
              <td><b>{w['symbol']}</b><div class="small">{w['analyst']}</div></td>
              <td class="{dir_cls}">{dir_txt}</td>
              <td>{w['level'] or '<span class="small">chart</span>'}</td>
              <td>{status}</td>
              <td class="small mono">{w['note']}</td>
            </tr>"""
        return html

    def analyst_rows():
        if not analysts:
            return '<tr><td colspan="6" class="empty">No analyst trades resolved yet</td></tr>'
        html = ""
        for a in analysts:
            # Leverage bar across the 50–125 band
            pct = max(0, min(100, (a["leverage"] - 50) / 75 * 100))
            html += f"""<tr>
              <td><b>{a['analyst']}</b></td>
              <td>
                <div style="display:flex;align-items:center;gap:8px;">
                  <div style="flex:1;background:#21262d;border-radius:4px;height:8px;min-width:80px;">
                    <div style="width:{pct:.0f}%;background:#58a6ff;height:8px;border-radius:4px;"></div>
                  </div>
                  <b style="color:#f0f6fc;">{a['leverage']}x</b>
                </div>
              </td>
              <td class="buy">{a['wins']}</td>
              <td class="sell">{a['losses']}</td>
              <td>{a['win_rate']}</td>
              <td>{_pnl_html(a.get('realized_pnl', 0))}</td>
            </tr>"""
        return html

    def sig_rows():
        if not signals:
            return '<tr><td colspan="6" class="empty">No signals yet</td></tr>'
        html = ""
        for sig in signals:
            outcome = sig["outcome"] or ""
            cls = "outcome-ok" if "execut" in outcome or "dry_run" in outcome else \
                  "outcome-err" if "error" in outcome or "reject" in outcome else "outcome-info"
            html += f"""<tr>
              <td class="small">{sig['ts'][:19]}</td>
              <td>{sig['analyst'] or '—'}</td>
              <td><b>{sig['symbol'] or '—'}</b></td>
              <td>{sig['side'] or '—'}</td>
              <td class="{cls}">{outcome}</td>
              <td class="small mono">{sig['raw_text']}</td>
            </tr>"""
        return html

    now_str = datetime.now().strftime("%H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="15">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Discord Signal Bot</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }}
    a {{ color: #58a6ff; }}
    .header {{ background: #161b22; border-bottom: 1px solid #30363d; padding: 14px 24px; display: flex; align-items: center; gap: 16px; }}
    .header h1 {{ font-size: 18px; color: #f0f6fc; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; background: {'#3fb950' if running else '#f85149'}; box-shadow: 0 0 6px {'#3fb950' if running else '#f85149'}; }}
    .badge {{ padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
    .badge.dry {{ background: #b08800; color: #fff; }}
    .badge.live {{ background: #238636; color: #fff; }}
    .updated {{ margin-left: auto; color: #8b949e; font-size: 12px; }}
    .container {{ padding: 20px 24px; display: flex; flex-direction: column; gap: 20px; }}
    .roster-row {{ display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }}
    .roster-row:last-child {{ margin-bottom: 0; }}
    /* Traders stretch to share their row evenly; signal cards stay compact. */
    .tcard {{ flex: 1 1 0; min-width: 140px; background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 12px 14px; }}
    .roster-row.signals .tcard {{ flex: 0 0 220px; }}
    .tcard-name {{ font-size: 14px; font-weight: 700; color: #f0f6fc; display: flex; align-items: center; gap: 6px; }}
    .tcard-closed {{ font-size: 12px; color: #8b949e; margin: 4px 0 8px; }}
    .tcard-stats {{ font-size: 13px; color: #c9d1d9; display: flex; align-items: center; gap: 6px; }}
    .tcard-stats .sep {{ color: #6e7681; }}
    .tcard-tag {{ font-size: 9px; font-weight: 700; letter-spacing: .5px; padding: 1px 5px; border-radius: 6px; background: #21262d; color: #8b949e; }}
    .tcard-tag.strat {{ background: #1f2d3d; color: #58a6ff; }}
    .epoch-bar {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 10px 18px; display: flex; gap: 18px; align-items: center; flex-wrap: wrap; }}
    .epoch-txt {{ color: #f0f6fc; font-weight: 600; }}
    .view-toggle a {{ padding: 3px 10px; border: 1px solid #30363d; border-radius: 12px; font-size: 12px; text-decoration: none; color: #8b949e; margin-right: 6px; }}
    .view-toggle a.view-on {{ background: #238636; border-color: #238636; color: #fff; }}
    .status-bar {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px 18px; display: flex; gap: 28px; flex-wrap: wrap; }}
    .stat {{ display: flex; flex-direction: column; gap: 2px; }}
    .stat .label {{ font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: .5px; }}
    .stat .value {{ font-size: 16px; font-weight: 600; color: #f0f6fc; }}
    .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }}
    .card-title {{ padding: 10px 16px; background: #1c2128; font-weight: 600; font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: .5px; border-bottom: 1px solid #30363d; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ padding: 8px 12px; text-align: left; font-size: 11px; color: #8b949e; text-transform: uppercase; border-bottom: 1px solid #30363d; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #21262d; vertical-align: top; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #1c2128; }}
    .buy  {{ color: #3fb950; }}
    .sell {{ color: #f85149; }}
    .empty {{ color: #8b949e; text-align: center; padding: 20px; }}
    .mono {{ font-family: 'Consolas', monospace; }}
    .small {{ font-size: 12px; color: #8b949e; }}
    .outcome-ok   {{ color: #3fb950; }}
    .outcome-err  {{ color: #f85149; }}
    .outcome-info {{ color: #d29922; }}
    .log-box {{ padding: 12px 16px; font-family: 'Consolas', monospace; font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-all; max-height: 420px; overflow-y: auto; color: #8b949e; }}
    .log-box .ERR  {{ color: #f85149; }}
    .log-box .WARN {{ color: #d29922; }}
    .log-box .INFO {{ color: #58a6ff; }}
    /* Mobile: let wide tables scroll horizontally instead of overflowing the page. */
    @media (max-width: 760px) {{
      .container {{ padding: 12px; gap: 14px; }}
      .header {{ padding: 12px 14px; gap: 10px; flex-wrap: wrap; }}
      .status-bar {{ gap: 16px; padding: 10px 12px; }}
      table {{ display: block; overflow-x: auto; white-space: nowrap; }}
      .updated {{ width: 100%; margin-left: 0; }}
    }}
  </style>
</head>
<body>
  <div class="header">
    <div class="dot"></div>
    <h1>Discord Signal Bot</h1>
    {mode_badge}
    <span class="updated">↻ auto-refresh 15s &nbsp;|&nbsp; updated {now_str}</span>
  </div>

  <div class="container">

    {view_bar}

    {roster_rows()}

    <div class="status-bar">
      <div class="stat"><span class="label">Poll #</span><span class="value">{s.get('poll_count', 0)}</span></div>
      <div class="stat"><span class="label">Last Poll</span><span class="value">{_elapsed(s.get('last_poll_at'))}</span></div>
      <div class="stat"><span class="label">Last Signal</span><span class="value">{_elapsed(s.get('last_signal_at'))}</span></div>
      <div class="stat"><span class="label">Chrome</span><span class="value">{chrome_icon}</span></div>
      <div class="stat"><span class="label">Discord Tab</span><span class="value">{discord_icon}</span></div>
      <div class="stat"><span class="label">Balance</span><span class="value">{f"${s['balance']:,.2f}" if s.get('balance') is not None else '—'}</span></div>
      <div class="stat"><span class="label">Equity</span><span class="value">{f"${s['equity']:,.2f}" if s.get('equity') is not None else '—'}</span></div>
      <div class="stat"><span class="label">Free Margin</span><span class="value">{f"${s['free_margin']:,.2f}" if s.get('free_margin') is not None else '—'}</span></div>
      <div class="stat"><span class="label">Realized PnL ({view})</span><span class="value">{_pnl_html(total_realized)}</span></div>
      <div class="stat"><span class="label">Cum. R ({view})</span><span class="value">{total_r:+.2f}R</span></div>
      <div class="stat"><span class="label">Unrealized PnL</span><span class="value">{_pnl_html(total_unreal)}</span></div>
    </div>

    <div class="status-bar">
      <div class="stat"><span class="label">RSI Extreme</span><span class="value" style="color:{_strat('RSI_EXTREME_ENABLED')[1]}">{_strat('RSI_EXTREME_ENABLED')[0]}</span></div>
      <div class="stat"><span class="label">First Alert</span><span class="value" style="color:{_strat('RSI_FIRST_ALERT_ONLY')[1]}">{_strat('RSI_FIRST_ALERT_ONLY')[0]}</span></div>
      <div class="stat"><span class="label">RSI Filters</span><span class="value" style="color:{_flag('RSI_FILTERS_ENABLED')[1]}">{_flag('RSI_FILTERS_ENABLED')[0]}</span></div>
      <div class="stat"><span class="label">RSI Confirm</span><span class="value" style="color:{_flag('RSI_CONFIRM_ENABLED')[1]}">{_flag('RSI_CONFIRM_ENABLED')[0]}</span></div>
      <div class="stat"><span class="label">OracleAlgo</span><span class="value" style="color:{_strat('ORACLEALGO_ENABLED')[1]}">{_strat('ORACLEALGO_ENABLED')[0]}</span></div>
      <div class="stat"><span class="label">HTF Fallback</span><span class="value" style="color:{_strat('ORACLE_HTF_FALLBACK')[1]}">{_strat('ORACLE_HTF_FALLBACK')[0]}</span></div>
      <div class="stat"><span class="label">Oracle Confluence</span><span class="value">N={os.getenv('ORACLE_CONFLUENCE_N','1')}</span></div>
      <div class="stat"><span class="label">BTC 4H Bias</span><span class="value">{bias_html}</span></div>
      <div class="stat"><span class="label">BTC 4H Regime</span><span class="value" style="color:{_btc_adx_color};font-size:13px;">{_btc_adx_label}</span></div>
      <div class="stat"><span class="label">Risk / Trade</span><span class="value">{float(os.getenv('RISK_PCT','0.01'))*100:.1f}%</span></div>
      <div class="stat"><span class="label">Shadow Log</span><span class="value" style="color:{_strat('STRATEGY_SHADOW_LOG')[1]}">{_strat('STRATEGY_SHADOW_LOG')[0]}</span></div>
    </div>

    <div class="card">
      <div class="card-title">Open Positions ({len(positions)}) - {view}</div>
      <table>
        <thead><tr><th>Symbol / Strategy</th><th>Side</th><th>Entry</th><th>SL</th><th>TPs</th><th>Size</th><th>Last</th><th>Unreal. PnL</th><th>Progress</th></tr></thead>
        <tbody>{pos_rows()}</tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Equity Curve - {"live book since epoch" if view == "live" else "paper archive, cumulative net PnL"}</div>
      {equity_svg()}
    </div>

    <div class="card">
      <div class="card-title">Closed Trades ({len(trades)} of {n_view_trades}) - {view}</div>
      <table>
        <thead><tr><th>Closed</th><th>Symbol / Source</th><th>Side</th><th>Entry → Exit</th><th>Held</th><th>Reason</th><th>W/L</th><th>Lev</th><th>Net&nbsp;PnL</th></tr></thead>
        <tbody>{trade_rows()}</tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Pending Entries ({len(pending)})</div>
      <table>
        <thead><tr><th>Symbol / Source</th><th>Side</th><th>Condition</th><th>Ref Price</th><th>Age</th></tr></thead>
        <tbody>{pending_rows()}</tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Areas of Interest — Watchlist ({len(watches)})</div>
      <table>
        <thead><tr><th>Symbol / Analyst</th><th>Lean</th><th>Level</th><th>Status</th><th>Note</th></tr></thead>
        <tbody>{watch_rows()}</tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Analyst &amp; Strategy Performance ({len(analysts)}) - {view}</div>
      <table>
        <thead><tr><th>Analyst / Strategy</th><th>Leverage (50–125x)</th><th>Wins</th><th>Losses</th><th>Win&nbsp;Rate</th><th>Realized&nbsp;PnL</th></tr></thead>
        <tbody>{analyst_rows()}</tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Strategy Shadow Log — filter what-ifs ({len(shadow)})</div>
      <table>
        <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>RSI/Type</th><th>24h Δ</th><th>Bias / BTC ADX</th><th>Symbol ADX</th><th>Filters (s/c/r)</th><th>Decision</th></tr></thead>
        <tbody>{shadow_rows()}</tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Recent Signals (last 40){' — all' if show_all else ''} &nbsp;<a href="{_qs(all="" if show_all else "1")}" class="small">[{'hide chatter' if show_all else 'show all'}]</a></div>
      <table>
        <thead><tr><th>Time</th><th>Analyst</th><th>Symbol</th><th>Side</th><th>Outcome</th><th>Message</th></tr></thead>
        <tbody>{sig_rows()}</tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Log Tail (last {LOG_LINES} lines)</div>
      <div class="log-box" id="log">{log_tail}</div>
    </div>

  </div>
  <script>
    // Scroll log to bottom on load
    var lb = document.getElementById('log');
    if (lb) lb.scrollTop = lb.scrollHeight;
  </script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def start(state: dict, port: int = 5050):
    """Start the dashboard in a background daemon thread."""
    global _state
    _state = state
    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False),
        daemon=True,
        name="dashboard",
    )
    t.start()
