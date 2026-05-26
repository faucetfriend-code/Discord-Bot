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


def get_market_price(symbol: str) -> Optional[float]:
    """Return the last traded price for a symbol (used for CMP / market-order sizing)."""
    try:
        inst_id = symbol if "-USDT" in symbol else f"{symbol}-USDT"
        client = _get_client()
        resp = client.market.get_tickers(inst_id=inst_id)
        data = resp.get("data", [])
        if isinstance(data, list) and data:
            return float(data[0].get("last", 0) or 0) or None
        return None
    except Exception as e:
        log.warning(f"get_market_price({symbol}) failed: {e}")
        return None


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


def place_market_order(signal, size: float) -> dict:
    """
    Place a market order (no limit price) with optional TP and SL.
    Used for CMP / 'at market' signals where no entry price is specified.
    """
    inst_id = signal.symbol if "-USDT" in signal.symbol else f"{signal.symbol}-USDT"
    position_side = "long" if signal.side == "buy" else "short"

    params: dict = dict(
        inst_id=inst_id,
        margin_mode="cross",
        position_side=position_side,
        side=signal.side,
        order_type="market",
        size=str(size),
    )
    if signal.tp is not None:
        params["tp_trigger_px"] = str(signal.tp)
    if signal.sl is not None:
        params["sl_trigger_px"] = str(signal.sl)

    client = _get_client()
    resp = client.trading.place_order(**params)
    log.info(f"Market order response: {resp}")
    return resp


def amend_order(inst_id: str, order_id: str,
                new_sl: Optional[float] = None,
                new_tp: Optional[float] = None) -> dict:
    """Amend SL and/or TP on an existing open order."""
    params: dict = {"inst_id": inst_id, "ord_id": order_id}
    if new_sl is not None:
        params["new_sl_trigger_px"] = str(new_sl)
    if new_tp is not None:
        params["new_tp_trigger_px"] = str(new_tp)
    try:
        client = _get_client()
        resp = client.trading.amend_order(**params)
        log.info(f"Amend order response: {resp}")
        return resp
    except Exception as e:
        log.error(f"amend_order failed: {e}")
        return {}


def close_position_api(inst_id: str, position_side: str) -> dict:
    """Market-close an entire open position."""
    try:
        client = _get_client()
        resp = client.trading.close_position(
            inst_id=inst_id,
            margin_mode="cross",
            position_side=position_side,
        )
        log.info(f"Close position response: {resp}")
        return resp
    except Exception as e:
        log.error(f"close_position_api failed: {e}")
        return {}


def cancel_all_orders() -> dict:
    try:
        return _get_client().trading.cancel_all_orders()
    except Exception as e:
        log.error(f"cancel_all_orders failed: {e}")
        return {}
