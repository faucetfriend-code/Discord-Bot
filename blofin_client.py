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
Order endpoints are POSTed directly with the documented JSON bodies (the SDK's
place_order splices unknown kwargs raw into the body and lacks amend/cancel-all).

Reusable standalone: mostly — it depends on the `blofin` SDK, python-dotenv and the
project logger. get_market_price() will use a registered PriceStream cache if present
(set_price_stream) and otherwise falls back to a REST ticker call.
"""

import os
import sys
import time
from typing import Optional

from blofin import BloFinClient as _BloFinClient
from blofin.utils import send_request as _send_request
from dotenv import load_dotenv
from logger import log

load_dotenv()

# Pin outbound HTTPS to IPv4 before any BloFin request (IP-whitelisted keys).
import net_prefs as _net_prefs  # noqa: E402

_net_prefs.force_ipv4()

# Last API/auth error seen by get_balance(): {"code": str, "msg": str} or None.
# Lets preflight surface the real failure reason without changing the
# Optional[float] contract every existing caller relies on.
last_balance_error: Optional[dict] = None

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


# ---------------------------------------------------------------------------
# Broker ID (Broker/MCP-type API keys reject orders without one: code 152012)
# ---------------------------------------------------------------------------

DEMO_BASE_URL = "https://demo-trading-openapi.blofin.com"
LIVE_BASE_URL = "https://openapi.blofin.com"
# Same default as BloFin's official MCP server (github.com/blofin/blofin-mcp).
DEFAULT_BROKER_ID = "dd3511977f23cc87"

_broker_id_logged = False


def _active_base_url() -> str:
    return os.getenv("BLOFIN_BASE_URL", DEMO_BASE_URL).strip().rstrip("/")


def _is_live_url(base_url: str) -> bool:
    return base_url.strip().rstrip("/") == LIVE_BASE_URL


def _broker_id() -> Optional[str]:
    """
    Resolve the brokerId to send with order-creating requests, mirroring the
    official BloFin MCP server:

      demo endpoint                  -> None (demo never takes a brokerId)
      BLOFIN_BROKER_ID == "none"     -> None (Transaction-type keys: 152011
                                        if one is sent)
      BLOFIN_BROKER_ID set           -> that value
      unset, live endpoint           -> DEFAULT_BROKER_ID
    """
    if not _is_live_url(_active_base_url()):
        return None
    env = os.getenv("BLOFIN_BROKER_ID")
    if env is not None:
        val = env.strip()
        if val.lower() == "none":
            return None
        if val:
            return val
    return DEFAULT_BROKER_ID


def _with_broker_id(body: dict) -> dict:
    """Return `body` with brokerId added when one resolves (logged once)."""
    global _broker_id_logged
    bid = _broker_id()
    if bid is None:
        return body
    if not _broker_id_logged:
        log.info(f"BloFin brokerId in use: {bid}")
        _broker_id_logged = True
    out = dict(body)
    out["brokerId"] = bid
    return out


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
    global last_balance_error
    last_balance_error = None
    try:
        client = _get_client()
        # Primary: futures trading-account balance (correct margin balance for perps).
        resp = client.account.get_balance(account_type="futures")

        code = str(resp.get("code", "0"))
        if code != "0":
            last_balance_error = {"code": code, "msg": str(resp.get("msg", "unknown"))}
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
        last_balance_error = {"code": "exception", "msg": f"{type(e).__name__}: {e}"}
        log.error(f"get_balance failed: {e}")
        return None


_instruments_cache: Optional[dict] = None
_instruments_ts: float = 0.0


def _load_instruments() -> dict:
    """Fetch and cache all SWAP instrument specs, keyed by instId.

    The cache has a TTL (INSTRUMENT_CACHE_TTL_MIN, default 360 = 6h) so symbols
    newly listed mid-session are picked up without restarting the bot. On a
    refresh failure the previous good cache is kept rather than blanked."""
    global _instruments_cache, _instruments_ts
    ttl = float(os.getenv("INSTRUMENT_CACHE_TTL_MIN", "360")) * 60
    if _instruments_cache is not None and (time.time() - _instruments_ts) < ttl:
        return _instruments_cache
    try:
        resp = _get_client().public.get_instruments()
        data = resp.get("data", []) or []
        loaded = {d["instId"]: d for d in data if d.get("instId")}
        if loaded:                       # only swap in a non-empty fetch
            _instruments_cache = loaded
            _instruments_ts = time.time()
            log.info(f"Loaded {len(_instruments_cache)} BloFin instrument specs")
    except Exception as e:
        log.error(f"Failed to load instruments: {e}")
    if _instruments_cache is None:       # first load failed outright
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


def get_recent_close(symbol: str, bar: str = "1H") -> Optional[float]:
    """
    Close price of the most recently CLOSED candle on the `bar` timeframe,
    skipping the still-forming candle. Used by 'soft' stops, which only trigger
    when a candle CLOSES beyond the level (not on an intrabar wick).

    BloFin returns candles newest-first as [ts, open, high, low, close, vol, …,
    confirm] where the trailing flag is "1" for a closed candle and "0" for the
    one currently forming. Returns None on any failure.
    """
    inst_id = symbol if "-USDT" in symbol else f"{symbol}-USDT"
    try:
        resp = _get_client().public.get_candlesticks(inst_id=inst_id, bar=bar, limit=2)
        data = resp.get("data", []) if isinstance(resp, dict) else []
        for row in data:                      # newest first → first confirmed = last close
            if len(row) >= 5 and str(row[-1]) == "1":
                return float(row[4]) or None
        if len(data) >= 2 and len(data[1]) >= 5:   # fallback: candle before the forming one
            return float(data[1][4]) or None
        return None
    except Exception as e:
        log.warning(f"get_recent_close({symbol}) failed: {e}")
        return None


def get_funding_rate(symbol: str) -> Optional[float]:
    """Current perpetual funding rate for a symbol (e.g. -0.0000529), or None.
    BloFin charges/credits funding every 8h; used to model holding cost."""
    inst_id = symbol if "-USDT" in symbol else f"{symbol}-USDT"
    try:
        resp = _get_client().public.get_funding_rate(inst_id=inst_id)
        data = resp.get("data", []) if isinstance(resp, dict) else []
        if isinstance(data, list) and data:
            return float(data[0].get("fundingRate", 0) or 0)
        return None
    except Exception as e:
        log.warning(f"get_funding_rate({symbol}) failed: {e}")
        return None


def get_recent_fills(symbol: str) -> list[dict]:
    """Recent fills for a symbol from the exchange trade history, newest first.
    Each dict carries at least {price, size, fee, side, ts}. Used on LIVE to read
    the exchange's real fees and the exit price of an externally-closed position."""
    inst_id = symbol if "-USDT" in symbol else f"{symbol}-USDT"
    try:
        resp = _get_client().trading.get_trade_history(inst_id=inst_id)
        data = resp.get("data", []) if isinstance(resp, dict) else []
        out = []
        for f in data if isinstance(data, list) else []:
            out.append({
                "price": float(f.get("fillPrice", f.get("price", 0)) or 0),
                "size": float(f.get("fillSize", f.get("size", 0)) or 0),
                "fee": abs(float(f.get("fee", 0) or 0)),
                "side": f.get("side", ""),
                "ts": f.get("ts", ""),
            })
        return out
    except Exception as e:
        log.warning(f"get_recent_fills({symbol}) failed: {e}")
        return []


