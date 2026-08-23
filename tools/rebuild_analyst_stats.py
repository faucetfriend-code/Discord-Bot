"""Rebuild analyst_stats from the trades blotter by replaying the leverage ladder.

analyst_stats (wins, losses, leverage) is path-dependent state that drifts from
the trades table whenever a settlement is recorded in one place but not the
other (crashes mid-settle, manual edits, stale/merged analyst keys). Leverage is
NOT additive - +LEVERAGE_STEP per win, -LEVERAGE_STEP per loss, clamped to
[LEVERAGE_MIN, LEVERAGE_MAX] - so the only defensible rebuild is to replay each
analyst's outcomes in closed_at order from LEVERAGE_START
(position_tracker.replay_ladder, the same rule record_outcome applies live).

`won` is taken from the trades row as stored. Pass --won-from-pnl to instead
derive it as net_pnl > 0 (position_tracker.derive_won), which is what every
live settlement path now does; use it to also repair rows written before that
fix. realized_pnl is rebuilt as SUM(net_pnl) per analyst.

Usage:
    python tools/rebuild_analyst_stats.py                 # dry run (default)
    python tools/rebuild_analyst_stats.py --won-from-pnl  # dry run, derived won
    python tools/rebuild_analyst_stats.py --apply         # backup + write

--apply copies bot.db to bot.db.bak-<YYYYmmdd-HHMMSS>-statsrebuild first and
writes every analyst row in a single transaction. Rows in analyst_stats with no
trades are reset to LEVERAGE_START / 0 / 0 (never deleted - the bot may be
holding an open position for them). The running bot must be stopped first.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values  # noqa: E402

import position_tracker as pt  # noqa: E402

LEVERAGE_KEYS = ("LEVERAGE_START", "LEVERAGE_MIN", "LEVERAGE_MAX", "LEVERAGE_STEP")
DEFAULTS = {"LEVERAGE_START": 75, "LEVERAGE_MIN": 50,
            "LEVERAGE_MAX": 125, "LEVERAGE_STEP": 10}


def load_leverage_params(env_path: Path) -> dict[str, int]:
    """Read only the ladder keys from .env (dotenv_values - never load_dotenv,
    so nothing leaks into os.environ). Missing keys fall back to the bot's
    defaults in bot._leverage_params."""
    values = dotenv_values(env_path) if env_path.exists() else {}
    params = {}
    for key in LEVERAGE_KEYS:
        raw = values.get(key)
        params[key] = int(raw) if raw not in (None, "") else DEFAULTS[key]
    return params


def read_current(con: sqlite3.Connection) -> dict[str, dict]:
    rows = con.execute(
        "SELECT analyst, leverage, wins, losses, COALESCE(realized_pnl, 0) "
        "FROM analyst_stats"
    ).fetchall()
    return {r[0]: {"leverage": r[1], "wins": r[2], "losses": r[3],
                   "realized_pnl": r[4]} for r in rows}


def read_outcomes(con: sqlite3.Connection, won_from_pnl: bool) -> dict[str, dict]:
    """Per analyst: ordered won list + summed net_pnl, straight from trades."""
    rows = con.execute(
        "SELECT analyst, won, COALESCE(net_pnl, 0) FROM trades "
        "ORDER BY analyst, closed_at, id"
    ).fetchall()
    out: dict[str, dict] = {}
    for analyst, won, net in rows:
        entry = out.setdefault(analyst, {"outcomes": [], "net_pnl": 0.0})
        entry["outcomes"].append(pt.derive_won(net) if won_from_pnl else bool(won))
        entry["net_pnl"] += float(net)
    return out


def rebuild(current: dict, outcomes: dict, params: dict) -> dict[str, dict]:
    """Replay every analyst (union of both tables) and return the target rows."""
    target = {}
    for analyst in sorted(set(current) | set(outcomes)):
        data = outcomes.get(analyst, {"outcomes": [], "net_pnl": 0.0})
        replayed = pt.replay_ladder(
            data["outcomes"], params["LEVERAGE_START"], params["LEVERAGE_MIN"],
            params["LEVERAGE_MAX"], params["LEVERAGE_STEP"],
        )
        replayed["realized_pnl"] = round(data["net_pnl"], 6)
        target[analyst] = replayed
    return target


def print_table(current: dict, target: dict) -> int:
    """Print current vs rebuilt per analyst; return the number of drifted rows."""
    hdr = (f"{'analyst':<20} {'W cur':>5} {'W new':>5} {'L cur':>5} {'L new':>5} "
           f"{'lev cur':>7} {'lev new':>7} {'pnl cur':>10} {'pnl new':>10}  flag")
    print(hdr)
    print("-" * len(hdr))
    drift = 0
    for analyst, new in target.items():
        cur = current.get(analyst, {"leverage": None, "wins": None,
                                    "losses": None, "realized_pnl": None})
        changed = any(cur[k] != new[k] for k in ("wins", "losses", "leverage")) \
            or abs((cur["realized_pnl"] or 0.0) - new["realized_pnl"]) > 0.005
        drift += 1 if changed else 0
        flag = "DRIFT" if changed else "ok"
        if cur["leverage"] is None:
            flag = "NEW"
        fmt = lambda v: "-" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))
        print(f"{analyst:<20} {fmt(cur['wins']):>5} {new['wins']:>5} "
              f"{fmt(cur['losses']):>5} {new['losses']:>5} "
              f"{fmt(cur['leverage']):>7} {new['leverage']:>7} "
              f"{fmt(cur['realized_pnl']):>10} {new['realized_pnl']:>10.2f}  {flag}")
    return drift


def apply(db_path: Path, target: dict) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak-{stamp}-statsrebuild")
    shutil.copy2(db_path, backup)
    con = sqlite3.connect(db_path)
    now = datetime.now().astimezone().isoformat()
    try:
        with con:  # one transaction; rolled back on any exception
            for analyst, row in target.items():
                con.execute(
                    "INSERT INTO analyst_stats "
                    "(analyst, leverage, wins, losses, realized_pnl, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(analyst) DO UPDATE SET leverage=excluded.leverage, "
                    "wins=excluded.wins, losses=excluded.losses, "
                    "realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at",
                    (analyst, row["leverage"], row["wins"], row["losses"],
                     row["realized_pnl"], now),
                )
    finally:
        con.close()
    return backup


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="back up bot.db and write the rebuilt rows")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="report only (default)")
    ap.add_argument("--won-from-pnl", action="store_true",
                    help="derive won as net_pnl > 0 instead of trusting trades.won")
    ap.add_argument("--db", default=str(pt.DB_PATH), help="path to bot.db")
    ap.add_argument("--env", default=str(ROOT / ".env"), help="path to .env")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"no database at {db_path}")
        return 2

    params = load_leverage_params(Path(args.env))
    print("ladder params: " + ", ".join(f"{k}={params[k]}" for k in LEVERAGE_KEYS))
    print(f"won source: {'net_pnl > 0' if args.won_from_pnl else 'trades.won as stored'}")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        current = read_current(con)
        outcomes = read_outcomes(con, args.won_from_pnl)
    finally:
        con.close()
    target = rebuild(current, outcomes, params)
    drift = print_table(current, target)
    print(f"\n{drift} of {len(target)} analyst rows differ from the replay")

    if not args.apply:
        print("dry run - nothing written (pass --apply to rebuild)")
        return 0
    backup = apply(db_path, target)
    print(f"backup written to {backup}")
    print(f"analyst_stats rebuilt for {len(target)} analysts in one transaction")
    return 0


if __name__ == "__main__":
    sys.exit(main())
