"""
Main bot loop — real-time event-driven via CDP MutationObserver.

A persistent Chrome DevTools Protocol WebSocket injects a MutationObserver
into Discord's notification inbox.  New notifications are delivered to a
queue instantly.  A periodic sweep (SWEEP_INTERVAL seconds) re-injects the
observer and does a full inbox scan as a safety net.

Signal routing:
  NEW    → validate risk, size, place order on BloFin
  UPDATE → amend SL/TP on existing BloFin order
  CLOSE  → market-close existing BloFin position
"""

import math
import os
import queue as _queue
import re
import time
from dotenv import load_dotenv

load_dotenv()

import logger as _logger_mod
import position_tracker as pt
import discord_reader as dr
import signal_parser as sp
from signal_parser import MessageType
import risk_manager as rm
import blofin_client as bf
import dashboard
from price_stream import PriceStream
from logger import log, now_local

_logger_mod.init_db()
pt.init_db()


def _get_whitelist() -> list[str]:
    """Parse ANALYST_WHITELIST into a list of analyst display names."""
    raw = os.getenv("ANALYST_WHITELIST", "")
    return [name.strip() for name in raw.split(",") if name.strip()]


def _hedge_mode() -> bool:
    """HEDGE_MODE allows each source to hold its own position per symbol (and
    opposing long/short positions to coexist). When off, it's one position per
    symbol globally — a second source's signal on an open symbol is skipped."""
    return os.getenv("HEDGE_MODE", "false").lower() == "true"


def _find_existing(symbol: str, source_key: str):
    """The open position that would block/own a new signal: in hedge mode only the
    SAME source counts; otherwise any open position on the symbol does."""
    return pt.find_open_by_symbol(symbol, source_key if _hedge_mode() else None)


def _leverage_params() -> tuple[int, int, int, int]:
    """Return (start, lo, hi, step) for the adaptive per-analyst leverage system."""
    start = int(os.getenv("LEVERAGE_START", "75"))
    lo = int(os.getenv("LEVERAGE_MIN", "50"))
    hi = int(os.getenv("LEVERAGE_MAX", "125"))
    step = int(os.getenv("LEVERAGE_STEP", "10"))
    return start, lo, hi, step


def _canonical_analyst(msg: dict, whitelist: list[str]) -> str:
    """
    Resolve the canonical analyst name for a message (e.g. "Soul Alerts"),
    using the same matching tiers as the whitelist gate. This is the key used
    for performance attribution, so it must be stable across the entry signal
    and the later outcome resolution. Falls back to the raw author.
    """
    author = msg.get("author", "").lower()
    content = msg.get("content", "").lower()
    haystack = author + " " + content
    for name in whitelist:
        nl = name.lower()
        if nl in haystack:
            return name
        base = re.sub(r'\s*alerts\s*$', '', nl).strip()
        if len(base) >= 4 and re.search(r'(?<!\w)' + re.escape(base), haystack):
            return name
    return msg.get("author", "") or "unknown"


# ---------------------------------------------------------------------------
# Per-type handlers
# ---------------------------------------------------------------------------

def _calc_auto_tp(signal: sp.Signal) -> float | None:
    """
    Auto-calculate TP using DEFAULT_RR risk:reward ratio when analyst omits it.
    Long:  TP = entry + RR * (entry - SL)
    Short: TP = entry - RR * (SL - entry)
    Returns None if entry/SL are missing or the geometry is wrong.
    """
    try:
        rr = float(os.getenv("DEFAULT_RR", "2.0"))
        entry, sl = signal.entry, signal.sl
        if entry is None or sl is None:
            return None
        if signal.side == "buy":
            risk = entry - sl
            if risk <= 0:
                return None
            return round(entry + rr * risk, 8)
        else:  # sell / short
            risk = sl - entry
            if risk <= 0:
                return None
            return round(entry - rr * risk, 8)
    except Exception:
        return None


