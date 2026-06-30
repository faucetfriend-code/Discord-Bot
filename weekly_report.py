"""
Weekly per-source performance report (read-only).

Turns the manual "which signal sources made or lost money this week" sweep into
one command. Reads the existing tables in bot.db -- no schema changes, no writes:

  trades        -> per-source net PnL, win/loss, reasons (filtered by closed_at)
  bot_ledger    -> per-source R-multiple total in the window (filtered by ts)
  analyst_stats -> current adaptive leverage per source
  bots          -> current (all-time compounded) paper balance per source

Usage:
  python weekly_report.py                     # last 7 days
  python weekly_report.py --since 2026-06-22  # explicit start (YYYY-MM-DD)
  python weekly_report.py --since 2026-06-01 --until 2026-06-15
  python weekly_report.py --days 30           # last N days

Dates are compared on the YYYY-MM-DD prefix of the stored ISO timestamps, so the
local timezone offset stored on each row does not affect bucketing.
"""

import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot.db"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Per-source trading performance report")
    p.add_argument("--since", help="Start date YYYY-MM-DD (inclusive)")
    p.add_argument("--until", help="End date YYYY-MM-DD (inclusive)")
    p.add_argument("--days", type=int, default=7,
                   help="Window size in days when --since is omitted (default 7)")
    return p.parse_args()


def _fmt_money(v: float) -> str:
    return f"{v:+.2f}"


def _fmt_r(v: float) -> str:
    return f"{v:+.2f}"


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def main() -> None:
    args = _parse_args()
    since = args.since or _days_ago(args.days)
    until = args.until or _today()

    if not DB_PATH.exists():
        print(f"No database found at {DB_PATH}")
        return

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Per-source trade aggregates within the window (by closed_at date prefix).
    trade_rows = con.execute(
        """
        SELECT analyst,
               COUNT(*)                              AS trades,
               COALESCE(SUM(won), 0)                 AS wins,
               ROUND(COALESCE(SUM(net_pnl), 0), 2)   AS net_pnl,
               ROUND(COALESCE(SUM(gross_pnl), 0), 2) AS gross_pnl,
               ROUND(COALESCE(SUM(fees), 0), 2)      AS fees
        FROM trades
        WHERE substr(closed_at, 1, 10) >= ?
          AND substr(closed_at, 1, 10) <= ?
        GROUP BY analyst
        """,
        (since, until),
    ).fetchall()

    # Per-source R-multiple total within the window (bot_ledger.ts).
    r_rows = con.execute(
        """
        SELECT bot_key,
               ROUND(COALESCE(SUM(r_mult), 0), 2) AS r_total,
               COUNT(*)                           AS ledger_trades
        FROM bot_ledger
        WHERE substr(ts, 1, 10) >= ?
          AND substr(ts, 1, 10) <= ?
        GROUP BY bot_key
        """,
        (since, until),
    ).fetchall()
    r_by_key = {row["bot_key"]: row for row in r_rows}

    # Current (not window-scoped) state: adaptive leverage + paper balance.
    lev_by_key = {
        row["analyst"]: row["leverage"]
        for row in con.execute("SELECT analyst, leverage FROM analyst_stats")
    }
    bal_by_key = {
        row["key"]: (row["balance"], row["start_balance"])
        for row in con.execute("SELECT key, balance, start_balance FROM bots")
    }

    # Reason breakdown within the window.
    reason_rows = con.execute(
        """
        SELECT reason,
               COUNT(*)                            AS n,
               ROUND(COALESCE(SUM(net_pnl), 0), 2) AS net_pnl
        FROM trades
        WHERE substr(closed_at, 1, 10) >= ?
          AND substr(closed_at, 1, 10) <= ?
        GROUP BY reason
        ORDER BY n DESC
        """,
        (since, until),
    ).fetchall()

    # Open positions snapshot (current, not window-scoped).
    open_rows = con.execute(
        """
        SELECT analyst, symbol, side, tps_hit, opened_at,
               ROUND(COALESCE(unrealized_pnl, 0), 2) AS upnl
        FROM positions
        WHERE status = 'open'
        ORDER BY opened_at
        """
    ).fetchall()

    con.close()

    print("=" * 72)
    print(f"PER-SOURCE PERFORMANCE REPORT   {since} .. {until}")
    print("=" * 72)

    # ---- Per-source table, sorted by window R then net PnL ----
    rows = []
    for tr in trade_rows:
        key = tr["analyst"]
        r = r_by_key.get(key)
        bal, start = bal_by_key.get(key, (None, None))
        rows.append({
            "key": key,
            "trades": tr["trades"],
            "wins": tr["wins"],
            "losses": tr["trades"] - tr["wins"],
            "win_rate": round(tr["wins"] / tr["trades"] * 100) if tr["trades"] else 0,
            "net_pnl": tr["net_pnl"],
            "fees": tr["fees"],
            "r_total": r["r_total"] if r else 0.0,
            "lev": lev_by_key.get(key, "-"),
            "balance": bal,
        })
    rows.sort(key=lambda x: (x["r_total"], x["net_pnl"]), reverse=True)

    _section(f"By source ({len(rows)} active in window)")
    if not rows:
        print("  No closed trades in this window.")
    else:
        hdr = f"  {'Source':<20}{'Trd':>4}{'W/L':>7}{'Win%':>6}{'NetPnL':>10}{'R':>8}{'Lev':>6}{'Bal':>9}"
        print(hdr)
        for x in rows:
            wl = f"{x['wins']}/{x['losses']}"
            bal = f"{x['balance']:.0f}" if x["balance"] is not None else "-"
            lev = f"{x['lev']}x" if x["lev"] != "-" else "-"
            print(f"  {x['key']:<20}{x['trades']:>4}{wl:>7}{x['win_rate']:>5}%"
                  f"{_fmt_money(x['net_pnl']):>10}{_fmt_r(x['r_total']):>8}{lev:>6}{bal:>9}")

        tot_trades = sum(x["trades"] for x in rows)
        tot_wins = sum(x["wins"] for x in rows)
        tot_net = sum(x["net_pnl"] for x in rows)
        tot_r = sum(x["r_total"] for x in rows)
        wr = round(tot_wins / tot_trades * 100) if tot_trades else 0
        print("  " + "-" * 68)
        print(f"  {'TOTAL':<20}{tot_trades:>4}{f'{tot_wins}/{tot_trades - tot_wins}':>7}"
              f"{wr:>5}%{_fmt_money(tot_net):>10}{_fmt_r(tot_r):>8}")

    # ---- Reason breakdown ----
    _section("Close reasons (window)")
    if not reason_rows:
        print("  none")
    else:
        for r in reason_rows:
            print(f"  {r['reason'] or '(none)':<18}{r['n']:>4} trades   net {_fmt_money(r['net_pnl'])}")

    # ---- Open positions ----
    _section(f"Open positions now ({len(open_rows)})")
    if not open_rows:
        print("  none")
    else:
        for p in open_rows:
            age_d = ""
            try:
                age = datetime.now().astimezone() - datetime.fromisoformat(p["opened_at"])
                age_d = f"{age.days}d"
            except Exception:
                age_d = "?"
            runner = " [runner@BE+]" if p["tps_hit"] >= 1 else ""
            print(f"  {p['symbol']:<12}{p['side']:<5}{p['analyst']:<18}"
                  f"age {age_d:<5}uPnL {_fmt_money(p['upnl'])}{runner}")

    print()


if __name__ == "__main__":
    main()
