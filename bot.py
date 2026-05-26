"""
Main bot loop.
Polls Discord notification inbox every POLL_INTERVAL seconds.
Parses and classifies signals, then routes:
  NEW    → validate risk, size, place order on BloFin
  UPDATE → amend SL/TP on existing BloFin order
  CLOSE  → market-close existing BloFin position
"""

import os
import time
from datetime import datetime, timezone
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
from logger import log

_logger_mod.init_db()
pt.init_db()


def _get_whitelist() -> list[str]:
    raw = os.getenv("ANALYST_WHITELIST", "")
    return [name.strip() for name in raw.split(",") if name.strip()]


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


def _process_new(signal: sp.Signal, open_positions: list, dry_run: bool):
    """Validate and place a new order (limit or market)."""
    analyst = signal.analyst

    # For market/CMP orders: fetch live price so we can size and auto-TP correctly.
    if signal.is_market_order and signal.entry is None:
        live_price = bf.get_market_price(signal.symbol)
        if live_price:
            signal.entry = live_price
            log.info(f"[{analyst}] CMP signal — fetched live price {live_price} for {signal.symbol}")
        else:
            log.warning(f"[{analyst}] CMP signal but could not fetch live price — skipping")
            _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                                   outcome="cmp_price_fetch_failed")
            return

    # Auto-calculate TP at DEFAULT_RR if analyst didn't provide one
    if signal.tp is None and signal.entry is not None and signal.sl is not None:
        signal.tp = _calc_auto_tp(signal)
        if signal.tp is not None:
            log.info(f"[{analyst}] No TP in signal — auto-calculated at "
                     f"{os.getenv('DEFAULT_RR', '2.0')}:1 R:R → TP={signal.tp}")

    if signal.entry is None or signal.sl is None or signal.tp is None:
        log.warning(f"[{analyst}] NEW signal missing entry/sl/tp — skipping")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="incomplete_signal")
        return

    order_label = "MARKET" if signal.is_market_order else "LIMIT"
    log.info(f"[{analyst}] NEW {order_label} {signal.side.upper()} {signal.symbol} "
             f"entry={signal.entry} sl={signal.sl} tp={signal.tp}")

    ok, reason = rm.validate(signal, open_positions)
    if not ok:
        log.warning(f"Signal rejected: {reason}")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome=f"rejected:{reason}")
        return

    balance = bf.get_balance()
    size = rm.calculate_size(balance, signal)
    if size is None:
        log.warning("Position size too small — skipping")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="size_too_small")
        return

    log.info(f"Balance ${balance:.2f} | Size {size} {signal.symbol} | DRY_RUN={dry_run}")

    if dry_run:
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="dry_run")
        return

    try:
        if signal.is_market_order:
            resp = bf.place_market_order(signal, size)
        else:
            resp = bf.place_order(signal, size)
        order_id = resp.get("data", {}).get("ordId", "") or str(resp)
        log.info(f"Order placed: {order_id}")

        pos = pt.Position(
            symbol=signal.symbol,
            side=signal.side,
            entry=signal.entry,
            sl=signal.sl,
            tp=signal.tp,
            size=size,
            order_id=order_id,
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
        pt.open_position(pos)
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="executed", order_id=order_id)
    except Exception as e:
        log.error(f"Order failed: {e}")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome=f"error:{e}")


def _process_update(signal: sp.Signal, position: pt.Position, dry_run: bool):
    """Amend SL/TP on an existing open order."""
    analyst = signal.analyst
    changes = []
    if signal.new_sl is not None:
        changes.append(f"SL→{signal.new_sl}")
    if signal.new_tp is not None:
        changes.append(f"TP→{signal.new_tp}")
    desc = ", ".join(changes) if changes else "no numeric changes extracted"

    log.info(f"[{analyst}] UPDATE {signal.symbol} | {desc} | order={position.order_id}")

    if dry_run:
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="dry_run_update", order_id=position.order_id)
        return

    if signal.new_sl is None and signal.new_tp is None:
        log.info("Update had no numeric SL/TP values — nothing to amend on BloFin")
        _logger_mod.log_signal(analyst, signal.raw_text, signal=signal,
                               outcome="update_no_values", order_id=position.order_id)
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