def _process_new(signal: sp.Signal, open_positions: list, dry_run: bool,
                 leverage: int = 50, analyst_key: str = ""):
    """Validate and place a new order (limit or market)."""
    analyst = signal.analyst
    analyst_key = analyst_key or analyst or "unknown"

    # For market/CMP orders: fetch live price so we can size and auto-TP correctly.
    if signal.is_market_order and signal.entry is None:
        live_price = bf.get_market_price(signal.symbol)
        if live_price:
            signal.entry = live_price
            log.info(f"[{analyst}] CMP signal — fetched live price {live_price} for {signal.symbol}")
        elif bf.get_contract_specs(signal.symbol) is None:
            # Symbol isn't listed on this BloFin endpoint (demo has a limited set;
            # live has many more). Nothing wrong — we just can't trade it here.
            log.info(f"[{analyst}] {signal.symbol} not listed on BloFin — skipping")
            _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                                   outcome="symbol_not_listed")
            return
        else:
            log.warning(f"[{analyst}] {signal.symbol} is listed but live price fetch failed "
                        f"(transient API error) — skipping")
            _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                                   outcome="cmp_price_fetch_failed")
            return

    # tp_ladder = ordered TP targets (nearest first) for scale-out management.
    tp_ladder: list[float] = []

    # RSI Extreme strategy: derive SL/TP as fixed percentages around the live
    # entry (mean-reversion). Long: TP above / SL below; short: the reverse.
    if signal.source == "rsi_extreme" and signal.entry is not None:
        tp_pct = float(os.getenv("RSI_TP_PCT", "0.10"))
        sl_pct = float(os.getenv("RSI_SL_PCT", "0.05"))
        if signal.side == "buy":
            signal.sl = round(signal.entry * (1 - sl_pct), 8)
            signal.tp = round(signal.entry * (1 + tp_pct), 8)
        else:
            signal.sl = round(signal.entry * (1 + sl_pct), 8)
            signal.tp = round(signal.entry * (1 - tp_pct), 8)
        tp_ladder = [signal.tp]
        log.info(f"[RSI] {signal.symbol} {signal.side.upper()} reversion — entry {signal.entry} "
                 f"SL {signal.sl} ({sl_pct*100:.0f}%) TP {signal.tp} (+{tp_pct*100:.0f}%)")

    # OracleAlgo: fixed % stop + a scale-out TP ladder around the live entry.
    if signal.source == "oraclealgo" and signal.entry is not None:
        sl_pct = float(os.getenv("ORACLE_SL_PCT", "0.015"))
        tp_pcts = [float(p) for p in os.getenv("ORACLE_TP_PCTS", "0.02,0.04").split(",") if p.strip()]
        if signal.side == "buy":
            signal.sl = round(signal.entry * (1 - sl_pct), 8)
            tp_ladder = [round(signal.entry * (1 + p), 8) for p in tp_pcts]
        else:
            signal.sl = round(signal.entry * (1 + sl_pct), 8)
            tp_ladder = [round(signal.entry * (1 - p), 8) for p in tp_pcts]
        signal.tp = tp_ladder[-1]
        log.info(f"[OracleAlgo] {signal.symbol} {signal.side.upper()} — entry {signal.entry} "
                 f"SL {signal.sl} ({sl_pct*100:.1f}%) TP ladder {tp_ladder} "
                 f"({'/'.join(f'{p*100:.0f}%' for p in tp_pcts)})")

    # Assign SL + the full TP ladder from chart levels by geometry now that the
    # entry (live price for market orders) is known. The vision model reads the
    # numbers reliably but mislabels roles, so derive them from the side:
    # short → SL above / TPs below entry; long → the reverse.
    if getattr(signal, "vision_levels", None) and signal.entry is not None and not tp_ladder:
        chart_sl, chart_tps = rm.chart_sl_tp(signal.entry, signal.side, signal.vision_levels)
        if signal.sl is None and chart_sl is not None:
            signal.sl = chart_sl
            log.info(f"[{analyst}] {signal.symbol} SL from chart (geometry) → {signal.sl}")
        if chart_tps:
            tp_ladder = chart_tps                 # full ladder (TP1, TP2, TP3 …)
            signal.tp = chart_tps[-1]             # furthest target = final/attached TP
            log.info(f"[{analyst}] {signal.symbol} TP ladder from chart → {tp_ladder}")

    # Auto-calculate TP at DEFAULT_RR if still none (analyst gave no TP, no chart).
    if signal.tp is None and signal.entry is not None and signal.sl is not None:
        signal.tp = _calc_auto_tp(signal)
        if signal.tp is not None:
            tp_ladder = [signal.tp]
            log.info(f"[{analyst}] No TP in signal — auto-calculated at "
                     f"{os.getenv('DEFAULT_RR', '2.0')}:1 R:R → TP={signal.tp}")

    # Single TP supplied directly (analyst text / regex) — make it the ladder.
    if not tp_ladder and signal.tp is not None:
        tp_ladder = [signal.tp]

    if signal.entry is None or signal.sl is None or signal.tp is None:
        log.warning(f"[{analyst}] NEW signal missing entry/sl/tp — skipping")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="incomplete_signal")
        return

    # Sanity guard for vision/market levels: SL or TP implausibly far from the
    # live entry usually means the vision model misread the chart axis (e.g. read
    # $16 for a coin trading at $44). Reject rather than trade on a bad level.
    # RSI levels (fixed 5/10%) and normal analyst stops sit well inside this band.
    if signal.is_market_order and signal.source != "rsi_extreme":
        band = float(os.getenv("VISION_MAX_DEVIATION_PCT", "0.5"))
        sl_dev = abs(signal.sl / signal.entry - 1)
        tp_dev = abs(signal.tp / signal.entry - 1)
        if sl_dev > band or tp_dev > band:
            log.warning(f"[{analyst}] {signal.symbol} levels implausible vs live entry "
                        f"{signal.entry} (SL {sl_dev*100:.0f}%, TP {tp_dev*100:.0f}% away; "
                        f"max {band*100:.0f}%) — likely vision misread, skipping")
            _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                                   outcome="levels_implausible")
            return

    order_label = "MARKET" if signal.is_market_order else "LIMIT"
    log.info(f"[{analyst}] NEW {order_label} {signal.side.upper()} {signal.symbol} "
             f"entry={signal.entry} sl={signal.sl} tp={signal.tp}")

    ok, reason = rm.validate(signal, open_positions, hedge_mode=_hedge_mode())
    if not ok:
        log.warning(f"Signal rejected: {reason}")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome=f"rejected:{reason}")
        return

    balance = bf.get_balance()
    if balance is None:
        log.error(f"[{analyst}] Balance unavailable (API/auth error) — cannot size {signal.symbol}, skipping")
        _alert("BloFin balance unavailable (auth/API error) — trades are being skipped",
               "error", key="balance_unavailable")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="balance_unavailable")
        return
    if balance <= 0:
        log.warning(f"[{analyst}] Account balance is ${balance:.2f} — nothing to trade with, skipping")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="zero_balance")
        return

    size = rm.calculate_size(balance, signal)
    if size is None:
        log.warning("Position size too small — skipping")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="size_too_small")
        return

    log.info(f"Balance ${balance:.2f} | Size {size} {signal.symbol} | "
             f"[{analyst_key}] Leverage {leverage}x cross | DRY_RUN={dry_run}")

    if len(tp_ladder) > 1:
        log.info(f"[{analyst}] {signal.symbol} scale-out ladder: {tp_ladder} "
                 f"(partial close + SL ratchet at each target)")

    if dry_run:
        # Record a virtual position so the outcome resolver can track it and
        # adapt the analyst's leverage exactly as it would in live mode.
        order_id = f"DRYRUN-{signal.symbol}-{int(time.time())}"
        pt.open_position(pt.Position(
            symbol=signal.symbol, side=signal.side, entry=signal.entry,
            sl=signal.sl, tp=signal.tp, size=size, order_id=order_id,
            opened_at=now_local().isoformat(), analyst=analyst_key,
            tps=tp_ladder, orig_size=size,
        ))
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome=f"dry_run (lev {leverage}x)", order_id=order_id)
        return

    try:
        # Set leverage on this instrument before the order (cross margin).
        # Returns the leverage actually applied after clamping to the exchange max.
        applied_lev = bf.set_leverage(signal.symbol, leverage, margin_mode="cross")
        if signal.is_market_order:
            resp = bf.place_market_order(signal, size)
        else:
            resp = bf.place_order(signal, size)
        order_id = resp.get("data", {}).get("ordId", "") or str(resp)
        log.info(f"Order placed: {order_id} @ {applied_lev}x")

        pos = pt.Position(
            symbol=signal.symbol,
            side=signal.side,
            entry=signal.entry,
            sl=signal.sl,
            tp=signal.tp,
            size=size,
            order_id=order_id,
            opened_at=now_local().isoformat(),
            analyst=analyst_key,
            tps=tp_ladder,
            orig_size=size,
        )
        pt.open_position(pos)
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="executed", order_id=order_id)
    except Exception as e:
        log.error(f"Order failed: {e}")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome=f"error:{e}")


