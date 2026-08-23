"""
One-shot recovery for shadow_outcomes.csv.

analyze_shadow.py used to rewrite shadow_outcomes.csv from scratch on every
run (mode "w"). Gate.io only retains ~34 days of 5m candles, so any row
older than that re-simulates to "no_data" and its previously-computed real
outcome gets DESTROYED. This script reconstructs the best-known outcome for
every row still present in the current shadow_outcomes.csv by scanning the
file's git history and, per row, keeping whichever version of its outcome
columns is the most informative (see the precedence ladder below).

It only ever touches the 11 outcome columns; the 19 shadow (signal) columns
are copied through unchanged from the current file, and row order/row set
are preserved exactly as-is (rows found only in history are reported as
orphans and dropped, not appended).

Usage:  python recover_shadow.py [--dry-run] [--out shadow_outcomes.csv]
"""

import argparse
import csv
import io
import os
import subprocess
import sys
import tempfile
from collections import Counter

CSV_PATH = "shadow_outcomes.csv"

# The last 11 columns of the 30-column header. Always copied/preserved
# together as one unit -- they are all derived from the same candle fetch.
OUTCOME_COLUMNS = [
    "outcome", "ret_pct", "r_mult", "mfe_pct", "mae_pct", "hours",
    "confirm_fired", "confirm_outcome", "confirm_r", "alt_outcome", "alt_r",
]

# Informativeness ladder (higher wins). A more-informative outcome must
# never be overwritten by a less-informative one.
TERMINAL_OUTCOMES = {"loss_sl", "tp_then_be", "win_full"}


def outcome_rank(outcome) -> int:
    """Rank an outcome value on the informativeness ladder (higher = better).

    2 = terminal (loss_sl, tp_then_be, win_full)
    1 = open
    0 = no_data, no_entry_price, empty/missing
    """
    if outcome in TERMINAL_OUTCOMES:
        return 2
    if outcome == "open":
        return 1
    return 0


def row_key(row: dict):
    """Return the (ts, symbol) identity key for a shadow row."""
    return (row.get("ts", ""), row.get("symbol", ""))


def extract_outcome(row: dict) -> dict:
    """Pull just the 11 outcome columns out of a row dict.

    Older CSV schemas (early revisions) may be missing some of these
    columns entirely (e.g. alt_outcome/alt_r were added later); treat
    those as empty, which ranks lowest and can never win a tie.
    """
    return {col: row.get(col) or "" for col in OUTCOME_COLUMNS}


def list_revisions() -> list:
    """Newest-to-oldest commit SHAs that touched shadow_outcomes.csv."""
    result = subprocess.run(
        ["git", "log", "--format=%H", "--", CSV_PATH],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    )
    return [sha for sha in result.stdout.splitlines() if sha.strip()]


def read_revision(sha: str) -> list:
    """Rows of shadow_outcomes.csv as of commit sha, or [] if unavailable."""
    proc = subprocess.run(
        ["git", "show", f"{sha}:{CSV_PATH}"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    return list(csv.DictReader(io.StringIO(proc.stdout)))


def build_best_map(sources: list, current_keys: set):
    """Choose the best outcome-columns set for every key in current_keys.

    sources is ordered [("working-copy", rows), (sha, rows), ...] with git
    revisions newest -> oldest. Within that ordering, the first source that
    provides a strictly-more-informative outcome than what's already chosen
    wins -- so ties prefer the earlier (fresher) source and only a strict
    improvement causes an upgrade.

    Returns (best, orphan_keys) where best[key] = (outcome_dict, label).
    """
    best: dict = {}
    orphan_keys = set()
    for label, rows in sources:
        for row in rows:
            key = row_key(row)
            if key not in current_keys:
                if label != "working-copy":
                    orphan_keys.add(key)
                continue
            outc = extract_outcome(row)
            rank = outcome_rank(outc["outcome"])
            prior = best.get(key)
            if prior is None or rank > prior[2]:
                best[key] = (outc, label, rank)
    return best, orphan_keys


def terminal_counts_by_decision(rows, outcome_getter) -> Counter:
    """Count rows with a terminal outcome, bucketed by the decision column."""
    counts = Counter()
    for r in rows:
        if outcome_rank(outcome_getter(r)) == 2:
            counts[r.get("decision") or "?"] += 1
    return counts


def enter_rows_with_r_mult(rows) -> int:
    """Count decision=='enter' rows that carry a non-blank r_mult."""
    return sum(
        1 for r in rows
        if r.get("decision") == "enter" and str(r.get("r_mult") or "").strip()
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                     help="report only, write nothing")
    ap.add_argument("--out", default=CSV_PATH,
                     help="output path (default: shadow_outcomes.csv)")
    args = ap.parse_args()

    if not os.path.exists(CSV_PATH):
        print(f"{CSV_PATH} not found", file=sys.stderr)
        sys.exit(1)

    current_rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    if not current_rows:
        print(f"{CSV_PATH} is empty", file=sys.stderr)
        sys.exit(1)
    fields = list(current_rows[0].keys())
    current_keys = {row_key(r) for r in current_rows}

    sources = [("working-copy", current_rows)]
    for sha in list_revisions():
        sources.append((sha[:10], read_revision(sha)))

    best, orphan_keys = build_best_map(sources, current_keys)

    out_rows = []
    upgraded = 0
    for row in current_rows:
        key = row_key(row)
        chosen_outc, label, _rank = best[key]
        out = dict(row)
        if label != "working-copy":
            upgraded += 1
        out.update(chosen_outc)
        out_rows.append(out)

    # ---- summary ---------------------------------------------------------
    print("=== rows scanned per source ===")
    for label, rows in sources:
        print(f"  {label:14s} {len(rows)} rows")

    print(f"\nrows upgraded from history: {upgraded} / {len(out_rows)}")
    print(f"orphan (ts, symbol) keys found in history but not in current "
          f"file: {len(orphan_keys)}")

    before_by_decision = terminal_counts_by_decision(
        current_rows, lambda r: r.get("outcome"))
    after_by_decision = terminal_counts_by_decision(
        out_rows, lambda r: r.get("outcome"))
    before_total = sum(before_by_decision.values())
    after_total = sum(after_by_decision.values())

    print(f"\n=== usable (terminal) rows: {before_total} -> {after_total} ===")
    all_decisions = sorted(set(before_by_decision) | set(after_by_decision))
    for d in all_decisions:
        print(f"  {d:16s} {before_by_decision.get(d, 0):4d} -> "
              f"{after_by_decision.get(d, 0):4d}")

    before_enter_r = enter_rows_with_r_mult(current_rows)
    after_enter_r = enter_rows_with_r_mult(out_rows)
    print(f"\ndecision=='enter' rows with an r_mult: "
          f"{before_enter_r} -> {after_enter_r}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    out_dir = os.path.dirname(os.path.abspath(args.out)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(out_rows)
        os.replace(tmp_path, args.out)
    except OSError:
        os.remove(tmp_path)
        raise

    print(f"\nwrote {args.out} ({len(out_rows)} rows)")


if __name__ == "__main__":
    main()