def get_live_positions() -> list[dict]:
    """Open positions as the EXCHANGE sees them, normalized to
    {symbol, side, size, avg_price, liq_price}. Empty list on failure or no
    positions. Used by live reconciliation to detect external closes/liquidations."""
    try:
        resp = _get_client().trading.get_positions()
        data = resp.get("data", []) if isinstance(resp, dict) else []
        out = []
        for p in data if isinstance(data, list) else []:
            size = float(p.get("positions", p.get("pos", 0)) or 0)
            if size == 0:
                continue
            ps = (p.get("positionSide") or "").lower()
            side = "buy" if ps == "long" else "sell" if ps == "short" else \
                   ("buy" if size > 0 else "sell")
            out.append({
                "symbol": p.get("instId", ""),
                "side": side,
                "size": abs(size),
                "avg_price": float(p.get("averagePrice", p.get("avgPx", 0)) or 0),
                "liq_price": float(p.get("liquidationPrice", p.get("liqPx", 0)) or 0),
            })
        return out
    except Exception as e:
        log.warning(f"get_live_positions failed: {e}")
        return []


def get_account_summary() -> Optional[dict]:
    """{balance, equity, free_margin} from the futures account, or None on failure.
    `free_margin` is the available (non-frozen) USDT; `equity` includes unrealized PnL."""
    try:
        resp = _get_client().trading.get_futures_account_balance()
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        equity = float(data.get("totalEquity", 0) or 0)
        bal = free = None
        for d in data.get("details", []) or []:
            if d.get("currency") == "USDT":
                bal = float(d.get("balance", 0) or 0)
                free = float(d.get("available", d.get("availableEquity", 0)) or 0)
                break
        return {"balance": bal, "equity": equity, "free_margin": free}
    except Exception as e:
        log.warning(f"get_account_summary failed: {e}")
        return None


