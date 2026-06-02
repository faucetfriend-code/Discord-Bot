"""
Go-live preflight for the Discord Signal Bot.

Read-only checks against the LIVE BloFin endpoint, to run BEFORE you flip
DRY_RUN=false. It verifies:
  • your LIVE API keys authenticate, and shows the live balance
  • how many instruments live lists (vs demo's limited set)
  • which of your recent signal symbols are actually tradeable on live
  • config safety (DRY_RUN, risk %, leverage band, live keys present)

It places NO orders and changes NO settings. Run it manually when ready:

    python preflight.py

Going live afterwards is three manual steps:
    1. BLOFIN_BASE_URL=https://openapi.blofin.com   (in .env)
    2. DRY_RUN=false                                 (in .env)
    3. restart the bot
The bot auto-uses your LIVE keys on the live endpoint (Demo-* keys are ignored).
"""

import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LIVE_URL = "https://openapi.blofin.com"
DEMO_URL = "https://demo-trading-openapi.blofin.com"
DB_PATH = Path(__file__).parent / "bot.db"

PASS, WARN, FAIL = "[PASS]", "[WARN]", "[FAIL]"
_PLACEHOLDERS = {"", "your_blofin_api_key", "your_live_blofin_api_key",
                 "your_blofin_secret_key", "your_blofin_passphrase"}


def _recent_symbols(limit_signals: int = 600) -> list[str]:
    """
    Distinct ticker symbols the bot actually TRIED to trade recently — i.e. signals
    whose outcome was a real entry attempt (dry-run, executed, sized, rejected,
    not-listed, or a resolved win/loss). Excludes parser noise (blocklist words,
    analyst names from old mis-parses, empty bases).
    """
    try:
        from signal_parser import _SYMBOL_BLOCKLIST
    except Exception:
        _SYMBOL_BLOCKLIST = set()
    trade_outcomes = (
        "dry_run", "executed", "size_too_small", "symbol_not_listed",
        "rejected", "balance_unavailable", "win:", "loss:", "scaled_tp",
        "incomplete_signal", "levels_implausible",
    )
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT symbol, outcome FROM signals ORDER BY id DESC LIMIT ?",
            (limit_signals,),
        ).fetchall()
        con.close()
    except Exception as e:
        print(f"  (could not read recent symbols: {e})")
        return []

    out = set()
    for sym, outcome in rows:
        if not sym or sym == "UNKNOWN-USDT":
            continue
        outcome = outcome or ""
        if not any(outcome.startswith(t) or t in outcome for t in trade_outcomes):
            continue
        base = sym.replace("-USDT", "").strip()
        if not base or base.upper() in _SYMBOL_BLOCKLIST:
            continue
        out.add(sym)
    return sorted(out)


def _switch_to_live():
    """Point blofin_client at the LIVE endpoint with the live (non-Demo) keys."""
    import blofin_client as bf
    os.environ["BLOFIN_BASE_URL"] = LIVE_URL
    bf._client = None              # force re-init against live
    bf._instruments_cache = None   # force re-fetch live instruments
    return bf


def main():
    print("=" * 64)
    print("  GO-LIVE PREFLIGHT  —  read-only checks against LIVE BloFin")
    print("=" * 64)

    go = True

    # ---- Config safety -------------------------------------------------
    print("\n[Config]")
    dry = os.getenv("DRY_RUN", "true").lower() == "true"
    print(f"  {WARN if dry else PASS} DRY_RUN = {dry}"
          + ("   → still simulation; set false (after this passes) for live"
             if dry else "   → LIVE execution armed"))

    api = os.getenv("BloFinAPI", "")
    sec = os.getenv("Blofin_secret_key", "")
    pas = os.getenv("Passphrase", "")
    keys_ok = all(v and v not in _PLACEHOLDERS for v in (api, sec, pas))
    print(f"  {PASS if keys_ok else FAIL} Live API credentials "
          + ("present" if keys_ok else "MISSING/placeholder — fill BloFinAPI/secret/Passphrase"))
    go = go and keys_ok

    print(f"  {PASS} Risk per trade .... {float(os.getenv('RISK_PCT','0.01'))*100:.1f}%")
    print(f"  {PASS} Leverage band ..... {os.getenv('LEVERAGE_MIN','50')}-{os.getenv('LEVERAGE_MAX','125')}x "
          f"(start {os.getenv('LEVERAGE_START','75')}x)")
    print(f"  {PASS} Max positions ..... {os.getenv('MAX_OPEN_POSITIONS','3')}")
    print(f"  {PASS} Strategies ........ RSI={os.getenv('RSI_EXTREME_ENABLED','true')}, "
          f"OracleAlgo={os.getenv('ORACLEALGO_ENABLED','true')}")

    if not keys_ok:
        print("\nCannot test live auth without credentials. Fix .env and re-run.")
        _verdict(False)
        return

    # ---- Live auth + balance ------------------------------------------
    print(f"\n[Live BloFin]  {LIVE_URL}")
    bf = _switch_to_live()
    balance = bf.get_balance()
    if balance is None:
        print(f"  {FAIL} Auth — live keys rejected (check key/secret/passphrase on blofin.com)")
        go = False
        _verdict(go)
        return
    print(f"  {PASS} Auth — live keys accepted")
    if balance > 0:
        print(f"  {PASS} Balance ........... ${balance:,.2f}")
    else:
        print(f"  {WARN} Balance ........... $0.00 — fund the live futures account before trading")
        go = False

    try:
        n_live = len(bf._load_instruments())
        print(f"  {PASS} Instruments listed  {n_live} on live")
    except Exception as e:
        print(f"  {WARN} Could not load live instruments: {e}")
        n_live = 0

    # ---- Recent signal symbols tradeable on live? ----------------------
    print("\n[Recent signal symbols — tradeable on live?]")
    syms = _recent_symbols()
    if not syms:
        print("  (no recent symbols in bot.db yet — run the bot in dry-run first)")
    else:
        tradeable = 0
        for s in syms:
            listed = bf.get_contract_specs(s) is not None
            tradeable += listed
            mark = "yes" if listed else "NO "
            print(f"    {mark}  {s}")
        print(f"\n  Tradeable on live: {tradeable}/{len(syms)} recent symbols"
              + ("  — the rest have no BloFin market (they'll keep skipping, which is fine)"
                 if tradeable < len(syms) else ""))

    _verdict(go)


def _verdict(go: bool):
    print("\n" + "=" * 64)
    if go:
        print("  VERDICT: GO  ✅   Live auth + funded. To trade live:")
        print("    1) set BLOFIN_BASE_URL=https://openapi.blofin.com")
        print("    2) set DRY_RUN=false")
        print("    3) restart the bot")
    else:
        print("  VERDICT: NO-GO  ❌   Resolve the [FAIL]/[WARN] items above, then re-run.")
    print("=" * 64)


if __name__ == "__main__":
    main()