def _process_update(signal: sp.Signal, position: pt.Position, dry_run: bool):
    """
    Apply an update to an open position: a new SL/TP (amended on the exchange when
    live), and/or a new average entry from DCA/averaging (a local basis shift —
    future PnL and win/loss are measured from the new entry). Messages with no
    numeric changes (e.g. "Limit Entry Reached") are acknowledged as a clean no-op.
    """
    analyst = signal.analyst
    changes = []
    if signal.new_sl is not None:
        changes.append(f"SL→{signal.new_sl}")
    if signal.new_tp is not None:
        changes.append(f"TP→{signal.new_tp}")
    if signal.new_avg_entry is not None:
        changes.append(f"avg entry→{signal.new_avg_entry}")
    desc = ", ".join(changes) if changes else "info / acknowledgement (no numeric changes)"
    log.info(f"[{analyst}] UPDATE {signal.symbol} | {desc} | order={position.order_id}")

    # Average-entry shift is a LOCAL state change (no exchange order), applied in
    # both dry-run and live so PnL/attribution use the right basis.
    if signal.new_avg_entry is not None:
        pt.update_position_entry(position.order_id, signal.new_avg_entry)
        log.info(f"[{analyst}] {position.symbol} entry basis updated "
                 f"{position.entry} → {signal.new_avg_entry} (DCA/averaging)")

    # Nothing to send to the exchange (info no-op, or avg-entry only)?
    if signal.new_sl is None and signal.new_tp is None:
        outcome = "avg_entry_updated" if signal.new_avg_entry is not None else "acknowledged"
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome=outcome, order_id=position.order_id)
        return

    if dry_run:
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="dry_run_update", order_id=position.order_id)
        return

    inst_id = signal.symbol if "-USDT" in signal.symbol else f"{signal.symbol}-USDT"
    try:
        bf.amend_order(inst_id, position.order_id,
                       new_sl=signal.new_sl, new_tp=signal.new_tp)
        pt.update_position_sl_tp(position.order_id, signal.new_sl, signal.new_tp)
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="amended", order_id=position.order_id)
    except Exception as e:
        log.error(f"Amend order failed: {e}")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome=f"amend_error:{e}", order_id=position.order_id)


def _pnl_usdt(entry: float, exit_price: float, contracts: float,
             contract_value: float, side: str) -> float:
    """Realized/unrealized PnL in USDT for `contracts` of a position."""
    coins = contracts * contract_value
    return (exit_price - entry) * coins if side == "buy" else (entry - exit_price) * coins


def _settle_position(position: pt.Position, exit_price: float, reason: str,
                     won: bool = None):
    """
    Close a position locally, decide win/loss, record realized PnL, and adapt
    the analyst's leverage. If `won` is not given it's inferred from exit vs entry
    (scale-outs pass won explicitly — any banked TP = net win).
    """
    start, lo, hi, step = _leverage_params()
    if won is None:
        won = exit_price >= position.entry if position.side == "buy" \
            else exit_price <= position.entry

    # Realized PnL on the remaining size being closed here.
    specs = bf.get_contract_specs(position.symbol)
    cv = specs["contract_value"] if specs else 0.0
    pnl = _pnl_usdt(position.entry, exit_price, position.size, cv, position.side)
    pt.add_realized_pnl(position.analyst, pnl)

    new_lev = pt.record_outcome(position.analyst, won, step, lo, hi)
    pt.close_position(position.order_id)

    verdict = "WIN" if won else "LOSS"
    arrow = f"+{step}" if won else f"-{step}"
    log.info(f"[{position.analyst}] {position.symbol} {verdict} ({reason}) "
             f"exit={exit_price} entry={position.entry} tps_hit={position.tps_hit} "
             f"PnL ${pnl:+.2f} → leverage {arrow} = {new_lev}x")
    _logger_mod.log_signal(position.analyst, f"{position.symbol} {reason}",
                           outcome=f"{'win' if won else 'loss'}:{reason} pnl=${pnl:+.2f} lev={new_lev}x",
                           order_id=position.order_id)
    return won, new_lev