def _get_account_mode(getter_name: str, field: str) -> Optional[str]:
    """Shared body for get_position_mode / get_margin_mode: call the named
    `client.trading` getter and return data[field] as a string, or None on any
    API error (non-zero code, missing field, exception). Errors log at warning."""
    try:
        resp = getattr(_get_client().trading, getter_name)()
        if not isinstance(resp, dict):
            log.warning(f"{getter_name}: unexpected response type {type(resp).__name__}")
            return None
        code = str(resp.get("code", "0"))
        if code != "0":
            log.warning(f"{getter_name} API error {code}: {resp.get('msg', 'unknown')}")
            return None
        data = resp.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        value = data.get(field) if isinstance(data, dict) else None
        if not value:
            log.warning(f"{getter_name}: no '{field}' in response: {resp}")
            return None
        return str(value)
    except Exception as e:
        log.warning(f"{getter_name} failed: {e}")
        return None


def get_position_mode() -> Optional[str]:
    """'long_short_mode' (hedge) or 'net_mode' for the account, or None on failure."""
    return _get_account_mode("get_position_mode", "positionMode")


def get_margin_mode() -> Optional[str]:
    """'cross' or 'isolated' for the account, or None on failure."""
    return _get_account_mode("get_margin_mode", "marginMode")


# ---------------------------------------------------------------------------
# Order placement & management
# ---------------------------------------------------------------------------

class OrderRejected(Exception):
    """
    Raised when BloFin accepts the HTTP call (200) but rejects the order itself.

    A non-zero business `code` (or per-order `sCode`), or a success code with no
    `ordId`, means the order never reached the book - insufficient balance, symbol
    delisted on this endpoint, bad price, etc. Treating that as success would record
    a phantom position the exchange never opened.
    """

    def __init__(self, code, msg, resp=None):
        self.code = str(code)
        self.msg = str(msg or "")
        self.resp = resp
        super().__init__(f"order rejected (code {self.code}): {self.msg}")


def _order_id_from_resp(resp: dict) -> str:
    """Pull the exchange order id out of a place-order response, or '' if absent.

    The documented Place Order response key is `orderId` (data is a list of
    one dict). `ordId` is accepted too for older/alternate payloads."""
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return ""
    return str(data.get("orderId") or data.get("ordId") or "")


def _validate_order_resp(resp: dict) -> str:
    """
    Confirm BloFin actually accepted the order; return its order id on success.

    The HTTP layer can return 200 with a non-zero business `code` - that is a
    rejection, not a success. Each entry of `data` also carries its own
    `code`/`msg` (docs) - older payloads used `sCode`/`sMsg` - and we require a
    non-empty `orderId`. Raises OrderRejected on any of these, so the caller
    never records an unfilled order as "executed".
    """
    if not isinstance(resp, dict):
        raise OrderRejected("unknown", f"unexpected response type: {type(resp).__name__}", resp)
    code = str(resp.get("code", "0"))
    if code != "0":
        raise OrderRejected(code, resp.get("msg", ""), resp)
    data = resp.get("data")
    first = data[0] if isinstance(data, list) and data else data
    if isinstance(first, dict):
        scode = str(first.get("code", first.get("sCode", "0")) or "0")
        if scode != "0":
            raise OrderRejected(scode, first.get("msg") or first.get("sMsg")
                                or resp.get("msg", ""), resp)
    ord_id = _order_id_from_resp(resp)
    if not ord_id:
        raise OrderRejected(code, resp.get("msg", "") or "no orderId in response", resp)
    return ord_id


