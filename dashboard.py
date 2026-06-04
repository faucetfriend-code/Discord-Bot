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

from flask import Flask, Response, jsonify, request

DB_PATH   = Path(__file__).parent / "bot.db"
LOG_PATH  = Path(__file__).parent / "bot.log"
LOG_LINES = 80

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
# Data helpers
# ---------------------------------------------------------------------------

def _read_log_tail() -> str:
    """Return the last LOG_LINES lines of bot.log as a single string."""
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-LOG_LINES:])
    except Exception:
        return "(log unavailable)"


def _read_positions() -> list[dict]:
    """Open positions with their scale-out progress and mark-to-market PnL."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT symbol, side, entry, sl, tp, size, order_id, opened_at, "
            "analyst, tps, tps_hit, last_price, unrealized_pnl "
            "FROM positions WHERE status='open' ORDER BY id DESC"
        ).fetchall()
        con.close()
        out = []
        for r in rows:
            try:
                tps = json.loads(r[9]) if r[9] else []
            except (ValueError, TypeError):
                tps = []
            out.append({
                "symbol": r[0], "side": r[1], "entry": r[2], "sl": r[3], "tp": r[4],
                "size": r[5], "order_id": r[6], "opened_at": r[7], "analyst": r[8] or "",
                "tps": tps, "tps_hit": r[10] or 0, "last_price": r[11] or 0,
                "unrealized_pnl": r[12] or 0,
            })
        return out
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


def _read_analyst_stats() -> list[dict]:
    """Per-analyst/strategy leverage, win/loss tally and realized PnL, best PnL first."""
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
            wr = f"{(wins / total * 100):.0f}%" if total else "—"
            out.append({"analyst": r[0], "leverage": r[1], "wins": wins,
                        "losses": losses, "total": total, "win_rate": wr,
                        "realized_pnl": r[4] or 0})
        return out
    except Exception:
        return []


def _read_roster() -> list[dict]:
    """
    One card per signal source: every whitelisted analyst PLUS the strategy feeds
    (RSI Extreme, OracleAlgo). Merges current stats; sources with no trades yet
    still appear at their starting leverage.
    """
    raw = os.getenv("ANALYST_WHITELIST", "")
    analysts = [n.strip() for n in raw.split(",") if n.strip()]
    strategies = ["RSI Extreme", "OracleAlgo"]
    start_lev = int(os.getenv("LEVERAGE_START", "75"))
    stats = {a["analyst"]: a for a in _read_analyst_stats()}

    def card(name, kind):
        s = stats.get(name, {})
        wins, losses = s.get("wins", 0), s.get("losses", 0)
        total = wins + losses
        return {
            "name": name, "kind": kind, "closed": total, "wins": wins, "losses": losses,
            "win_rate": f"{(wins / total * 100):.0f}%" if total else "—",
            "leverage": s.get("leverage", start_lev),
            "realized_pnl": s.get("realized_pnl", 0),
        }

    roster = [card(n, "strategy") for n in strategies] + [card(n, "analyst") for n in analysts]
    # include any one-off analysts that traded but aren't in the whitelist
    known = set(strategies) | set(analysts)
    for name in stats:
        if name not in known:
            roster.append(card(name, "other"))
    # most active first, then by PnL
    roster.sort(key=lambda c: (c["closed"], c["realized_pnl"]), reverse=True)
    return roster


def _read_signals(limit: int = 40) -> list[dict]:
    """The most recent `limit` parsed signals with their outcomes."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT ts, analyst, symbol, side, outcome, raw_text "
            "FROM signals ORDER BY id DESC LIMIT ?", (limit,)
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
    """JSON snapshot of process state, open positions, analyst stats and recent signals."""
    return jsonify({
        "state": _state,
        "positions": _read_positions(),
        "analyst_stats": _read_analyst_stats(),
        "recent_signals": _read_signals(20),
    })