def _process_close(signal: sp.Signal, position: pt.Position, dry_run: bool):
    """Market-close an existing open position and attribute the outcome."""
    analyst = signal.analyst
    log.info(f"[{analyst}] CLOSE {position.symbol} | order={position.order_id}")

    price = bf.get_market_price(position.symbol)

    if not dry_run:
        inst_id = position.symbol if "-USDT" in position.symbol else f"{position.symbol}-USDT"
        position_side = "long" if position.side == "buy" else "short"
        try:
            bf.close_position_api(inst_id, position_side)
        except Exception as e:
            log.error(f"Close position failed: {e}")
            _logger_mod.log_signal(analyst, signal.raw_text,
                                   outcome=f"close_error:{e}", order_id=position.order_id)
            return

    if price is not None:
        _settle_position(position, price, "manual_close")
    else:
        # Couldn't price the exit — close without adjusting leverage (rare).
        pt.close_position(position.order_id)
        log.warning(f"[{analyst}] Closed {position.symbol} but no price for win/loss attribution")
        _logger_mod.log_signal(analyst, signal.raw_text,
                               outcome="closed_no_price", order_id=position.order_id)


def _resolve_open_positions(dry_run: bool = True):
    """
    Poll market price for every open position and manage it as a scale-out ladder:

      • At each TP target (nearest first): close an equal slice of the original
        size and ratchet the stop up — to break-even after TP1, then to the
        previous TP after each subsequent target.
      • At the FINAL target: close the remainder (win).
      • If the (ratcheted) stop is hit: close the remainder. It's a win if any TP
        was already banked (stop sits at BE or a prior TP), otherwise a loss.

    Single-TP positions (RSI, plain analyst calls) are just full-close-at-TP.
    Works in dry-run (virtual sizes) and live (reduce-only orders + SL amend).
    """
    for pos in pt.get_open_positions():
        price = bf.get_market_price(pos.symbol)
        if price is None:
            continue

        specs = bf.get_contract_specs(pos.symbol)
        cv = specs["contract_value"] if specs else 0.0

        # Mark-to-market: update unrealized PnL on the remaining size each sweep.
        unreal = _pnl_usdt(pos.entry, price, pos.size, cv, pos.side)
        pt.update_unrealized(pos.order_id, price, unreal)

        ladder = pos.tps or ([pos.tp] if pos.tp else [])
        n = len(ladder)
        if n == 0:
            continue
        is_long = pos.side == "buy"

        # 1) Stop-loss (possibly ratcheted) hit?
        sl_hit = (is_long and price <= pos.sl) or (not is_long and price >= pos.sl)
        if sl_hit:
            won = pos.tps_hit >= 1  # banked >=TP1 → net win; raw stop with none → loss
            reason = "trail_stop" if pos.tps_hit >= 1 else "sl"
            if not dry_run:
                _live_close_remaining(pos)
            _settle_position(pos, price, reason, won=won)
            continue

        # 2) Next take-profit target hit?
        if pos.tps_hit >= n:
            continue
        next_tp = ladder[pos.tps_hit]
        tp_hit = (is_long and price >= next_tp) or (not is_long and price <= next_tp)
        if not tp_hit:
            continue

        new_hit = pos.tps_hit + 1

        # Final target → close the remainder.
        if new_hit >= n:
            if not dry_run:
                _live_close_remaining(pos)
            _settle_position(pos, price, f"tp{new_hit}_final", won=True)
            continue

        # Intermediate target → partial close + ratchet SL.
        lot = specs["lot_size"] if specs and specs.get("lot_size") else 0
        chunk = pos.orig_size / n
        if lot:
            chunk = math.floor(chunk / lot) * lot
        remaining = round(pos.size - chunk, 8)

        # Too small to split (or rounding wiped the chunk) → take it all here as a win.
        if chunk <= 0 or remaining <= 0:
            if not dry_run:
                _live_close_remaining(pos)
            _settle_position(pos, price, f"tp{new_hit}_nosplit", won=True)
            continue

        new_sl = pos.entry if new_hit == 1 else ladder[new_hit - 2]
        sl_label = "break-even" if new_hit == 1 else f"TP{new_hit - 1}"

        if not dry_run:
            try:
                bf.reduce_position(pos.symbol, pos.side, chunk)
                inst = pos.symbol if "-USDT" in pos.symbol else f"{pos.symbol}-USDT"
                bf.amend_order(inst, pos.order_id, new_sl=new_sl)
            except Exception as e:
                log.error(f"Scale-out order/amend failed for {pos.symbol}: {e}")

        # Realized PnL on the slice we just took off.
        slice_pnl = _pnl_usdt(pos.entry, price, chunk, cv, pos.side)
        pt.add_realized_pnl(pos.analyst, slice_pnl)
        pt.apply_partial(pos.order_id, remaining, new_sl, new_hit)
        log.info(f"[{pos.analyst}] {pos.symbol} TP{new_hit} hit @ {price} — closed {chunk} "
                 f"(${slice_pnl:+.2f}), {remaining} left, SL → {new_sl} ({sl_label})")
        _logger_mod.log_signal(pos.analyst, f"{pos.symbol} TP{new_hit} scale-out",
                               outcome=f"scaled_tp{new_hit} pnl=${slice_pnl:+.2f} sl={new_sl}",
                               order_id=pos.order_id)