# Endpoints (documented at https://docs.blofin.com - Trading > REST API).
ORDER_PATH = "/api/v1/trade/order"
AMEND_ORDER_PATH = "/api/v1/trade/amend-order"
CANCEL_BATCH_PATH = "/api/v1/trade/cancel-batch-orders"
ORDERS_PENDING_PATH = "/api/v1/trade/orders-pending"
TPSL_ORDER_PATH = "/api/v1/trade/order-tpsl"
TPSL_AMEND_PATH = "/api/v1/trade/amend-tpsl"
TPSL_CANCEL_PATH = "/api/v1/trade/cancel-tpsl"
TPSL_PENDING_PATH = "/api/v1/trade/orders-tpsl-pending"
CLOSE_POSITION_PATH = "/api/v1/trade/close-position"
CANCEL_BATCH_MAX = 20
MARKET_ORDER_PRICE = "-1"   # docs: -1 = execute the TP/SL leg at market


def _post(path: str, body):
    """Signed POST through the SDK transport. `body` is a dict or a JSON array."""
    client = _get_client()
    return _send_request("POST", path, client.auth, data=body, authenticate=True)


def _get(path: str, params: Optional[dict] = None):
    """Signed GET through the SDK transport."""
    client = _get_client()
    return _send_request("GET", path, client.auth, params=params or {},
                         authenticate=True)


def _inst(symbol: str) -> str:
    return symbol if "-USDT" in symbol else f"{symbol}-USDT"


_position_mode_cache: Optional[str] = None


def _exchange_position_side(side: str) -> str:
    """Map the bot's "buy"/"sell" to the positionSide BloFin expects.

    Hedge (long_short_mode) accounts need "long"/"short"; one-way (net_mode)
    accounts reject those and need "net". The account mode is read once per
    process (read-only call) and falls back to long/short if unreadable, which
    matches the bot's prior behaviour.
    """
    global _position_mode_cache
    if _position_mode_cache is None:
        _position_mode_cache = get_position_mode() or "long_short_mode"
    if _position_mode_cache == "net_mode":
        return "net"
    return "long" if side == "buy" else "short"


def _attach_exchange_tp() -> bool:
    """The bot runs its own TP ladder (partial closes), so an exchange-side TP
    would fight it by closing the whole position at signal.tp. Off unless
    ATTACH_EXCHANGE_TP=true."""
    return os.getenv("ATTACH_EXCHANGE_TP", "false").strip().lower() == "true"


def _entry_body(signal, size: float, order_type: str) -> dict:
    """Documented Place Order body for a new entry, SL backstop attached."""
    body = {
        "instId": _inst(signal.symbol),
        "marginMode": "cross",
        "positionSide": _exchange_position_side(signal.side),
        "side": signal.side,
        "orderType": order_type,
        "size": str(size),
        "reduceOnly": "false",
    }
    if order_type != "market":
        body["price"] = str(signal.entry)
    sl = getattr(signal, "sl", None)
    if sl is not None:
        body["slTriggerPrice"] = str(sl)
        body["slOrderPrice"] = MARKET_ORDER_PRICE
        body["slTriggerPriceType"] = "last"
    tp = getattr(signal, "tp", None)
    if tp is not None and _attach_exchange_tp():
        body["tpTriggerPrice"] = str(tp)
        body["tpOrderPrice"] = MARKET_ORDER_PRICE
        body["tpTriggerPriceType"] = "last"
    return _with_broker_id(body)


