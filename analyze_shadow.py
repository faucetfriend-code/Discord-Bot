"""
Shadow-log outcome analyzer — turns strategy_shadow.csv from a list of filter
opinions into measured results.

For every shadow row it fetches what price ACTUALLY did after the alert
(Gate.io public 5m candles — keyless, read-only; covers the micro-caps BloFin
demo doesn't list) and simulates the trade under the strategy's own rules:

  rsi_extreme : entry at alert/live price, SL -5%, scale-out TPs +5%/+10%,
                SL ratchets to break-even after TP1   (RSI_SL_PCT / RSI_TP_PCTS)
  oraclealgo  : SL -1.5%, TPs +2%/+4%, same ratchet   (ORACLE_SL_PCT / ORACLE_TP_PCTS)

It also simulates the proposed-but-disabled CONFIRMATION-ON-TURN entry (wait for
price to revert RSI_CONFIRM_REVERT_PCT within RSI_CONFIRM_WINDOW_MIN before
entering) on the same data, so the two entry styles can be compared per signal.

An ALTERNATE EXIT model is simulated on the same entries (report-only, live
exits unchanged until it proves out):
  rsi_extreme : TP1/BE ratchet moved to +3.75% (0.75R) - 9/41 losers in the
                Jun'26 sample ran +0.75R..1R before stopping full -1R
  oraclealgo  : SL widened to 2.0% - tests whether the 1.5% stop is noise-tight
                                              (RSI_ALT_TP_PCTS / ORACLE_ALT_SL_PCT)

Output: shadow_outcomes.csv (one row per shadow signal, original columns +
outcome/R-multiple/MFE/MAE/confirm/alt-exit columns) and a console tuning report:
would-be win rate & expectancy overall, per filter verdict, confirmation vs
immediate entry, current vs alternate exits, decision-gate vetoes, and
repeat-alert buckets.

Usage:  python analyze_shadow.py [--horizon-hours 72] [--csv strategy_shadow.csv]

Read-only: no orders, no DB writes, no BloFin auth — public market data only.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

GATE_URL = ("https://api.gateio.ws/api/v4/spot/candlesticks"
            "?currency_pair={pair}&interval=5m&from={frm}&to={to}")

_candle_cache: dict = {}


def fetch_candles(symbol: str, start_ts: int, end_ts: int):
    """5m candles [(t, open, high, low, close), ...] from Gate.io public API.
    Returns [] when the pair isn't listed there either. Tries the exact ticker,
    then with a trailing digit stripped (Discord renames like SKYAI1 → SKYAI)."""
    base = symbol.replace("-USDT", "")
    tries = [base]
    if base and base[-1].isdigit():
        tries.append(base.rstrip("0123456789"))
    for b in tries:
        key = (b, start_ts // 300, end_ts // 300)
        if key in _candle_cache:
            return _candle_cache[key]
        url = GATE_URL.format(pair=f"{b}_USDT", frm=start_ts, to=end_ts)
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.loads(r.read().decode())
            # Gate v4 row: [t, quote_vol, close, high, low, open, base_vol, closed]
            candles = [(int(c[0]), float(c[5]), float(c[3]), float(c[4]), float(c[2]))
                       for c in data]
            candles.sort(key=lambda c: c[0])
            _candle_cache[key] = candles
            if candles:
                return candles
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, KeyError):
            continue
        finally:
            time.sleep(0.15)          # stay polite on the public endpoint
    return []


def simulate(side: str, entry: float, candles: list, sl_pct: float, tp_pcts: list):
    """Walk candles, applying the bot's scale-out engine: equal slice at each TP
    rung, SL → break-even after the first. Intra-candle ambiguity (SL and a TP
    rung both inside one candle) resolves CONSERVATIVELY as the stop.

    Returns dict(outcome, ret_pct, r_mult, mfe_pct, mae_pct, hours).
    ret_pct is the blended position return; r_mult is ret/sl_pct (risk units)."""
    long = side == "buy"
    sl = entry * (1 - sl_pct) if long else entry * (1 + sl_pct)
    rungs = [entry * (1 + p) if long else entry * (1 - p) for p in tp_pcts]
    slice_ret = []                    # banked per-slice returns (fraction of entry)
    rung_i, mfe, mae = 0, 0.0, 0.0
    t0 = candles[0][0] if candles else 0

    for (t, o, h, l, c) in candles:
        fav = (h - entry) / entry if long else (entry - l) / entry
        adv = (entry - l) / entry if long else (h - entry) / entry
        mfe, mae = max(mfe, fav), max(mae, adv)
        hours = (t - t0) / 3600.0

        sl_hit = (l <= sl) if long else (h >= sl)
        tp_hit = rung_i < len(rungs) and ((h >= rungs[rung_i]) if long else (l <= rungs[rung_i]))

        if sl_hit:                    # conservative: stop fires before a same-candle TP
            stop_ret = (sl - entry) / entry if long else (entry - sl) / entry
            n_left = len(rungs) - rung_i
            slice_ret.extend([stop_ret] * n_left)
            ret = sum(slice_ret) / len(rungs)
            label = "loss_sl" if rung_i == 0 else "tp_then_be"
            return dict(outcome=label, ret_pct=ret * 100, r_mult=ret / sl_pct,
                        mfe_pct=mfe * 100, mae_pct=mae * 100, hours=round(hours, 1))
        if tp_hit:
            slice_ret.append(tp_pcts[rung_i])
            rung_i += 1
            if rung_i == len(rungs):
                ret = sum(slice_ret) / len(rungs)
                return dict(outcome="win_full", ret_pct=ret * 100, r_mult=ret / sl_pct,
                            mfe_pct=mfe * 100, mae_pct=mae * 100, hours=round(hours, 1))
            sl = entry                # ratchet to break-even after the first rung

    # horizon exhausted — mark-to-market the remainder at the last close
    if not candles:
        return dict(outcome="no_data", ret_pct=0, r_mult=0, mfe_pct=0, mae_pct=0, hours=0)
    last = candles[-1][4]
    open_ret = (last - entry) / entry if long else (entry - last) / entry
    n_left = len(rungs) - rung_i
    slice_ret.extend([open_ret] * n_left)
    ret = sum(slice_ret) / len(rungs)
    return dict(outcome="open", ret_pct=ret * 100, r_mult=ret / sl_pct,
                mfe_pct=mfe * 100, mae_pct=mae * 100,
                hours=round((candles[-1][0] - t0) / 3600.0, 1))


def simulate_confirm(side: str, alert_price: float, candles: list, revert_pct: float,
                     window_min: float, sl_pct: float, tp_pcts: list):
    """Confirmation-on-turn: enter only once price reverts revert_pct in the
    trade's favour from the alert price within the window. Returns
    (fired, result_dict_or_None)."""
    long = side == "buy"
    trigger = alert_price * (1 + revert_pct) if long else alert_price * (1 - revert_pct)
    window_end = candles[0][0] + window_min * 60 if candles else 0
    for i, (t, o, h, l, c) in enumerate(candles):
        if t > window_end:
            return False, None
        fired = (h >= trigger) if long else (l <= trigger)
        if fired:
            return True, simulate(side, trigger, candles[i:], sl_pct, tp_pcts)
    return False, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="strategy_shadow.csv")
    ap.add_argument("--out", default="shadow_outcomes.csv")
    ap.add_argument("--horizon-hours", type=float, default=72.0)
    args = ap.parse_args()

    rsi_sl = float(os.getenv("RSI_SL_PCT", "0.05"))
    rsi_tps = [float(p) for p in os.getenv("RSI_TP_PCTS", "0.05,0.10").split(",") if p.strip()]
    ora_sl = float(os.getenv("ORACLE_SL_PCT", "0.015"))
    ora_tps = [float(p) for p in os.getenv("ORACLE_TP_PCTS", "0.02,0.04").split(",") if p.strip()]
    c_revert = float(os.getenv("RSI_CONFIRM_REVERT_PCT", "0.005"))
    c_window = float(os.getenv("RSI_CONFIRM_WINDOW_MIN", "30"))
    # Alternate exit models, simulated on the SAME entries as the main outcome
    # so the comparison isolates the exit change.
    rsi_alt_tps = [float(p) for p in
                   os.getenv("RSI_ALT_TP_PCTS", "0.0375,0.10").split(",") if p.strip()]
    ora_alt_sl = float(os.getenv("ORACLE_ALT_SL_PCT", "0.02"))

    rows = list(csv.DictReader(open(args.csv, encoding="utf-8")))
    if not rows:
        print("shadow log is empty"); return

    # Backfill blank entry_price on HISTORIC rows from the alert text in
    # signals_log.csv (live capture of the quoted price only ships going forward).
    try:
        from signal_parser import _RSI_PRICE
        siglog = [r for r in csv.DictReader(open("signals_log.csv", encoding="utf-8"))
                  if r.get("analyst") == "RSI Extreme"]
        for r in rows:
            if r.get("entry_price") or r["source"] != "rsi_extreme":
                continue
            rt = datetime.fromisoformat(r["ts"])
            base = r["symbol"].split("-")[0]      # raw text says "ALLO/USDT", not "ALLO-USDT"
            for s in siglog:
                if base not in s.get("raw_text", ""):
                    continue
                if abs((datetime.fromisoformat(s["ts"]) - rt).total_seconds()) > 120:
                    continue
                m = _RSI_PRICE.search(s["raw_text"])
                if m:
                    r["entry_price"] = m.group("px").replace(",", "")
                    break
    except (OSError, ImportError):
        pass

    # repeat-alert count: Nth alert for the same symbol within the prior 24h.
    # Newer rows carry repeat_n live (logged by the bot, which sees the full alert
    # stream); for historic blanks fall back to counting within this CSV.
    times = {}
    for r in rows:
        ts = datetime.fromisoformat(r["ts"])
        sym = r["symbol"]
        times.setdefault(sym, []).append(ts)
        logged = str(r.get("repeat_n") or "").strip()
        if logged:
            r["repeat_n"] = int(float(logged))
        else:
            r["repeat_n"] = sum(1 for t in times[sym] if 0 <= (ts - t).total_seconds() <= 86400)

    out_rows = []
    for r in rows:
        ts = datetime.fromisoformat(r["ts"])
        start = int(ts.timestamp())
        end = int(min(datetime.now(ts.tzinfo), ts + timedelta(hours=args.horizon_hours)).timestamp())
        entry = float(r["entry_price"]) if r.get("entry_price") else None
        res = {"outcome": "no_entry_price", "ret_pct": "", "r_mult": "", "mfe_pct": "",
               "mae_pct": "", "hours": ""}
        cf_fired, cf, alt = "", {}, {}
        candles = []
        if entry:
            candles = fetch_candles(r["symbol"], start, end)
            if candles:
                sl_pct, tps = (rsi_sl, rsi_tps) if r["source"] == "rsi_extreme" else (ora_sl, ora_tps)
                res = simulate(r["side"], entry, candles, sl_pct, tps)
                if r["source"] == "rsi_extreme":
                    fired, cres = simulate_confirm(r["side"], entry, candles,
                                                   c_revert, c_window, sl_pct, tps)
                    cf_fired = "yes" if fired else "no"
                    cf = cres or {}
                    alt = simulate(r["side"], entry, candles, rsi_sl, rsi_alt_tps)
                else:
                    alt = simulate(r["side"], entry, candles, ora_alt_sl, ora_tps)
            else:
                res = {"outcome": "no_data", "ret_pct": "", "r_mult": "", "mfe_pct": "",
                       "mae_pct": "", "hours": ""}
        out = dict(r)
        out.update({k: (round(v, 3) if isinstance(v, float) else v) for k, v in res.items()})
        out["confirm_fired"] = cf_fired
        out["confirm_outcome"] = cf.get("outcome", "")
        out["confirm_r"] = round(cf["r_mult"], 3) if cf.get("r_mult") is not None and cf else ""
        out["alt_outcome"] = alt.get("outcome", "")
        out["alt_r"] = round(alt["r_mult"], 3) if alt and alt.get("r_mult") is not None else ""
        out_rows.append(out)
        print(f"  {r['ts'][:16]} {r['source'][:6]:6} {r['symbol']:13} {r['side']:4} "
              f"-> {out['outcome']:12} R={out['r_mult'] if out['r_mult']!='' else '—'}")

    fields = list(out_rows[0].keys())
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(out_rows)
    print(f"\nwrote {args.out} ({len(out_rows)} rows)\n")

    # ---- tuning report -------------------------------------------------
    def stats(rs):
        done = [x for x in rs if isinstance(x.get("r_mult"), (int, float))]
        if not done:
            return "n=0"
        wins = sum(1 for x in done if x["r_mult"] > 0)
        avg = sum(x["r_mult"] for x in done) / len(done)
        return f"n={len(done)} WR={wins/len(done)*100:.0f}% avgR={avg:+.2f} totR={sum(x['r_mult'] for x in done):+.1f}"

    rsi = [x for x in out_rows if x["source"] == "rsi_extreme"]
    ora = [x for x in out_rows if x["source"] == "oraclealgo"]
    print("=== RSI Extreme (immediate entry, as the bot trades today) ===")
    print("  ALL signals:        ", stats(rsi))
    print("  chase-filter PASS:  ", stats([x for x in rsi if x.get("f_chase") == "True"]))
    print("  chase-filter BLOCK: ", stats([x for x in rsi if x.get("f_chase") == "False"]))
    print("  regime PASS:        ", stats([x for x in rsi if x.get("f_regime") == "True"]))
    print("  regime BLOCK:       ", stats([x for x in rsi if x.get("f_regime") == "False"]))
    print("  1st alert (24h):    ", stats([x for x in rsi if x.get("repeat_n") == 1]))
    print("  repeat alert (2+):  ", stats([x for x in rsi if isinstance(x.get("repeat_n"), int) and x["repeat_n"] > 1]))
    conf = [dict(r_mult=x["confirm_r"]) for x in rsi
            if x.get("confirm_fired") == "yes" and isinstance(x.get("confirm_r"), (int, float))]
    nofire = sum(1 for x in rsi if x.get("confirm_fired") == "no")
    print("\n=== Confirmation-on-turn (simulated, RSI only) ===")
    print(f"  fired {len(conf)} / skipped {nofire} of {len(conf)+nofire} with data")
    print("  confirmed entries:  ", stats(conf))

    print("\n=== RSI Extreme by symbol 1H ADX regime at alert time ===")
    for lbl in ("ranging_calm", "indecisive", "trending_moderate", "trending_strong"):
        print(f"  {lbl:26s}", stats([x for x in rsi if x.get("adx_regime") == lbl]))
    print("  (no ADX data):            ", stats([x for x in rsi if not x.get("adx_regime")]))
    print("  BTC 4H trending (macro):", stats([x for x in rsi
                                               if str(x.get("btc_adx_regime","")).endswith(
                                                   ("trending_moderate","trending_strong"))]))
    print("  BTC 4H ranging (macro): ", stats([x for x in rsi
                                               if str(x.get("btc_adx_regime","")).endswith(
                                                   ("ranging_calm","indecisive"))]))

    def alt_stats(rs):
        return stats([dict(r_mult=x["alt_r"]) for x in rs
                      if isinstance(x.get("alt_r"), (int, float))])

    print("\n=== Exit-model comparison (same entries, alternate exits; report-only) ===")
    print(f"  RSI current  (TP1 @ +{rsi_tps[0]*100:g}% = {rsi_tps[0]/rsi_sl:.2f}R):",
          stats(rsi))
    print(f"  RSI alt      (TP1 @ +{rsi_alt_tps[0]*100:g}% = {rsi_alt_tps[0]/rsi_sl:.2f}R):",
          alt_stats(rsi))
    print(f"  Oracle current (SL {ora_sl*100:g}%):", stats(ora))
    print(f"  Oracle alt     (SL {ora_alt_sl*100:g}%):", alt_stats(ora))

    print("\n=== Gate vetoes (what each decision reason would have done) ===")
    for src, subset in (("rsi", rsi), ("oracle", ora)):
        reasons = sorted({str(x.get("decision") or "?") for x in subset})
        for d in reasons:
            print(f"  {src:6s} {d:16s}", stats([x for x in subset
                                                if str(x.get("decision") or "?") == d]))

    print("\n=== OracleAlgo (simulated as if every 1H signal entered) ===")
    print("  ALL 1H signals:     ", stats(ora))
    print("  had fresh bias:     ", stats([x for x in ora if x.get("btc_bias") in ("bull", "bear")]))
    print("  htf-fallback bias:  ", stats([x for x in ora if str(x.get("btc_bias", "")).startswith("htf_")]))
    print("  stale-bias only:    ", stats([x for x in ora if str(x.get("btc_bias", "")).startswith("stale")]))
    print("  no bias at all:     ", stats([x for x in ora if not x.get("btc_bias")]))
    print("\n  BTC 4H ADX regime at signal time:")
    for lbl in ("ranging_calm", "indecisive", "trending_moderate", "trending_strong"):
        subset = [x for x in ora if f"_{lbl}" in str(x.get("btc_adx_regime", ""))]
        print(f"    {lbl:26s}", stats(subset))


if __name__ == "__main__":
    main()