def _reconcile_on_startup(dry_run: bool):
    """
    On boot, immediately settle/scale any open position that moved while the bot
    was offline. Price may have crossed SEVERAL ladder levels during downtime, so
    resolve repeatedly until the open-position state stops changing (each pass
    advances a position by at most one level).
    """
    open_now = pt.get_open_positions()
    if not open_now:
        return
    log.info(f"Reconciling {len(open_now)} open position(s) against current price "
             f"(catching up on moves during downtime)…")
    for _ in range(12):  # safety cap
        snapshot = {(p.order_id, p.tps_hit) for p in pt.get_open_positions()}
        if not snapshot:
            break
        _resolve_open_positions(dry_run)
        if {(p.order_id, p.tps_hit) for p in pt.get_open_positions()} == snapshot:
            break
    still_open = len(pt.get_open_positions())
    log.info(f"Startup reconciliation complete — {still_open} position(s) still open")


def _live_close_remaining(pos: pt.Position):
    """Market-close whatever remains of a live position (reduce-only)."""
    inst = pos.symbol if "-USDT" in pos.symbol else f"{pos.symbol}-USDT"
    side = "long" if pos.side == "buy" else "short"
    try:
        bf.close_position_api(inst, side)
    except Exception as e:
        log.error(f"Live close failed for {pos.symbol}: {e}")


# ---------------------------------------------------------------------------
# Message router
# ---------------------------------------------------------------------------

def _is_whitelisted(msg: dict, whitelist_lower: set[str]) -> bool:
    """
    Return True if the message is from a whitelisted analyst.

    Three match tiers (any one is sufficient):
      1. Full name   — "sveezy alerts" anywhere in author+content  (real-time path
                       where @Sveezy Alerts role mention is in the content text)
      2. Base name   — the part before " alerts" as a whole word, e.g. "sveezy"
                       (poll path where embed says "Signal by Sveezy")
      3. Author word — any 4+ char word from the analyst name found in the author
                       field alone (handles "Sveezy ✅ | Unity" style usernames)
    """
    author = msg.get("author", "").lower()
    content = msg.get("content", "").lower()
    haystack = author + " " + content

    for name in whitelist_lower:
        # Tier 1: exact full name
        if name in haystack:
            return True
        # Tier 2: base name (everything before " alerts").
        # Use a start-of-word anchor only (not end) because Discord's embed
        # textContent sometimes concatenates fields: "Signal by SveezyEntry..."
        base = re.sub(r'\s*alerts\s*$', '', name).strip()
        if len(base) >= 4:
            if re.search(r'(?<!\w)' + re.escape(base), haystack):
                return True
        # Tier 3: any significant word from the name found in author field
        for word in name.split():
            if len(word) >= 4 and word != 'alerts' and word in author:
                return True
    return False


_ORACLE_BIAS_KEY = "btc_oracle_bias"


def _process_oraclealgo(signal: sp.Signal, msg: dict, dry_run: bool):
    """
    OracleAlgo BTC state machine:
      • 4H signal → set/refresh the BTC bias (bull/bear). On a bias FLIP, close
        any open OracleAlgo position that now runs counter to it. No entry.
      • 1H signal → enter ONLY if it agrees with the current 4H bias and there's
        no BTC position open. SL/TP (1.5% stop + 2%/4% scale-out) set in _process_new.
    """
    content = msg.get("content", "")
    if os.getenv("ORACLEALGO_ENABLED", "true").lower() != "true":
        log.info(f"[OracleAlgo] {signal.side.upper()} {signal.symbol} {signal.signal_tf} — disabled, skipping")
        _logger_mod.log_signal("OracleAlgo", content, signal=signal, outcome="oracle_disabled")
        return

    want_bias = "bull" if signal.side == "buy" else "bear"

    # --- 4H signal: set bias (and flatten an opposing position) ---
    if signal.signal_tf == "4h":
        prev = pt.get_state(_ORACLE_BIAS_KEY)
        pt.set_state(_ORACLE_BIAS_KEY, want_bias)
        if prev != want_bias:
            log.info(f"[OracleAlgo] 4H bias flip {prev or 'none'} → {want_bias.upper()}")
            existing = pt.find_open_by_symbol(signal.symbol, "OracleAlgo")
            if existing:
                counter = (want_bias == "bull" and existing.side == "sell") or \
                          (want_bias == "bear" and existing.side == "buy")
                if counter:
                    log.info(f"[OracleAlgo] bias flipped against open {existing.side} — closing")
                    _process_close(signal, existing, dry_run)
        else:
            log.info(f"[OracleAlgo] 4H bias reaffirmed {want_bias.upper()}")
        _logger_mod.log_signal("OracleAlgo", content, signal=signal,
                               outcome=f"bias_{want_bias}")
        return

    # --- 1H signal: entry if aligned with bias ---
    bias = pt.get_state(_ORACLE_BIAS_KEY)
    if not bias:
        log.info(f"[OracleAlgo] 1H {signal.side} but no 4H bias yet — waiting for a 4H signal")
        _logger_mod.log_signal("OracleAlgo", content, signal=signal, outcome="oracle_no_bias")
        return
    if bias != want_bias:
        log.info(f"[OracleAlgo] 1H {signal.side} counter to {bias.upper()} bias — skipping")
        _logger_mod.log_signal("OracleAlgo", content, signal=signal, outcome="oracle_counter_bias")
        return
    if _find_existing(signal.symbol, "OracleAlgo"):
        log.info(f"[OracleAlgo] {signal.symbol} already open — skipping")
        _logger_mod.log_signal("OracleAlgo", content, signal=signal, outcome="oracle_already_open")
        return

    start, lo, hi, _step = _leverage_params()
    leverage = pt.get_analyst_leverage("OracleAlgo", start, lo, hi)
    log.info(f"[OracleAlgo] 1H {signal.side.upper()} aligned with {bias.upper()} bias — entering")
    _process_new(signal, pt.get_open_positions(), dry_run, leverage, "OracleAlgo")