def place_order(signal, size: float) -> dict:
    """
    Place a limit entry with the stop-loss attached (executes at market when
    the last price hits signal.sl). TP is attached only if ATTACH_EXCHANGE_TP
    is true - the bot manages its own TP ladder.

    Body sent (docs "Place Order"): instId, marginMode, positionSide, side,
    orderType=limit, price, size, reduceOnly="false", slTriggerPrice,
    slOrderPrice="-1", slTriggerPriceType="last" [, tp*] [, brokerId].
    Returns the raw API response dict; raises OrderRejected if not accepted.
    """
    body = _entry_body(signal, size, "limit")
    resp = _post(ORDER_PATH, body)
    log.info(f"Order response: {resp}")
    _validate_order_resp(resp)
    return resp


def place_market_order(signal, size: float) -> dict:
    """
    Place a market entry (no `price` field) with the SL attached when the
    signal carries one. Same body as place_order minus price, orderType=market.
    """
    body = _entry_body(signal, size, "market")
    resp = _post(ORDER_PATH, body)
    log.info(f"Market order response: {resp}")
    _validate_order_resp(resp)
    return resp


def _ok(resp) -> bool:
    if not isinstance(resp, dict) or str(resp.get("code", "1")) != "0":
        return False
    data = resp.get("data")
    first = data[0] if isinstance(data, list) and data else data
    if isinstance(first, dict) and str(first.get("code", "0") or "0") != "0":
        return False
    return True


def _pending_tpsl(inst_id: str, position_side: Optional[str]) -> list[dict]:
    """Untriggered TP/SL orders for an instrument (optionally one positionSide),
    those carrying a stop-loss leg first."""
    resp = _get(TPSL_PENDING_PATH, {"instId": inst_id, "limit": "100"})
    rows = resp.get("data", []) if isinstance(resp, dict) else []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if position_side and (r.get("positionSide") or "").lower() != position_side:
            continue
        out.append(r)
    out.sort(key=lambda r: 0 if r.get("slTriggerPrice") else 1)
    return out


def _place_tpsl(inst_id: str, position_side: str, close_side: str, size: str,
                new_sl: Optional[float], new_tp: Optional[float]) -> dict:
    body = {
        "instId": inst_id,
        "marginMode": "cross",
        "positionSide": position_side,
        "side": close_side,
        "size": size,
        "reduceOnly": "true",
    }
    if new_sl is not None:
        body["slTriggerPrice"] = str(new_sl)
        body["slOrderPrice"] = MARKET_ORDER_PRICE
        body["slTriggerPriceType"] = "last"
    if new_tp is not None:
        body["tpTriggerPrice"] = str(new_tp)
        body["tpOrderPrice"] = MARKET_ORDER_PRICE
        body["tpTriggerPriceType"] = "last"
    return _post(TPSL_ORDER_PATH, _with_broker_id(body))


