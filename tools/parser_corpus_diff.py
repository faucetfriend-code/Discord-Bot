"""Regression harness for signal_parser.py changes.

Loads the committed parser (``git show <ref>:signal_parser.py``) and the
working-tree parser as two independent modules, stubs out every network /
LLM / vision call in both, replays every unique raw_text from signals_log.csv
through both ``parse()`` functions, and prints each row whose
(message_type, symbol, side, entry, sl, tp) tuple changed, plus per-analyst
flip counts.

Usage (from the repo root):

    python tools/parser_corpus_diff.py [--ref HEAD] [--csv signals_log.csv]
                                       [--verbose]

Exit code is 0 when there are no flips, 1 otherwise, so it can gate a change.
"""

from __future__ import annotations

import argparse
import collections
import csv
import importlib.util
import io
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
STUBS = ("_llm_parse", "_vision_fill", "_vision_parse")


def _load_module(name: str, source: str) -> types.ModuleType:
    """Exec ``source`` as a module named ``name`` with the repo on sys.path."""
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(REPO / "signal_parser.py")
    sys.modules[name] = mod
    exec(compile(source, f"<{name}>", "exec"), mod.__dict__)
    for fn in STUBS:
        if hasattr(mod, fn):
            setattr(mod, fn, _stub_for(fn))
    return mod


def _stub_for(fn: str):
    if fn == "_vision_fill":
        return lambda sig, msg: sig
    return lambda *a, **k: None


def _git_show(ref: str) -> str:
    out = subprocess.run(
        ["git", "show", f"{ref}:signal_parser.py"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    return out.decode("utf-8")


def _key(sig: Any) -> tuple:
    if sig is None:
        return ("none", "", "", None, None, None)
    return (
        sig.message_type.value,
        sig.symbol,
        sig.side,
        sig.entry,
        sig.sl,
        sig.tp,
    )


def _load_rows(csv_path: Path) -> list[dict]:
    """Unique (author, raw_text) rows, first-seen order."""
    seen: set = set()
    rows: list[dict] = []
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            text = r.get("raw_text") or ""
            author = r.get("analyst") or r.get("author") or ""
            k = (author, text)
            if not text or k in seen:
                continue
            seen.add(k)
            rows.append({"analyst": author, "text": text})
    return rows


def run(ref: str, csv_path: Path, verbose: bool) -> int:
    old = _load_module("signal_parser_old", _git_show(ref))
    new_src = (REPO / "signal_parser.py").read_text(encoding="utf-8")
    new = _load_module("signal_parser_new", new_src)

    rows = _load_rows(csv_path)
    flips: list[tuple] = []
    per_analyst: collections.Counter = collections.Counter()
    transitions: collections.Counter = collections.Counter()

    for r in rows:
        msg = {
            "id": "x",
            "author": r["analyst"],
            "content": r["text"],
            "time": "",
            "image_url": "",
            "server": "",
        }
        a = _key(old.parse(dict(msg)))
        b = _key(new.parse(dict(msg)))
        if a != b:
            flips.append((r["analyst"], r["text"], a, b))
            per_analyst[r["analyst"]] += 1
            transitions[(r["analyst"], a[0], b[0])] += 1

    print(f"rows replayed: {len(rows)} unique raw_text  (ref={ref})")
    print(f"flips: {len(flips)}")
    for analyst, n in per_analyst.most_common():
        print(f"  {analyst!r}: {n}")
    print("transitions (analyst, old_type -> new_type):")
    for (analyst, ta, tb), n in transitions.most_common():
        print(f"  {analyst!r}: {ta} -> {tb}: {n}")
    if verbose or len(flips) <= 60:
        print("-" * 70)
        for analyst, text, a, b in flips:
            snippet = " ".join(text.split())[:110]
            print(f"[{analyst}] {snippet}")
            print(f"    old: {a}")
            print(f"    new: {b}")
    return 1 if flips else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", default="HEAD", help="git ref for the baseline parser")
    ap.add_argument("--csv", default=str(REPO / "signals_log.csv"))
    ap.add_argument("--verbose", action="store_true", help="print every flip")
    args = ap.parse_args()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.path.insert(0, str(REPO))
    sys.exit(run(args.ref, Path(args.csv), args.verbose))


if __name__ == "__main__":
    main()
