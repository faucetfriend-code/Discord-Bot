"""
Main bot loop.
Polls Discord notification inbox every POLL_INTERVAL seconds.
Parses signals, validates risk, executes on BloFin demo (or live).
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
import risk_manager as rm
import blofin_client as bf
from logger import log

_logger_mod.init_db()
pt.init_db()


def _get_whitelist() -> list[str]:
    raw = os.getenv("ANALYST_WHITELIST", "")
    return [name.strip() for name in raw.split(",") if name.strip()]


def _process_message(msg: dict, dry_run: bool):
    signal = sp.parse(msg)
    analyst = msg.get("author", "unknown")

    if signal is None:
        log.info(f"[{analyst}] No trade signal detected")
        _logger_mod.log_signal(analyst, msg.get("content", ""), outcome="no_signal")
        return

    log.info(f"[{analyst}] Signal: {signal.side.upper()} {signal.symbol} "
             f"entry={signal.entry} sl={signal.sl} tp={signal.tp}")

    open_positions = pt.get_open_positions()
    ok, reason = rm.validate(signal, open_positions)
    if not ok:
        log.warning(f"Signal rejected: {reason}")
        _logger_mod.log_signal(analyst, msg["content"], signal=signal, outcome=f"rejected:{reason}")
        return

    balance = bf.get_balance()
    size = rm.calculate_size(balance, signal)
    if size is None:
        log.warning("Position size too small — skipping")
        _logger_mod.log_signal(analyst, msg["content"], signal=signal, outcome="size_too_small")
        return

    log.info(f"Balance ${balance:.2f} | Size {size} {signal.symbol} | DRY_RUN={dry_run}")

    if dry_run:
        _logger_mod.log_signal(analyst, msg["content"], signal=signal, outcome="dry_run")
        return

    try:
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
        _logger_mod.log_signal(analyst, msg["content"], signal=signal,
                               outcome="executed", order_id=order_id)
    except Exception as e:
        log.error(f"Order failed: {e}")
        _logger_mod.log_signal(analyst, msg["content"], signal=signal, outcome=f"error:{e}")


def main():
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    poll_interval = int(os.getenv("POLL_INTERVAL", 60))
    whitelist = _get_whitelist()

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

    seen_ids = pt.get_seen_ids()
    log.info(f"Loaded {len(seen_ids)} previously-seen message IDs")

    while True:
        try:
            new_msgs = dr.poll_inbox(seen_ids, whitelist)
            for msg in new_msgs:
                pt.mark_seen(msg["id"])
                seen_ids.add(msg["id"])
                _process_message(msg, dry_run)
        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.error(f"Poll loop error: {e}")

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