def amend_order(inst_id: str, order_id: str,
                new_sl: Optional[float] = None,
                new_tp: Optional[float] = None,
                side: Optional[str] = None) -> dict:
    """Move the SL (and/or TP) protecting a position, wherever it lives.

    Three-step strategy, simplest object first:
      1. POST /api/v1/trade/amend-order on the ENTRY order (newSlTriggerPrice /
         newSlOrderPrice="-1", newTp*). Works while the entry is still live or
         partially filled - the attached TP/SL is amended in place.
      2. If that is rejected (entry already filled: the attached SL then exists
         as a TP/SL order), GET /api/v1/trade/orders-tpsl-pending for instId
         (+ positionSide when `side` is given) and POST /api/v1/trade/amend-tpsl
         on the first row carrying a stop-loss leg. If amend-tpsl is rejected,
         cancel that row (/trade/cancel-tpsl) and re-place it (/trade/order-tpsl).
      3. If no pending TP/SL exists (e.g. the attached SL never landed), place a
         fresh reduce-only TP/SL order via /api/v1/trade/order-tpsl sized to the
         live position (size from GET positions).

    `side` is the ORIGINAL position side ("buy"/"sell"); pass it so hedge-mode
    accounts holding both a long and a short on one symbol amend the right one.
    Returns the response of whichever step succeeded; {} when all failed.
    Never raises.
    """
    if new_sl is None and new_tp is None:
        return {}
    inst_id = _inst(inst_id)
    position_side = _exchange_position_side(side) if side else None
    try:
        body: dict = {"instId": inst_id, "orderId": str(order_id)}
        if new_sl is not None:
            body["newSlTriggerPrice"] = str(new_sl)
            body["newSlOrderPrice"] = MARKET_ORDER_PRICE
        if new_tp is not None:
            body["newTpTriggerPrice"] = str(new_tp)
            body["newTpOrderPrice"] = MARKET_ORDER_PRICE
        resp = _post(AMEND_ORDER_PATH, body)
        log.info(f"Amend order response: {resp}")
        if _ok(resp):
            return resp
        log.info(f"amend-order rejected ({resp.get('msg', resp)}); "
                 f"falling back to the TP/SL order for {inst_id}")

        rows = _pending_tpsl(inst_id, position_side)
        if rows:
            row = rows[0]
            tpsl_id = str(row.get("tpslId", ""))
            body = {"instId": inst_id, "tpslId": tpsl_id}
            if new_sl is not None:
                body["newSlTriggerPrice"] = str(new_sl)
                body["newSlOrderPrice"] = MARKET_ORDER_PRICE
            if new_tp is not None:
                body["newTpTriggerPrice"] = str(new_tp)
                body["newTpOrderPrice"] = MARKET_ORDER_PRICE
            resp = _post(TPSL_AMEND_PATH, body)
            log.info(f"Amend TP/SL response: {resp}")
            if _ok(resp):
                return resp
            log.warning(f"amend-tpsl rejected ({resp.get('msg', resp)}); "
                        f"cancelling and re-placing the TP/SL order")
            cancel = _post(TPSL_CANCEL_PATH, [{"instId": inst_id, "tpslId": tpsl_id}])
            log.info(f"Cancel TP/SL response: {cancel}")
            ps = (row.get("positionSide") or position_side or "net").lower()
            close_side = row.get("side") or ("sell" if ps == "long" else "buy")
            resp = _place_tpsl(inst_id, ps, close_side, str(row.get("size", "")),
                               new_sl, new_tp)
            log.info(f"Re-place TP/SL response: {resp}")
            return resp if _ok(resp) else {}

        if side is None:
            log.error(f"amend_order {inst_id}: no pending TP/SL order and no side "
                      f"given to place a new one")
            return {}
        live = [p for p in get_live_positions()
                if p["symbol"] == inst_id and p["side"] == side]
        if not live:
            log.error(f"amend_order {inst_id}: no live {side} position to protect")
            return {}
        close_side = "sell" if side == "buy" else "buy"
        resp = _place_tpsl(inst_id, position_side or "net", close_side,
                           str(live[0]["size"]), new_sl, new_tp)
        log.info(f"New TP/SL order response: {resp}")
        return resp if _ok(resp) else {}
    except Exception as e:
        log.error(f"amend_order failed: {e}")
        return {}


def reduce_position(symbol: str, side: str, size: float) -> dict:
    """
    Partially close an open position with a reduce-only market order.
    `side` is the ORIGINAL position side ("buy"/"sell"); we send the opposite
    side with the SAME positionSide (hedge mode: close a long = sell + long).

    Body: instId, marginMode, positionSide, side, orderType=market, size,
    reduceOnly="true" [, brokerId].
    """
    inst_id = _inst(symbol)
    close_side = "sell" if side == "buy" else "buy"
    try:
        body = _with_broker_id({
            "instId": inst_id,
            "marginMode": "cross",
            "positionSide": _exchange_position_side(side),
            "side": close_side,
            "orderType": "market",
            "size": str(size),
            "reduceOnly": "true",
        })
        resp = _post(ORDER_PATH, body)
        log.info(f"Reduce-only close {size} {inst_id} ({close_side}): {resp}")
        return resp
    except Exception as e:
        log.error(f"reduce_position failed: {e}")
        return {}


def close_position_api(inst_id: str, position_side: str) -> dict:
    """Market-close an entire open position.

    The SDK's close_positions() has no brokerId kwarg (and this module used
    to call a non-existent `close_position`), so POST the documented body to
    /api/v1/trade/close-position directly, signed through the SDK auth.
    `position_side` is "long"/"short" from the bot; on a net_mode account it
    is translated to "net".
    """
    try:
        if _exchange_position_side("buy") == "net":
            position_side = "net"
        body = _with_broker_id({
            "instId": _inst(inst_id),
            "marginMode": "cross",
            "positionSide": position_side,
        })
        resp = _post(CLOSE_POSITION_PATH, body)
        log.info(f"Close position response: {resp}")
        return resp
    except Exception as e:
        log.error(f"close_position_api failed: {e}")
        return {}