@app.route("/")
def index():
    """Render the full dashboard page (self-contained HTML + CSS, no external assets)."""
    s        = _state
    positions = _read_positions()
    signals  = _read_signals(40)
    analysts = _read_analyst_stats()
    roster   = _read_roster()
    watches  = _read_watches()
    state    = _read_strategy_state()
    log_tail = _read_log_tail()

    mode_badge  = '<span class="badge dry">DRY RUN</span>' if s.get("dry_run") else '<span class="badge live">LIVE</span>'
    chrome_icon = "✅" if s.get("chrome_connected") else "❌"
    discord_icon= "✅" if s.get("discord_tab") else "❌"
    running     = s.get("poll_count", 0) > 0

    # Aggregate PnL across strategies
    total_realized = sum(a.get("realized_pnl", 0) for a in analysts)
    total_unreal = sum(p.get("unrealized_pnl", 0) for p in positions)

    def _pnl_html(v):
        cls = "buy" if v >= 0 else "sell"
        return f'<span class="{cls}">${v:+,.2f}</span>'

    def _card_html(c):
        tag = ('<span class="tcard-tag strat">SIGNAL</span>' if c["kind"] == "strategy"
               else '<span class="tcard-tag">ANALYST</span>')
        pnl_cls = "buy" if c["realized_pnl"] >= 0 else "sell"
        return f"""<div class="tcard">
          <div class="tcard-name">{c['name']} {tag}</div>
          <div class="tcard-closed">{c['closed']} closed trade{'' if c['closed']==1 else 's'}</div>
          <div class="tcard-stats">
            <span title="win rate">{c['win_rate']}</span>
            <span class="sep">·</span>
            <span title="leverage">{c['leverage']}x</span>
            <span class="sep">·</span>
            <span class="{pnl_cls}" title="realized PnL">${c['realized_pnl']:+,.2f}</span>
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

    bias = state.get("btc_oracle_bias", "")
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
            html += f"""<tr>
              <td><b>{p['symbol']}</b><div class="small">{p['analyst']}</div></td>
              <td class="{side_cls}">{p['side'].upper()}</td>
              <td>{p['entry']}</td>
              <td>{p['sl']}</td>
              <td class="small">{', '.join(str(t) for t in p['tps']) if p['tps'] else p['tp']}</td>
              <td>{p['size']}</td>
              <td>{p['last_price'] or '—'}</td>
              <td>{_pnl_html(p['unrealized_pnl'])}</td>
              <td class="small">{prog}</td>
            </tr>"""
        return html

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

    {roster_rows()}

    <div class="status-bar">
      <div class="stat"><span class="label">Poll #</span><span class="value">{s.get('poll_count', 0)}</span></div>
      <div class="stat"><span class="label">Last Poll</span><span class="value">{_elapsed(s.get('last_poll_at'))}</span></div>
      <div class="stat"><span class="label">Last Signal</span><span class="value">{_elapsed(s.get('last_signal_at'))}</span></div>
      <div class="stat"><span class="label">Chrome</span><span class="value">{chrome_icon}</span></div>
      <div class="stat"><span class="label">Discord Tab</span><span class="value">{discord_icon}</span></div>
      <div class="stat"><span class="label">Realized PnL</span><span class="value">{_pnl_html(total_realized)}</span></div>
      <div class="stat"><span class="label">Unrealized PnL</span><span class="value">{_pnl_html(total_unreal)}</span></div>
    </div>

    <div class="status-bar">
      <div class="stat"><span class="label">RSI Extreme</span><span class="value" style="color:{_strat('RSI_EXTREME_ENABLED')[1]}">{_strat('RSI_EXTREME_ENABLED')[0]}</span></div>
      <div class="stat"><span class="label">OracleAlgo</span><span class="value" style="color:{_strat('ORACLEALGO_ENABLED')[1]}">{_strat('ORACLEALGO_ENABLED')[0]}</span></div>
      <div class="stat"><span class="label">BTC 4H Bias</span><span class="value">{bias_html}</span></div>
      <div class="stat"><span class="label">Risk / Trade</span><span class="value">{float(os.getenv('RISK_PCT','0.01'))*100:.1f}%</span></div>
      <div class="stat"><span class="label">Max Positions</span><span class="value">{os.getenv('MAX_OPEN_POSITIONS','3')}</span></div>
      <div class="stat"><span class="label">Vision Model</span><span class="value small">{os.getenv('LOCAL_VISION_MODEL','(none)')}</span></div>
    </div>

    <div class="card">
      <div class="card-title">Open Positions ({len(positions)})</div>
      <table>
        <thead><tr><th>Symbol / Strategy</th><th>Side</th><th>Entry</th><th>SL</th><th>TPs</th><th>Size</th><th>Last</th><th>Unreal. PnL</th><th>Progress</th></tr></thead>
        <tbody>{pos_rows()}</tbody>
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
      <div class="card-title">Analyst &amp; Strategy Performance ({len(analysts)})</div>
      <table>
        <thead><tr><th>Analyst / Strategy</th><th>Leverage (50–125x)</th><th>Wins</th><th>Losses</th><th>Win&nbsp;Rate</th><th>Realized&nbsp;PnL</th></tr></thead>
        <tbody>{analyst_rows()}</tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Recent Signals (last 40)</div>
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
