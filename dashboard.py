"""
Web dashboard for the Discord Signal Bot.
Runs on http://localhost:5050 in a background daemon thread.
Auto-refreshes every 15 seconds.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify

DB_PATH   = Path(__file__).parent / "bot.db"
LOG_PATH  = Path(__file__).parent / "bot.log"
LOG_LINES = 80

app = Flask(__name__)
_state: dict = {}

# Suppress Flask/Werkzeug request logs from polluting bot.log
logging.getLogger("werkzeug").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _read_log_tail() -> str:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-LOG_LINES:])
    except Exception:
        return "(log unavailable)"


def _read_positions() -> list[dict]:
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT symbol, side, entry, sl, tp, size, order_id, opened_at "
            "FROM positions WHERE status='open' ORDER BY id DESC"
        ).fetchall()
        con.close()
        return [
            {"symbol": r[0], "side": r[1], "entry": r[2], "sl": r[3],
             "tp": r[4], "size": r[5], "order_id": r[6], "opened_at": r[7]}
            for r in rows
        ]
    except Exception:
        return []


def _read_analyst_stats() -> list[dict]:
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT analyst, leverage, wins, losses FROM analyst_stats "
            "ORDER BY leverage DESC, wins DESC, analyst"
        ).fetchall()
        con.close()
        out = []
        for r in rows:
            wins, losses = r[2] or 0, r[3] or 0
            total = wins + losses
            wr = f"{(wins / total * 100):.0f}%" if total else "—"
            out.append({"analyst": r[0], "leverage": r[1], "wins": wins,
                        "losses": losses, "total": total, "win_rate": wr})
        return out
    except Exception:
        return []


def _read_signals(limit: int = 40) -> list[dict]:
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
    return jsonify({
        "state": _state,
        "positions": _read_positions(),
        "analyst_stats": _read_analyst_stats(),
        "recent_signals": _read_signals(20),
    })


@app.route("/")
def index():
    s        = _state
    positions = _read_positions()
    signals  = _read_signals(40)
    analysts = _read_analyst_stats()
    log_tail = _read_log_tail()

    mode_badge  = '<span class="badge dry">DRY RUN</span>' if s.get("dry_run") else '<span class="badge live">LIVE</span>'
    chrome_icon = "✅" if s.get("chrome_connected") else "❌"
    discord_icon= "✅" if s.get("discord_tab") else "❌"
    running     = s.get("poll_count", 0) > 0

    def pos_rows():
        if not positions:
            return '<tr><td colspan="8" class="empty">No open positions</td></tr>'
        html = ""
        for p in positions:
            side_cls = "buy" if p["side"] == "buy" else "sell"
            html += f"""<tr>
              <td><b>{p['symbol']}</b></td>
              <td class="{side_cls}">{p['side'].upper()}</td>
              <td>{p['entry']}</td>
              <td>{p['sl']}</td>
              <td>{p['tp']}</td>
              <td>{p['size']}</td>
              <td class="mono small">{p['order_id'][:16]}…</td>
              <td class="small">{p['opened_at'][:19]}</td>
            </tr>"""
        return html

    def analyst_rows():
        if not analysts:
            return '<tr><td colspan="5" class="empty">No analyst trades resolved yet</td></tr>'
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

    <div class="status-bar">
      <div class="stat"><span class="label">Poll #</span><span class="value">{s.get('poll_count', 0)}</span></div>
      <div class="stat"><span class="label">Last Poll</span><span class="value">{_elapsed(s.get('last_poll_at'))}</span></div>
      <div class="stat"><span class="label">Notifications Found</span><span class="value">{s.get('last_poll_found', 0)}</span></div>
      <div class="stat"><span class="label">Last Signal</span><span class="value">{_elapsed(s.get('last_signal_at'))}</span></div>
      <div class="stat"><span class="label">Chrome</span><span class="value">{chrome_icon}</span></div>
      <div class="stat"><span class="label">Discord Tab</span><span class="value">{discord_icon}</span></div>
      <div class="stat"><span class="label">Uptime Since</span><span class="value small">{(s.get('started_at') or '—')[:19]}</span></div>
    </div>

    <div class="card">
      <div class="card-title">Open Positions ({len(positions)})</div>
      <table>
        <thead><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Size</th><th>Order ID</th><th>Opened</th></tr></thead>
        <tbody>{pos_rows()}</tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Analyst Leverage &amp; Performance ({len(analysts)})</div>
      <table>
        <thead><tr><th>Analyst</th><th>Leverage (50–125x)</th><th>Wins</th><th>Losses</th><th>Win&nbsp;Rate</th></tr></thead>
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