def _paged(path: str, params: dict, id_key: str) -> list[dict]:
    """Walk a paginated GET (limit 100, `after` = last id) and return all rows."""
    rows: list[dict] = []
    after = None
    for _ in range(50):
        q = dict(params, limit="100")
        if after:
            q["after"] = after
        resp = _get(path, q)
        data = resp.get("data", []) if isinstance(resp, dict) else []
        if not isinstance(data, list) or not data:
            break
        rows.extend(data)
        if len(data) < 100:
            break
        after = str(data[-1].get(id_key, "") or "")
        if not after:
            break
    return rows


def _cancel_batches(path: str, items: list[dict], summary: dict, key: str) -> None:
    for i in range(0, len(items), CANCEL_BATCH_MAX):
        chunk = items[i:i + CANCEL_BATCH_MAX]
        try:
            resp = _post(path, chunk)
        except Exception as e:
            summary["errors"].append(f"{path}: {e}")
            continue
        data = resp.get("data", []) if isinstance(resp, dict) else []
        if str(resp.get("code", "1")) != "0" and not data:
            summary["errors"].append(f"{path}: {resp.get('msg', resp)}")
            continue
        for r in data if isinstance(data, list) else []:
            if str(r.get("code", "0") or "0") == "0":
                summary[key] += 1
            else:
                summary["errors"].append(f"{r.get('orderId') or r.get('tpslId')}: "
                                         f"{r.get('msg', '')}")


def cancel_all_orders(inst_id: Optional[str] = None) -> dict:
    """Cancel every pending order AND every untriggered TP/SL order on the
    account (optionally just one instrument). Emergency / cleanup helper.

    Flow: GET /api/v1/trade/orders-pending -> POST /api/v1/trade/cancel-batch-orders
    in batches of 20 ([{instId, orderId}, ...]); GET /api/v1/trade/orders-tpsl-pending
    -> POST /api/v1/trade/cancel-tpsl in batches of 20 ([{instId, tpslId}, ...]).
    Returns {orders_found, orders_cancelled, tpsl_found, tpsl_cancelled, errors}.
    """
    summary = {"orders_found": 0, "orders_cancelled": 0,
               "tpsl_found": 0, "tpsl_cancelled": 0, "errors": []}
    params = {"instId": _inst(inst_id)} if inst_id else {}
    try:
        orders = _paged(ORDERS_PENDING_PATH, params, "orderId")
        summary["orders_found"] = len(orders)
        items = [{"instId": o.get("instId", ""), "orderId": str(o.get("orderId", ""))}
                 for o in orders if o.get("orderId")]
        _cancel_batches(CANCEL_BATCH_PATH, items, summary, "orders_cancelled")

        tpsl = _paged(TPSL_PENDING_PATH, params, "tpslId")
        summary["tpsl_found"] = len(tpsl)
        items = [{"instId": t.get("instId", ""), "tpslId": str(t.get("tpslId", ""))}
                 for t in tpsl if t.get("tpslId")]
        _cancel_batches(TPSL_CANCEL_PATH, items, summary, "tpsl_cancelled")
    except Exception as e:
        log.error(f"cancel_all_orders failed: {e}")
        summary["errors"].append(str(e))
    log.info(f"cancel_all_orders: {summary}")
    return summary


# ---------------------------------------------------------------------------
# Order status (resting-limit lifecycle + market fill confirmation)
# ---------------------------------------------------------------------------

CANCEL_ORDER_PATH = "/api/v1/trade/cancel-order"
ORDERS_HISTORY_PATH = "/api/v1/trade/orders-history"
FILLS_HISTORY_PATH = "/api/v1/trade/fills-history"


