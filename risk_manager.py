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

# Contract specs (contract_value, lot_size, min_size) are fetched live from
# BloFin's instruments endpoint via blofin_client.get_contract_specs() — see
# calculate_size(). No hardcoded lot table is needed; the exchange is the source
# of truth, so newly-listed symbols size correctly with no code changes.

_MAX_RISK_REWARD = 20.0  # reject signals with RR > 20x (likely bad parse)
_MIN_RISK_REWARD = 1.0   # reject signals with RR < 1x


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
    Risk-based position sizing, returned in CONTRACTS (what BloFin's order API expects).

    Steps:
      1. risk budget   = balance * RISK_PCT
      2. coins wanted  = risk budget / |entry - sl|   (base-currency amount)
      3. contracts     = coins wanted / contract_value
      4. floor to lot_size increment
      5. reject if below the exchange minimum (min_size)

    Returns None if specs are unavailable or the sized order is below min_size.
    """
    # Imported here to avoid a circular import at module load time.
    import blofin_client as bf

    risk_pct = float(os.getenv("RISK_PCT", 0.01))
    risk_per_unit = abs(signal.entry - signal.sl)
    if risk_per_unit == 0:
        return None

    coins_wanted = (balance * risk_pct) / risk_per_unit

    specs = bf.get_contract_specs(signal.symbol)
    if not specs or specs["contract_value"] <= 0:
        log.error(
            f"No contract specs for {signal.symbol} — cannot size safely, skipping. "
            f"(Instrument may be unlisted on BloFin or instruments fetch failed.)"
        )
        return None

    contract_value = specs["contract_value"]
    lot = specs["lot_size"] or contract_value
    min_size = specs["min_size"]

    contracts_wanted = coins_wanted / contract_value
    size = math.floor(contracts_wanted / lot) * lot

    if size < min_size or size <= 0:
        log.warning(
            f"{signal.symbol}: risk-based size {contracts_wanted:.4f} contracts "
            f"(<{min_size} min) too small for ${balance:.2f} balance at "
            f"{risk_pct*100:.1f}% risk — skipping. Stop is {risk_per_unit:.6f} wide; "
            f"a valid order would risk more than {risk_pct*100:.1f}% of balance."
        )
        return None

    return round(size, 8)