def _process_close(signal: sp.Signal, position: pt.Position, dry_run: bool):
    """Market-close an existing open position."""
    analyst = signal.analyst
    log.info(f"[{analyst}] CLOSE {position.symbol} | order={position.order_id}")

    if dry_run:
        _logger_mod.log_signal(analyst, signal.raw_text,
                               outcome="dry_run_close", order_id=position.order_id)
        return

    inst_id = position.symbol if "-USDT" in position.symbol else f"{position.symbol}-USDT"
    position_side = "long" if position.side == "buy" else "short"
    try:
        bf.close_position_api(inst_id, position_side)
        pt.close_position(position.order_id)
        _logger_mod.log_signal(analyst, signal.raw_text,
                               outcome="closed", order_id=position.order_id)
    except Exception as e:
        log.error(f"Close position failed: {e}")
        _logger_mod.log_signal(analyst, signal.raw_text,
                               outcome=f"close_error:{e}", order_id=position.order_id)


# ---------------------------------------------------------------------------
# Message router
# ---------------------------------------------------------------------------

def _is_whitelisted(msg: dict, whitelist_lower: set[str]) -> bool:
    """Return True if the message author or content contains a whitelisted analyst name."""
    haystack = (msg.get("author", "") + " " + msg.get("content", "")).lower()
    return any(w in haystack for w in whitelist_lower)


def _process_message(msg: dict, dry_run: bool, whitelist_lower: set[str]):
    signal = sp.parse(msg)
    analyst = msg.get("author", "unknown")

    if signal is None:
        log.debug(f"[{analyst}] No trade signal detected")
        _logger_mod.log_signal(analyst, msg.get("content", ""), outcome="no_signal")
        return

    log.info(f"[{analyst}] Classified as {signal.message_type.value.upper()} | {signal.symbol}")

    # Whitelist gate — log the signal but don't execute if analyst isn't whitelisted.
    if not _is_whitelisted(msg, whitelist_lower):
        log.info(f"[{analyst}] Signal parsed but analyst not whitelisted — skipping execution")
        _logger_mod.log_signal(analyst, msg.get("content", ""), signal=signal,
                               outcome="not_whitelisted")
        return

    if signal.message_type == MessageType.NEW:
        open_positions = pt.get_open_positions()
        existing = pt.find_open_by_symbol(signal.symbol)
        if existing:
            log.info(f"{signal.symbol} already open — routing NEW as UPDATE (safety net)")
            _process_update(signal, existing, dry_run)
        else:
            _process_new(signal, open_positions, dry_run)

    elif signal.message_type == MessageType.UPDATE:
        existing = pt.find_open_by_symbol(signal.symbol)
        if existing:
            _process_update(signal, existing, dry_run)
        else:
            log.info(f"UPDATE for {signal.symbol} but no open position — ignoring")
            _logger_mod.log_signal(analyst, msg.get("content", ""), signal=signal,
                                   outcome="update_no_position")

    elif signal.message_type == MessageType.CLOSE:
        existing = pt.find_open_by_symbol(signal.symbol)
        if existing:
            _process_close(signal, existing, dry_run)
        else:
            log.info(f"CLOSE for {signal.symbol} but no open position — ignoring")
            _logger_mod.log_signal(analyst, msg.get("content", ""), signal=signal,
                                   outcome="close_no_position")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    poll_interval = int(os.getenv("POLL_INTERVAL", 60))
    whitelist = _get_whitelist()

    # Shared state for the dashboard
    bot_state = {
        "started_at": datetime.now(timezone.utc).isoformat(),
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
    log.info(f"Poll interval: {poll_interval}s")
    log.info("=" * 60)

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
            "Make sure Chrome launched via run_bot.bat (it must start fresh with --remote-debugging-port=9222)."
        )
        return

    bot_state["chrome_connected"] = True
    seen_ids = pt.get_seen_ids()
    log.info(f"Loaded {len(seen_ids)} previously-seen message IDs")

    while True:
        try:
            bot_state["poll_count"] += 1
            bot_state["last_poll_at"] = datetime.now(timezone.utc).isoformat()
            bot_state["discord_tab"] = bool(dr._get_discord_ws_url())
            log.info(f"--- Poll #{bot_state['poll_count']} ---")

            new_msgs = dr.poll_inbox(seen_ids)
            bot_state["last_poll_found"] = len(new_msgs)

            whitelist_lower = {name.lower() for name in whitelist}
            for msg in new_msgs:
                pt.mark_seen(msg["id"])
                seen_ids.add(msg["id"])
                _process_message(msg, dry_run, whitelist_lower)
                bot_state["last_signal_at"] = datetime.now(timezone.utc).isoformat()

        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.error(f"Poll loop error: {e}")

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
