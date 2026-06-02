"""
blofin_client.py — every BloFin exchange call the bot makes, in one place.

A thin wrapper over the `blofin` PyPI SDK that papers over its quirks:
  • the SDK hardcodes the LIVE URL and has no base_url arg → we patch it (demo vs live)
  • demo and live use DIFFERENT API keys → we pick the right set from the endpoint
  • order `size` is in CONTRACTS, not coins → specs come from get_contract_specs()
Credentials and BLOFIN_BASE_URL are read from the environment (.env).

Public interface:
  Read:  get_balance(), get_market_price(), get_contract_specs(), set_price_stream()
  Trade: place_order(), place_market_order(), amend_order(), reduce_position(),
         close_position_api(), set_leverage(), cancel_all_orders()

Reusable standalone: mostly — it depends on the `blofin` SDK, python-dotenv and the
project logger. get_market_price() will use a registered PriceStream cache if present
(set_price_stream) and otherwise falls back to a REST ticker call.
"""

import os
import sys
from typing import Optional

from blofin import BloFinClient as _BloFinClient
from blofin.utils import send_request as _send_request
from dotenv import load_dotenv
from logger import log

load_dotenv()

_client: Optional[_BloFinClient] = None


# ---------------------------------------------------------------------------
# Client setup (endpoint patching + demo/live credential selection)
# ---------------------------------------------------------------------------

def _patch_base_url(base_url: str) -> None:
    """
    The installed `blofin` SDK hardcodes the LIVE endpoint
    (https://openapi.blofin.com) in blofin.constants.REST_API_URL and does NOT
    accept a base_url constructor argument.  send_request() reads REST_API_URL
    as a module-level global (imported via `from blofin.constants import ...`),
    so a single constant edit won't reach the copies already bound in other
    modules.  We rebind REST_API_URL in every loaded blofin.* module that has it
    — this is the only way to point the SDK at the demo endpoint.
    """
    base_url = base_url.rstrip("/")
    patched = []
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("blofin"):
            continue
        if hasattr(mod, "REST_API_URL"):
            setattr(mod, "REST_API_URL", base_url)
            patched.append(mod_name)
    log.info(f"BloFin endpoint patched to {base_url} in modules: {patched}")


def _select_credentials(base_url: str) -> tuple[str, str, str]:
    """
    Pick the credential set that matches the endpoint.

    Demo and live BloFin issue DIFFERENT API keys. When BLOFIN_BASE_URL points
    at the demo endpoint we use the Demo-* keys; otherwise the live keys.
    Falls back to the live keys if the Demo-* vars aren't set.
    """
    is_demo = "demo" in base_url.lower()
    if is_demo:
        api_key = os.getenv("Demo-BloFinAPI") or os.getenv("BloFinAPI")
        secret = os.getenv("Demo-Blofin_secret_key") or os.getenv("Blofin_secret_key")
        passphrase = os.getenv("Demo-Passphrase") or os.getenv("Passphrase")
        log.info("Using DEMO BloFin credentials")
    else:
        api_key = os.getenv("BloFinAPI")
        secret = os.getenv("Blofin_secret_key")
        passphrase = os.getenv("Passphrase")
        log.info("Using LIVE BloFin credentials")
    return api_key, secret, passphrase


def _get_client() -> _BloFinClient:
    """Lazily build (and cache) the SDK client, patched to the configured endpoint
    and credentials. Reset the module-level `_client` to force a rebuild."""
    global _client
    if _client is None:
        base_url = os.getenv("BLOFIN_BASE_URL", "https://demo-trading-openapi.blofin.com")
        _patch_base_url(base_url)
        api_key, secret, passphrase = _select_credentials(base_url)
        _client = _BloFinClient(
            api_key=api_key,
            api_secret=secret,
            passphrase=passphrase,
        )
    return _client


def _extract_usdt_available(resp: dict) -> Optional[float]:
    """
    Pull available USDT out of a BloFin balance response, or None if absent.

    Handles both response shapes the API returns:
      /api/v1/asset/balances  → data: [{currency, available, ...}]        (flat list)
      /api/v1/account/balance → data: {details: [{currency, available}]}  (nested)
    """
    data = resp.get("data", {})

    # Shape A: data is a flat list of currency dicts
    if isinstance(data, list):
        rows = data
    # Shape B: data is a dict with a nested "details" list
    elif isinstance(data, dict):
        rows = data.get("details", [])
    else:
        rows = []

    for asset in rows:
        if asset.get("currency", "").upper() == "USDT":
            return float(asset.get("available", 0) or 0)
    return None


# ---------------------------------------------------------------------------
# Read-only market & account data
# ---------------------------------------------------------------------------

def get_balance() -> Optional[float]:
    """
    Return available USDT balance in the futures account.

    Returns None (NOT 0.0) on any API/auth failure so the caller can tell the
    difference between "account is empty" and "we couldn't reach the API".
    A silent 0.0 previously caused every trade to size to zero for days when
    the credentials/endpoint were misconfigured.
    """
    try:
        client = _get_client()
        # Primary: futures trading-account balance (correct margin balance for perps).
        resp = client.account.get_balance(account_type="futures")

        code = str(resp.get("code", "0"))
        if code != "0":
            log.error(
                f"get_balance API error {code}: {resp.get('msg', 'unknown')} "
                f"— check BloFin API key/secret/passphrase and BLOFIN_BASE_URL "
                f"(demo keys only work on the demo endpoint)."
            )
            return None

        usdt = _extract_usdt_available(resp)
        if usdt is None:
            log.warning(f"get_balance: no USDT entry in response: {resp}")
            return None
        return usdt
    except Exception as e:
        log.error(f"get_balance failed: {e}")
        return None


