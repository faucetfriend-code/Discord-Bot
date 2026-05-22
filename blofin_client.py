"""
Thin wrapper around the blofin PyPI package for order placement and balance queries.
Reads credentials from environment; base_url selects demo vs live.
"""

import os
from typing import Optional

from blofin import BloFinClient as _BloFinClient
from dotenv import load_dotenv
from logger import log

load_dotenv()

_client: Optional[_BloFinClient] = None


def _get_client() -> _BloFinClient:
    global _client
    if _client is None:
        base_url = os.getenv("BLOFIN_BASE_URL", "https://demo-trading-openapi.blofin.com")
        _client = _BloFinClient(
            api_key=os.getenv("BloFinAPI"),
            api_secret=os.getenv("Blofin_secret_key"),
            passphrase=os.getenv("Passphrase"),
            base_url=base_url,
        )
    return _client


def get_balance() -> float:
    """Return available USDT balance."""
    try:
        client = _get_client()
        resp = client.account.get_balance(account_type="futures")
        # SDK returns a dict; navigate to USDT available
        details = resp.get("data", {})
        if isinstance(details, list):
            details = details[0] if details else {}
        details = details.get("details", [])
        for asset in details:
            if asset.get("currency", "").upper() == "USDT":
                return float(asset.get("available", 0))
        return 0.0
    except Exception as e:
        log.error(f"get_balance failed: {e}")
        return 0.0


def place_order(signal, size: float) -> dict:
    """
    Place a limit order with TP and SL attached.
    signal: Signal dataclass (symbol, side, entry, sl, tp)
    size:   contract quantity already rounded to lot size
    Returns the raw API response dict.
    """
    # Ensure inst_id is in BloFin format e.g. "BTC-USDT"
    inst_id = signal.symbol if "-USDT" in signal.symbol else f"{signal.symbol}-USDT"
    position_side = "long" if signal.side == "buy" else "short"

    client = _get_client()
    resp = client.trading.place_order(
        inst_id=inst_id,
        margin_mode="cross",
        position_side=position_side,
        side=signal.side,
        order_type="limit",
        price=str(signal.entry),
        size=str(size),
        tp_trigger_px=str(signal.tp),
        sl_trigger_px=str(signal.sl),
    )
    log.info(f"Order response: {resp}")
    return resp


def cancel_all_orders() -> dict:
    try:
        return _get_client().trading.cancel_all_orders()
    except Exception as e:
        log.error(f"cancel_all_orders failed: {e}")
        return {}