def _order_row_status(row: dict, pending: bool) -> dict:
    """Normalise one orders-pending / orders-history row."""
    return {
        "state": str(row.get("state") or ("live" if pending else "unknown")),
        "pending": pending,
        "filled_size": float(row.get("filledSize", 0) or 0),
        "avg_price": float(row.get("averagePrice", row.get("avgPx", 0)) or 0),
        "size": float(row.get("size", 0) or 0),
        "source": "orders-pending" if pending else "orders-history",
    }


def get_fills_for_order(symbol: str, order_id: str) -> tuple[float, float]:
    """(filled_size, avg_fill_price) aggregated over GET /api/v1/trade/fills-history
    rows for one orderId; (0.0, 0.0) when none. Never raises."""
    inst_id = _inst(symbol)
    try:
        resp = _get(FILLS_HISTORY_PATH, {"instId": inst_id, "orderId": str(order_id),
                                         "limit": "100"})
        data = resp.get("data", []) if isinstance(resp, dict) else []
        tot = notional = 0.0
        for f in data if isinstance(data, list) else []:
            if str(f.get("orderId", order_id)) != str(order_id):
                continue
            sz = float(f.get("fillSize", f.get("size", 0)) or 0)
            px = float(f.get("fillPrice", f.get("price", 0)) or 0)
            tot += sz
            notional += sz * px
        return (tot, notional / tot) if tot > 0 else (0.0, 0.0)
    except Exception as e:
        log.warning(f"get_fills_for_order({symbol}, {order_id}) failed: {e}")
        return (0.0, 0.0)


def get_order_status(symbol: str, order_id: str) -> Optional[dict]:
    """Where is this entry order now?

    Lookup order (cheapest/most authoritative first):
      1. GET /api/v1/trade/orders-pending?instId=  -> still on the book
         (state live / partially_filled, filledSize, averagePrice)
      2. GET /api/v1/trade/orders-history?instId=&limit=100 -> done
         (state filled / canceled / partially_filled, filledSize, averagePrice)
      3. GET /api/v1/trade/fills-history?instId=&orderId= -> fills only
         (state derived: "filled" when any fill exists)

    Returns {state, pending, filled_size, avg_price, size, source}; state
    "unknown" with pending=False when the order is in none of the three (API lag
    or a very old order). None only when the FIRST call raised (transport/auth
    error), so callers can tell "cannot see the exchange" from "order is gone".
    """
    inst_id = _inst(symbol)
    oid = str(order_id)
    try:
        resp = _get(ORDERS_PENDING_PATH, {"instId": inst_id, "limit": "100"})
    except Exception as e:
        log.warning(f"get_order_status({symbol}, {oid}) orders-pending failed: {e}")
        return None
    data = resp.get("data", []) if isinstance(resp, dict) else []
    for row in data if isinstance(data, list) else []:
        if str(row.get("orderId", "")) == oid:
            return _order_row_status(row, pending=True)
    try:
        resp = _get(ORDERS_HISTORY_PATH, {"instId": inst_id, "limit": "100"})
        data = resp.get("data", []) if isinstance(resp, dict) else []
        for row in data if isinstance(data, list) else []:
            if str(row.get("orderId", "")) == oid:
                return _order_row_status(row, pending=False)
    except Exception as e:
        log.warning(f"get_order_status({symbol}, {oid}) orders-history failed: {e}")
    filled, avg = get_fills_for_order(symbol, oid)
    if filled > 0:
        return {"state": "filled", "pending": False, "filled_size": filled,
                "avg_price": avg, "size": filled, "source": "fills-history"}
    return {"state": "unknown", "pending": False, "filled_size": 0.0,
            "avg_price": 0.0, "size": 0.0, "source": "none"}


def cancel_order(symbol: str, order_id: str) -> bool:
    """POST /api/v1/trade/cancel-order {instId, orderId}. True when the exchange
    accepted the cancel (code 0 and, if present, per-order code 0). Never raises."""
    body = {"instId": _inst(symbol), "orderId": str(order_id)}
    try:
        resp = _post(CANCEL_ORDER_PATH, body)
    except Exception as e:
        log.error(f"cancel_order({symbol}, {order_id}) failed: {e}")
        return False
    log.info(f"Cancel order response: {resp}")
    return _ok(resp)