def _process_message(msg: dict, dry_run: bool, whitelist: list[str]):
    """
    Parse one notification and route it. Order of handling:
    RSI Extreme → OracleAlgo → (analyst whitelist gate) → NEW/UPDATE/CLOSE.
    Strategy signals (RSI/OracleAlgo) have their own gates and bypass the whitelist.
    """
    whitelist_lower = {name.lower() for name in whitelist}
    signal = sp.parse(msg)
    analyst = msg.get("author", "unknown")

    if signal is None:
        log.debug(f"[{analyst}] No trade signal detected")
        _logger_mod.log_signal(analyst, msg.get("content", ""), outcome="no_signal")
        return

    log.info(f"[{analyst}] Classified as {signal.message_type.value.upper()} | {signal.symbol}")

    # RSI Extreme strategy — its own gate (RSI_EXTREME_ENABLED) and its own
    # adaptive-leverage track record under the "RSI Extreme" key. Handled here,
    # before the analyst whitelist (the RSI bot is not a whitelisted analyst).
    if signal.source == "rsi_extreme":
        if os.getenv("RSI_EXTREME_ENABLED", "true").lower() != "true":
            log.info(f"[RSI] {signal.side.upper()} {signal.symbol} — RSI strategy disabled, skipping")
            _logger_mod.log_signal("RSI Extreme", msg.get("content", ""), signal=signal,
                                   outcome="rsi_disabled")
            return
        existing = _find_existing(signal.symbol, "RSI Extreme")
        if existing:
            log.info(f"[RSI] {signal.symbol} already open — skipping duplicate RSI entry")
            _logger_mod.log_signal("RSI Extreme", msg.get("content", ""), signal=signal,
                                   outcome="rsi_already_open")
            return
        start, lo, hi, _step = _leverage_params()
        leverage = pt.get_analyst_leverage("RSI Extreme", start, lo, hi)
        _process_new(signal, pt.get_open_positions(), dry_run, leverage, "RSI Extreme")
        return

    # OracleAlgo BTC strategy — 4H signals set the bias; 1H signals enter only
    # when they agree with the current bias. Own gate + adaptive-leverage key.
    if signal.source == "oraclealgo":
        _process_oraclealgo(signal, msg, dry_run)
        return

    # Whitelist gate — log the signal but don't execute if analyst isn't whitelisted.
    if not _is_whitelisted(msg, whitelist_lower):
        log.info(f"[{analyst}] Signal parsed but analyst not whitelisted — skipping execution")
        _logger_mod.log_signal(analyst, msg.get("content", ""), signal=signal,
                               outcome="not_whitelisted")
        return

    # Per-source position: in hedge mode this analyst's own position; otherwise any.
    analyst_key = _canonical_analyst(msg, whitelist)
    existing = _find_existing(signal.symbol, analyst_key)

    if signal.message_type == MessageType.NEW:
        open_positions = pt.get_open_positions()
        if existing:
            log.info(f"{signal.symbol} already open for {analyst_key} — routing NEW as UPDATE (safety net)")
            _process_update(signal, existing, dry_run)
        else:
            start, lo, hi, _step = _leverage_params()
            leverage = pt.get_analyst_leverage(analyst_key, start, lo, hi)
            _process_new(signal, open_positions, dry_run, leverage, analyst_key)

    elif signal.message_type == MessageType.UPDATE:
        if existing:
            _process_update(signal, existing, dry_run)
        else:
            log.info(f"UPDATE for {signal.symbol} but no open position — ignoring")
            _logger_mod.log_signal(analyst, msg.get("content", ""), signal=signal,
                                   outcome="update_no_position")

    elif signal.message_type == MessageType.CLOSE:
        if existing:
            _process_close(signal, existing, dry_run)
        else:
            log.info(f"CLOSE for {signal.symbol} but no open position — ignoring")
            _logger_mod.log_signal(analyst, msg.get("content", ""), signal=signal,
                                   outcome="close_no_position")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _handle_msg(msg: dict, dry_run: bool, whitelist: list[str], bot_state: dict, seen_ids: set):
    """Mark a message seen and run the full parse + route pipeline."""
    msg_id = msg.get("id", "")
    if not msg_id or msg_id in seen_ids:
        return
    pt.mark_seen(msg_id)
    seen_ids.add(msg_id)
    bot_state["poll_count"] += 1
    bot_state["last_poll_at"] = now_local().isoformat()
    _process_message(msg, dry_run, whitelist)
    bot_state["last_signal_at"] = now_local().isoformat()