_instruments_cache: Optional[dict] = None


def _load_instruments() -> dict:
    """Fetch and cache all SWAP instrument specs, keyed by instId."""
    global _instruments_cache
    if _instruments_cache is None:
        try:
            resp = _get_client().public.get_instruments()
            data = resp.get("data", []) or []
            _instruments_cache = {d["instId"]: d for d in data if d.get("instId")}
            log.info(f"Loaded {len(_instruments_cache)} BloFin instrument specs")
        except Exception as e:
            log.error(f"Failed to load instruments: {e}")
            _instruments_cache = {}
    return _instruments_cache


def get_contract_specs(symbol: str) -> Optional[dict]:
    """
    Return contract specs for a symbol, or None if the instrument is unknown.

    BloFin order `size` is expressed in CONTRACTS, not base-currency coins:
      contract_value — base-currency amount per 1 contract (e.g. BTC: 0.001)
      lot_size       — size increment, in contracts (e.g. 0.1)
      min_size       — minimum order size, in contracts (e.g. 0.1)
      max_leverage   — exchange max leverage for the instrument
    """
    inst_id = symbol if "-USDT" in symbol else f"{symbol}-USDT"
    inst = _load_instruments().get(inst_id)
    if not inst:
        return None
    try:
        return {
            "contract_value": float(inst.get("contractValue", 0) or 0),
            "lot_size": float(inst.get("lotSize", 0) or 0),
            "min_size": float(inst.get("minSize", 0) or 0),
            "max_leverage": float(inst.get("maxLeverage", 0) or 0),
        }
    except (TypeError, ValueError) as e:
        log.warning(f"get_contract_specs({symbol}) parse error: {e}")
        return None


def set_leverage(symbol: str, leverage: int, margin_mode: str = "cross") -> int:
    """
    Set leverage for a symbol before placing an order.

    The requested leverage is clamped to the instrument's exchange maximum
    (e.g. some alts cap at 75x while BTC allows 150x). Returns the leverage
    actually applied (after clamping). The SDK doesn't wrap this endpoint, so
    we POST to /api/v1/account/set-leverage directly.
    """
    inst_id = symbol if "-USDT" in symbol else f"{symbol}-USDT"
    specs = get_contract_specs(symbol)
    max_lev = int(specs["max_leverage"]) if specs and specs.get("max_leverage") else int(leverage)
    lev = max(1, min(int(leverage), max_lev))

    data = {"instId": inst_id, "leverage": str(lev), "marginMode": margin_mode}
    try:
        client = _get_client()
        resp = _send_request("POST", "/api/v1/account/set-leverage",
                             client.auth, data=data, authenticate=True)
        code = str(resp.get("code", "0"))
        if code != "0":
            log.warning(f"set_leverage {inst_id} {lev}x failed: {resp.get('msg', resp)}")
        else:
            clamp_note = f" (clamped from {leverage}x, max {max_lev}x)" if lev != int(leverage) else ""
            log.info(f"Set leverage {inst_id} → {lev}x {margin_mode}{clamp_note}")
    except Exception as e:
        log.warning(f"set_leverage {inst_id} error: {e}")
    return lev


_price_stream = None  # optional PriceStream, set by the bot at startup


def set_price_stream(stream):
    """Register a live PriceStream whose cache get_market_price() prefers."""
    global _price_stream
    _price_stream = stream


def get_market_price(symbol: str) -> Optional[float]:
    """
    Last traded price for a symbol. Prefers the live WS price cache when it has a
    fresh tick; otherwise falls back to a REST ticker lookup. (On demo the stream
    is sparse, so REST does most of the work; on live the cache serves it instantly.)
    """
    inst_id = symbol if "-USDT" in symbol else f"{symbol}-USDT"

    if _price_stream is not None:
        try:
            max_age = float(os.getenv("PRICE_MAX_AGE", "15"))
            cached = _price_stream.get(inst_id, max_age=max_age)
            if cached:
                return cached
        except Exception:
            pass

    try:
        client = _get_client()
        # SDK exposes market data on the `public` API, not `market`.
        resp = client.public.get_tickers(inst_id=inst_id)
        data = resp.get("data", [])
        if isinstance(data, list) and data:
            return float(data[0].get("last", 0) or 0) or None
        return None
    except Exception as e:
        log.warning(f"get_market_price({symbol}) failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Order placement & management
# ---------------------------------------------------------------------------

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


def reduce_position(symbol: str, side: str, size: float) -> dict:
    """
    Partially close an open position with a reduce-only market order.
    `side` is the ORIGINAL position side ("buy"/"sell"); we send the opposite.
    """
    inst_id = symbol if "-USDT" in symbol else f"{symbol}-USDT"
    position_side = "long" if side == "buy" else "short"
    close_side = "sell" if side == "buy" else "buy"
    try:
        client = _get_client()
        resp = client.trading.place_order(
            inst_id=inst_id,
            margin_mode="cross",
            position_side=position_side,
            side=close_side,
            order_type="market",
            size=str(size),
            reduce_only=True,
        )
        log.info(f"Reduce-only close {size} {inst_id} ({close_side}): {resp}")
        return resp
    except Exception as e:
        log.error(f"reduce_position failed: {e}")
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
    """Cancel every open order on the account (emergency / cleanup helper)."""
    try:
        return _get_client().trading.cancel_all_orders()
    except Exception as e:
        log.error(f"cancel_all_orders failed: {e}")
        return {}
