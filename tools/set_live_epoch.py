"""Record the live epoch in bot.db and relabel mislabelled paper trades.

Writes two rows into a small meta(key, value) table:
    live_epoch_start   ISO-8601 timestamp of the paper -> live switch
    live_start_balance exchange balance (USDT) at that moment

and optionally flips dry_run=1 on trade ids that were settled as paper
positions but recorded with dry_run=0 (--relabel-paper 142,143). Only rows
whose positions row has a DRYRUN- order id are touched; anything else is
refused, so a real trade can never be hidden from the live book.

Usage:
    python tools/set_live_epoch.py --epoch 2026-08-23T11:00:36-05:00 \
        --balance 1494.08 --relabel-paper 142,143            # dry run
    python tools/set_live_epoch.py ... --apply                # backup + write

--apply copies bot.db to bot.db.bak-<YYYYmmdd-HHMMSS>-epoch first, then runs
everything in one short transaction (safe while the bot is running: the lock
is held for milliseconds). No rows are ever deleted.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import live_epoch  # noqa: E402

DB_PATH = ROOT / "bot.db"


def _parse_ids(raw: str) -> list[int]:
    return [int(x) for x in raw.split(",") if x.strip()]


def plan_relabel(con: sqlite3.Connection, ids: list[int]) -> tuple[list[int], list[str]]:
    """Return (ids safe to relabel, reasons for any refused id)."""
    ok, refused = [], []
    for tid in ids:
        row = con.execute(
            "SELECT symbol, analyst, closed_at, dry_run FROM trades WHERE id=?", (tid,)
        ).fetchone()
        if row is None:
            refused.append(f"trade {tid}: not found")
            continue
        if int(row[3] or 0) == 1:
            refused.append(f"trade {tid}: already dry_run=1")
            continue
        # The trades row has no FK to positions; match on symbol/analyst with a
        # DRYRUN- order id closed within a minute of the trade.
        pos = con.execute(
            "SELECT id, order_id FROM positions WHERE symbol=? AND analyst=? "
            "AND status!='open' AND order_id LIKE 'DRYRUN-%' ORDER BY id DESC LIMIT 1",
            (row[0], row[1]),
        ).fetchone()
        if pos is None:
            refused.append(f"trade {tid}: no closed DRYRUN- position for {row[0]}/{row[1]}")
            continue
        ok.append(tid)
    return ok, refused


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--epoch", required=True, help="ISO-8601 live epoch, with offset")
    ap.add_argument("--balance", type=float, required=True, help="live start balance")
    ap.add_argument("--relabel-paper", default="", metavar="IDS",
                    help="comma-separated trade ids to set dry_run=1")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    if live_epoch.parse_ts(args.epoch) is None:
        print(f"ERROR: cannot parse --epoch {args.epoch!r}")
        return 2
    ids = _parse_ids(args.relabel_paper)
    db = Path(args.db)

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    current = live_epoch.read_meta(con)
    ok_ids, refused = plan_relabel(con, ids)
    con.close()

    print(f"db: {db}")
    print(f"meta now: {current or '(no meta table)'}")
    print(f"set {live_epoch.META_EPOCH_KEY} = {args.epoch}")
    print(f"set {live_epoch.META_BALANCE_KEY} = {args.balance:.2f}")
    for r in refused:
        print(f"REFUSED {r}")
    if ok_ids:
        print(f"UPDATE trades SET dry_run=1 WHERE id IN ({','.join(map(str, ok_ids))})")
    if not args.apply:
        print("dry run - pass --apply to write")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db.with_name(f"{db.name}.bak-{stamp}-epoch")
    shutil.copy2(db, backup)
    print(f"backup: {backup}")

    con = sqlite3.connect(str(db), timeout=30)
    try:
        with con:
            con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        (live_epoch.META_EPOCH_KEY, args.epoch))
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        (live_epoch.META_BALANCE_KEY, f"{args.balance:.2f}"))
            if ok_ids:
                marks = ",".join("?" * len(ok_ids))
                cur = con.execute(
                    f"UPDATE trades SET dry_run=1 WHERE id IN ({marks}) AND dry_run=0",
                    ok_ids,
                )
                print(f"relabelled {cur.rowcount} trade row(s)")
    finally:
        con.close()
    print("applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