_alert_times: dict = {}


def _alert(message: str, level: str = "warning", key: str = None, cooldown: int = 1800):
    """
    Log an alert and, if ALERT_WEBHOOK_URL is set, post it to a Discord webhook.
    De-duplicated per `key` within `cooldown` seconds so failures don't spam.
    """
    key = key or message
    now = time.time()
    if now - _alert_times.get(key, 0) < cooldown:
        return
    _alert_times[key] = now
    getattr(log, level if level in ("info", "warning", "error") else "warning")(f"ALERT: {message}")
    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        import requests
        requests.post(url, json={"content": f"🚨 Discord Signal Bot: {message}"}, timeout=8)
    except Exception as e:
        log.debug(f"Alert webhook post failed: {e}")


def _log_strategy_config():
    """Print the active strategy configuration so settings are confirmed each run."""
    def on(v):
        return "ON " if os.getenv(v, "true").lower() == "true" else "off"
    log.info("Strategy config:")
    log.info(f"  Risk: {float(os.getenv('RISK_PCT','0.01'))*100:.1f}%/trade | "
             f"max {os.getenv('MAX_OPEN_POSITIONS','3')} positions | "
             f"leverage start {os.getenv('LEVERAGE_START','75')}x "
             f"({os.getenv('LEVERAGE_MIN','50')}-{os.getenv('LEVERAGE_MAX','125')}x, "
             f"±{os.getenv('LEVERAGE_STEP','10')} by performance)")
    log.info(f"  RSI Extreme [{on('RSI_EXTREME_ENABLED')}]: "
             f"SL {float(os.getenv('RSI_SL_PCT','0.05'))*100:.0f}% / "
             f"TP {float(os.getenv('RSI_TP_PCT','0.10'))*100:.0f}%")
    log.info(f"  OracleAlgo  [{on('ORACLEALGO_ENABLED')}]: 4H bias + 1H entry | "
             f"SL {float(os.getenv('ORACLE_SL_PCT','0.015'))*100:.1f}% / "
             f"TP ladder {os.getenv('ORACLE_TP_PCTS','0.02,0.04')}")
    log.info(f"  Vision: model '{os.getenv('LOCAL_VISION_MODEL','(none)')}' | "
             f"max level deviation {float(os.getenv('VISION_MAX_DEVIATION_PCT','0.5'))*100:.0f}%")


def _startup_health_check(dry_run: bool) -> bool:
    """Verify each subsystem is reachable; print a green/red checklist. Returns all-ok."""
    log.info("-" * 60)
    log.info("Startup health check:")
    ok = True

    # BloFin auth + instrument coverage
    bal = bf.get_balance()
    if bal is None:
        log.error("  [FAIL] BloFin: auth/API error — check keys + BLOFIN_BASE_URL")
        _alert("BloFin auth failed at startup", "error", key="health_blofin")
        ok = False
    else:
        n_inst = len(bf._load_instruments())
        log.info(f"  [ OK ] BloFin: balance ${bal:.2f}, {n_inst} instruments listed")
        if bal <= 0 and not dry_run:
            log.warning("  [WARN] Live balance is $0 — fund the account before trading")

    # LM Studio + models
    base = os.getenv("LOCAL_LLM_BASE_URL", "").strip()
    if base:
        try:
            import requests
            data = requests.get(base + "/models", timeout=6).json()
            loaded = [d.get("id", "") for d in data.get("data", [])]
            log.info(f"  [ OK ] LM Studio reachable — loaded: {loaded}")
            for var, label in (("LOCAL_LLM_MODEL", "text"), ("LOCAL_VISION_MODEL", "vision")):
                m = os.getenv(var, "").strip()
                if m and not any(m in x for x in loaded):
                    log.warning(f"  [WARN] {label} model '{m}' not loaded — that path is disabled")
        except Exception as e:
            log.warning(f"  [WARN] LM Studio not reachable at {base} ({e}) — LLM/vision fallback off")

    # Chrome CDP + Discord tab
    if dr.verify_connected():
        tab = bool(dr._get_discord_ws_url())
        if tab:
            log.info("  [ OK ] Chrome CDP reachable, Discord tab present")
        else:
            log.error("  [FAIL] Chrome CDP up but no Discord tab — open Discord in the tab")
            ok = False
    else:
        log.error("  [FAIL] Chrome CDP not reachable on port 9222")
        ok = False

    # Live + open dashboard = real balance/positions viewable by anyone with the URL.
    if not dry_run and not os.getenv("DASHBOARD_PASSWORD", "").strip():
        log.warning("  [WARN] LIVE trading with no DASHBOARD_PASSWORD set — if the dashboard "
                    "is exposed (e.g. a Cloudflare tunnel) your real balance/positions are public")

    log.info(f"Health check: {'ALL GOOD' if ok else 'FAILURES above — fix before relying on the bot'}")
    log.info("-" * 60)
    return ok


