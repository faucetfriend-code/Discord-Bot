"""
Validates signals and sizes positions.
All risk decisions flow through here — nothing else calculates size or exposure.
"""

import math
import os
from typing import Optional

from logger import log
from signal_parser import Signal
from position_tracker import Position

# BloFin lot sizes per instrument (contracts).
# These are minimums; update as new symbols are traded.
# Default fallback is 1 contract if symbol not listed.
LOT_SIZES: dict[str, float] = {
    "BTC-USDT": 0.001,
    "ETH-USDT": 0.01,
    "SOL-USDT": 0.1,
    "XRP-USDT": 1.0,
    "DOGE-USDT": 10.0,
    "BNB-USDT": 0.01,
    "ADA-USDT": 1.0,
    "AVAX-USDT": 0.1,
    "LTC-USDT": 0.1,
    "DOT-USDT": 0.1,
    "TRUMP-USDT": 0.1,
    "SUI-USDT": 1.0,
}

_MAX_RISK_REWARD = 20.0  # reject signals with RR > 20x (likely bad parse)
_MIN_RISK_REWARD = 1.0   # reject signals with RR < 1x


def _lot_size(symbol: str) -> float:
    return LOT_SIZES.get(symbol.upper(), 1.0)


def validate(signal: Signal, open_positions: list[Position]) -> tuple[bool, str]:
    """Returns (ok, reason). reason is empty string when ok=True."""
    side = signal.side

    # Price level sanity
    if side == "buy":
        if not (signal.sl < signal.entry):
            return False, f"BUY signal SL {signal.sl} not below entry {signal.entry}"
        if not (signal.entry < signal.tp):
            return False, f"BUY signal TP {signal.tp} not above entry {signal.entry}"
    else:
        if not (signal.sl > signal.entry):
            return False, f"SELL signal SL {signal.sl} not above entry {signal.entry}"
        if not (signal.entry > signal.tp):
            return False, f"SELL signal TP {signal.tp} not below entry {signal.entry}"

    # Risk/reward ratio sanity
    risk = abs(signal.entry - signal.sl)
    reward = abs(signal.tp - signal.entry)
    if risk == 0:
        return False, "Zero risk (entry == sl)"
    rr = reward / risk
    if rr < _MIN_RISK_REWARD:
        return False, f"RR {rr:.2f} below minimum {_MIN_RISK_REWARD}"
    if rr > _MAX_RISK_REWARD:
        return False, f"RR {rr:.2f} implausibly high — likely parse error"

    # Exposure limits
    max_pos = int(os.getenv("MAX_OPEN_POSITIONS", 3))
    if len(open_positions) >= max_pos:
        return False, f"Max open positions ({max_pos}) reached"

    open_symbols = {p.symbol.upper() for p in open_positions}
    if signal.symbol.upper() in open_symbols:
        return False, f"Already have an open position in {signal.symbol}"

    return True, ""


def calculate_size(balance: float, signal: Signal) -> Optional[float]:
    """
    Size = (balance * RISK_PCT) / |entry - sl|, floored to lot size.
    Returns None if the computed size is below minimum lot size.
    """
    risk_pct = float(os.getenv("RISK_PCT", 0.01))
    lot = _lot_size(signal.symbol)
    risk_per_unit = abs(signal.entry - signal.sl)
    if risk_per_unit == 0:
        return None
    raw = (balance * risk_pct) / risk_per_unit
    size = math.floor(raw / lot) * lot
    if size < lot:
        log.warning(f"Computed size {raw:.6f} is below minimum lot {lot} for {signal.symbol}")
        return None
    return round(size, 8)
