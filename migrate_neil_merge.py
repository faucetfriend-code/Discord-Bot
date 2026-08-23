"""One-shot migration: merge "Neilarora Alerts" into "Nurse-Neil Alerts".

The two keys are the same human. "Neilarora Alerts" is the Discord role-mention
string; "Nurse-Neil Alerts" is the real display name. Attribution split across
both because _canonical_analyst matches message CONTENT, so any post carrying
the "@Neilarora Alerts" mention resolved to the role name - including posts by
other authors (Unity Academy engagement spam).

This matters beyond display: position_tracker.get_analyst_leverage reads
analyst_stats to size orders, so a split row sizes trades off a partial record.

STOP bot.py AND watchdog.py BEFORE RUNNING. The bot marks positions to market
every few seconds; migrating underneath it races with position settlement.

Back up bot.db first. This destroys realized_pnl and ledger rows for the stale
key - there is no undo without the backup.

Usage:
    python migrate_neil_merge.py --dry-run     # show what would change
    python migrate_neil_merge.py --yes         # apply
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")
STALE = "Neilarora Alerts"
KEEP = "Nurse-Neil Alerts"

# Tables where the column holds an attribution key and must be rewritten.
# NOTE: signals.raw_text also contains the literal "@Neilarora Alerts" mention
# text. That is message content, not attribution - it must NOT be rewritten.
KEY_COLUMNS = [
    ("trades", "analyst"),
    ("positions", "analyst"),
    ("signals", "analyst"),
    ("watches", "analyst"),
    ("bot_ledger", "bot_key"),
]


def replay_leverage(con) -> int:
    """Recompute the merged analyst's leverage by replaying its outcomes.

    Leverage is path-dependent state produced by record_outcome (+step per win,
    -step per loss, clamped). It is NOT additive, so summing or max()-ing the
    two rows would hand out leverage that was never earned: max(55, 85) = 85
    credits three wins' worth to a 1W/2L record. The only defensible value is
    to replay the merged sequence from LEVERAGE_START.
    """
    start = int(os.getenv("LEVERAGE_START", "75"))
    lo = int(os.getenv("LEVERAGE_MIN", "50"))
    hi = int(os.getenv("LEVERAGE_MAX", "125"))
    step = int(os.getenv("LEVERAGE_STEP", "10"))
    rows = con.execute(
        "SELECT won FROM trades WHERE analyst IN (?, ?) ORDER BY closed_at",
        (STALE, KEEP),
    ).fetchall()
    lev = start
    for (won,) in rows:
        lev = max(lo, min(hi, lev + (step if won else -step)))
    return lev


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="apply the migration")
    ap.add_argument("--dry-run", action="store_true", help="report only")
    args = ap.parse_args()
    if not (args.yes or args.dry_run):
        print("refusing to run without --yes or --dry-run")
        return 2

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    stale = con.execute(
        "SELECT * FROM analyst_stats WHERE analyst=?", (STALE,)
    ).fetchone()
    if stale is None:
        print(f"nothing to do: no analyst_stats row for {STALE!r}")
        return 0
    keep = con.execute(
        "SELECT * FROM analyst_stats WHERE analyst=?", (KEEP,)
    ).fetchone()
    if keep is None:
        print(f"ABORT: no analyst_stats row for {KEEP!r} to merge into")
        return 1

    # An open position on the stale key would be rewritten mid-flight; the bot
    # must settle or close it first.
    open_stale = con.execute(
        "SELECT COUNT(*) FROM positions WHERE analyst=? AND status='open'", (STALE,)
    ).fetchone()[0]
    if open_stale:
        print(f"ABORT: {open_stale} OPEN position(s) still on {STALE!r}")
        return 1

    wins = (stale["wins"] or 0) + (keep["wins"] or 0)
    losses = (stale["losses"] or 0) + (keep["losses"] or 0)
    pnl = (stale["realized_pnl"] or 0) + (keep["realized_pnl"] or 0)
    lev = replay_leverage(con)

    print(f"  {STALE:20} {stale['wins']}W/{stale['losses']}L "
          f"pnl={stale['realized_pnl']:+.6f} lev={stale['leverage']}")
    print(f"  {KEEP:20} {keep['wins']}W/{keep['losses']}L "
          f"pnl={keep['realized_pnl']:+.6f} lev={keep['leverage']}")
    print(f"  MERGED -> {wins}W/{losses}L pnl={pnl:+.6f} lev={lev} "
          f"(replayed, not max())")
    for table, col in KEY_COLUMNS:
        n = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (STALE,)
        ).fetchone()[0]
        print(f"  {table}.{col}: {n} row(s) to rekey")

    if args.dry_run:
        print("\ndry run - nothing written")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    try:
        con.execute("BEGIN IMMEDIATE")
        for table, col in KEY_COLUMNS:
            con.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (KEEP, STALE))
        # analyst is the PRIMARY KEY, so a plain UPDATE would raise a UNIQUE
        # violation. Merge into the survivor, then drop the stale row.
        con.execute(
            "UPDATE analyst_stats SET wins=?, losses=?, realized_pnl=?, "
            "leverage=?, updated_at=? WHERE analyst=?",
            (wins, losses, pnl, lev, now, KEEP),
        )
        con.execute("DELETE FROM analyst_stats WHERE analyst=?", (STALE,))
        # Paper balances compound on balance, so summing them is wrong. Clear
        # the ledger and let _backfill_bot replay the merged trade history.
        con.execute("DELETE FROM bot_ledger WHERE bot_key IN (?, ?)", (STALE, KEEP))
        con.execute("DELETE FROM bots WHERE key=?", (STALE,))
        con.execute(
            "UPDATE bots SET balance=start_balance WHERE key=?", (KEEP,)
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    con.close()

    import accounts
    accounts._backfill_bot(KEEP)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM analyst_stats WHERE analyst=?", (KEEP,)).fetchone()
    bot = con.execute("SELECT balance FROM bots WHERE key=?", (KEEP,)).fetchone()
    leftovers = sum(
        con.execute(f"SELECT COUNT(*) FROM {t} WHERE {c}=?", (STALE,)).fetchone()[0]
        for t, c in KEY_COLUMNS
    )
    leftovers += con.execute(
        "SELECT COUNT(*) FROM analyst_stats WHERE analyst=?", (STALE,)
    ).fetchone()[0]
    leftovers += con.execute(
        "SELECT COUNT(*) FROM bots WHERE key=?", (STALE,)
    ).fetchone()[0]
    print(f"\nafter: {KEEP} {row['wins']}W/{row['losses']}L "
          f"pnl={row['realized_pnl']:+.6f} lev={row['leverage']} "
          f"paper_balance={bot['balance']:.3f}")
    print(f"stale key rows remaining (excl. raw_text mentions): {leftovers}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