def main():
    """
    Entry point. Starts the dashboard + price stream, runs the health check,
    reconciles positions that moved while offline, then enters the event loop:
    real-time notifications drive trades, a fast timer manages open positions
    (PnL + TP/SL/scale-out), and a slow timer re-scans the inbox as a safety net.
    """
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    # SWEEP_INTERVAL: how often to re-inject the observer and do a full inbox scan.
    # This is the only remaining periodic operation; real-time delivery is via the listener.
    sweep_interval = int(os.getenv("POLL_INTERVAL", 300))
    # RESOLVE_INTERVAL: how often to manage open positions (mark-to-market PnL +
    # TP/SL/scale-out). Runs off cached stream prices so it's cheap to run often.
    resolve_interval = int(os.getenv("RESOLVE_INTERVAL", 10))
    whitelist = _get_whitelist()
    server_filter = os.getenv("DISCORD_SERVER_FILTER", "")

    bot_state = {
        "started_at": now_local().isoformat(),
        "dry_run": dry_run,
        "poll_count": 0,
        "last_poll_at": None,
        "last_poll_found": 0,
        "chrome_connected": False,
        "discord_tab": False,
        "last_signal_at": None,
    }
    dashboard.start(bot_state, port=5050)
    log.info("Dashboard running at http://localhost:5050")

    log.info("=" * 60)
    log.info(f"Discord Signal Bot starting — DRY_RUN={dry_run}")
    log.info(f"BloFin base: {os.getenv('BLOFIN_BASE_URL')}")
    log.info(f"Analysts ({len(whitelist)}): {', '.join(whitelist)}")
    log.info(f"Server filter: '{server_filter or 'none'}'")
    log.info(f"Sweep interval: {sweep_interval}s")
    log.info("=" * 60)
    _log_strategy_config()

    # Wait for Chrome CDP to be ready
    connected = False
    for attempt in range(1, 7):
        if dr.verify_connected():
            connected = True
            break
        log.warning(f"CDP not ready (attempt {attempt}/6) — retrying in 5s...")
        time.sleep(5)

    if not connected:
        log.error(
            "Cannot reach Chrome on port 9222 after 30s.\n"
            "Make sure Chrome launched via run_bot.bat."
        )
        _alert("Cannot reach Chrome on port 9222 — bot not started", "error", key="startup_cdp")
        return

    bot_state["chrome_connected"] = True

    # Subsystem health check (BloFin, LM Studio, Chrome/Discord).
    _startup_health_check(dry_run)
    seen_ids = pt.get_seen_ids()
    log.info(f"Loaded {len(seen_ids)} previously-seen message IDs")

    # Live price stream (BloFin public WS). The resolver/dashboard peek at its
    # cache; REST is the fallback when a tick is stale (e.g. quiet demo market).
    price_stream = None
    try:
        price_stream = PriceStream(os.getenv("BLOFIN_BASE_URL", ""))
        price_stream.start()
        bf.set_price_stream(price_stream)
        price_stream.ensure([p.symbol for p in pt.get_open_positions()])
    except Exception as e:
        log.warning(f"Price stream unavailable ({e}) — using REST prices only")
        price_stream = None

    # Reconcile open positions FIRST — settle/scale anything that moved while
    # the bot was offline, before processing any new inbox signals.
    _reconcile_on_startup(dry_run)

    # Startup sweep — catch any messages that arrived while the bot was offline
    log.info("Running startup inbox sweep…")
    for msg in dr.poll_inbox(seen_ids, server_filter):
        _handle_msg(msg, dry_run, whitelist, bot_state, seen_ids)

    # Start real-time listener
    listener = dr.NotificationListener(seen_ids, server_filter)
    try:
        listener.start()
    except Exception as e:
        log.error(f"Failed to start real-time listener: {e} — falling back to sweep-only mode")
        listener = None

    last_sweep = time.time()
    last_resolve = time.time()

    while True:
        try:
            discord_tab = bool(dr._get_discord_ws_url())
            bot_state["discord_tab"] = discord_tab
            if not discord_tab:
                _alert("Lost the Discord tab (Chrome/CDP issue) — signals not being received",
                       "error", key="cdp_no_tab")

            if listener and listener.is_alive:
                # Wake at least every resolve_interval to manage positions.
                try:
                    msg = listener.queue.get(timeout=resolve_interval)
                    bot_state["last_poll_found"] = 1
                    log.info("--- Real-time notification ---")
                    _handle_msg(msg, dry_run, whitelist, bot_state, seen_ids)
                except _queue.Empty:
                    pass  # no new message — proceed to maintenance checks
            else:
                # Listener dead or unavailable — fall back to periodic sweep
                _alert("Real-time listener is down — running in sweep-only fallback mode",
                       "warning", key="listener_down")
                time.sleep(resolve_interval)

            # Fast cadence: mark-to-market PnL + TP/SL/scale-out off cached prices.
            if time.time() - last_resolve >= resolve_interval:
                if price_stream:
                    price_stream.ensure([p.symbol for p in pt.get_open_positions()])
                _resolve_open_positions(dry_run)
                last_resolve = time.time()

            # Periodic sweep: re-inject observer + full inbox scan.
            if time.time() - last_sweep >= sweep_interval:
                log.info("Periodic sweep: re-injecting observer and scanning inbox")
                if listener:
                    listener.reinject()
                sweep_msgs = dr.poll_inbox(seen_ids, server_filter)
                bot_state["last_poll_found"] = len(sweep_msgs)
                for msg in sweep_msgs:
                    _handle_msg(msg, dry_run, whitelist, bot_state, seen_ids)
                last_sweep = time.time()

        except KeyboardInterrupt:
            log.info("Shutting down.")
            if listener:
                listener.stop()
            if price_stream:
                price_stream.stop()
            break
        except Exception as e:
            log.error(f"Main loop error: {e}")


if __name__ == "__main__":
    main()
