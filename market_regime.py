"""
ADX-based market regime detection for shadow-log quality filters.

  get_adx_regime(symbol, tf)  → (adx_value, regime_label)
      The traded symbol's own trend strength on the given timeframe.
      Used to annotate RSI Extreme shadow rows — ranging_calm favours fades,
      trending_strong is the falling-knife / rising-knife condition.

  get_btc_htf_regime(tf)      → (direction, adx_value, regime_label)
      BTC macro context (typically 4H).  Used by OracleAlgo as a quantitative
      fallback when no text-parsed 4H bias signal has arrived yet.

Both functions pull from Gate.io public candles (no API key required) and
cache results for CACHE_TTL seconds so repeated calls within a window are
free.  Returns (None, None) / (None, None, None) when the pair isn't listed
or the fetch fails — callers treat None as "no data" and leave the shadow
field blank.
"""

import json
import time
import urllib.request
import urllib.error
from typing import Optional

_GATE_URL = ("https://api.gateio.ws/api/v4/spot/candlesticks"
             "?currency_pair={pair}&interval={tf}&limit={n}")
_TF_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}

_CACHE: dict = {}
CACHE_TTL = 300.0   # seconds; one request per symbol/tf per 5 min


def _fetch_ohlcv(symbol: str, tf: str, n: int = 60) -> list:
    """Fetch n candles from Gate.io.  Returns list of (t, open, high, low, close)
    sorted ascending.  Empty list on any error."""
    base = symbol.replace("-USDT", "")
    # Gate.io sometimes renames coins with a trailing digit (e.g. SKYAI1 → SKYAI);
    # try the plain base first, then strip any trailing digit.
    tries = [base]
    if base and base[-1].isdigit():
        tries.append(base.rstrip("0123456789"))
    for b in tries:
        pair = f"{b}_USDT"
        gate_tf = _TF_MAP.get(tf, tf)
        key = (pair, gate_tf)
        now = time.monotonic()
        if key in _CACHE and now - _CACHE[key][0] < CACHE_TTL:
            result = _CACHE[key][1]
            if result:                  # cached hit: return even if it's a miss
                return result
        url = _GATE_URL.format(pair=pair, tf=gate_tf, n=n)
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read().decode())
            # Gate v4 row: [t, quote_vol, close, high, low, open, base_vol, closed]
            candles = [(int(c[0]), float(c[5]), float(c[3]), float(c[4]), float(c[2]))
                       for c in data]
            candles.sort(key=lambda c: c[0])
            _CACHE[key] = (now, candles)
            if candles:
                return candles
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, KeyError):
            _CACHE[key] = (now, [])     # cache the miss to avoid hammering the API
    return []


def _adx(highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
    """Wilder's Average Directional Index.  Needs at least period*2+1 bars."""
    if len(highs) < period * 2 + 1:
        return None

    tr_l, pdm_l, mdm_l = [], [], []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        tr_l.append(tr)
        pdm_l.append(up if up > dn and up > 0 else 0.0)
        mdm_l.append(dn if dn > up and dn > 0 else 0.0)

    # Wilder's smoothing: initial sum, then rolling (prev - prev/n + current)
    s_tr  = sum(tr_l[:period])
    s_pdm = sum(pdm_l[:period])
    s_mdm = sum(mdm_l[:period])

    dx_vals = []
    for i in range(period, len(tr_l)):
        s_tr  = s_tr  - s_tr  / period + tr_l[i]
        s_pdm = s_pdm - s_pdm / period + pdm_l[i]
        s_mdm = s_mdm - s_mdm / period + mdm_l[i]
        if s_tr == 0:
            dx_vals.append(0.0)
            continue
        pdi = s_pdm / s_tr * 100
        mdi = s_mdm / s_tr * 100
        di_sum = pdi + mdi
        dx_vals.append(abs(pdi - mdi) / di_sum * 100 if di_sum else 0.0)

    if not dx_vals:
        return None
    n = min(period, len(dx_vals))
    adx_val = sum(dx_vals[:n]) / n
    for dx in dx_vals[n:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    return adx_val


def _ema_last(closes: list, period: int) -> Optional[float]:
    """EMA seeded with SMA, returns the final value.  None if fewer than period bars."""
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def _regime_label(adx_val: float) -> str:
    """Bucket raw ADX into a human-readable regime name."""
    if adx_val <= 20:
        return "ranging_calm"
    elif adx_val <= 25:
        return "indecisive"
    elif adx_val <= 30:
        return "trending_moderate"
    else:
        return "trending_strong"


def get_adx_regime(symbol: str, tf: str = "1h",
                   period: int = 14) -> tuple:
    """
    ADX regime for `symbol` on `tf`.
    Returns (adx_value: float|None, regime_label: str|None).
    """
    candles = _fetch_ohlcv(symbol, tf, n=max(60, period * 3))
    if not candles:
        return None, None
    highs  = [c[2] for c in candles]
    lows   = [c[3] for c in candles]
    closes = [c[4] for c in candles]
    adx_val = _adx(highs, lows, closes, period)
    if adx_val is None:
        return None, None
    return round(adx_val, 1), _regime_label(adx_val)


def get_btc_htf_regime(tf: str = "4h", period: int = 14) -> tuple:
    """
    BTC macro regime on `tf` (4h for OracleAlgo context).
    Direction determined by EMA(20) vs EMA(50) on the fetched candles.
    Returns (direction: 'bull'|'bear'|None, adx: float|None, regime_label: str|None).
    """
    candles = _fetch_ohlcv("BTC-USDT", tf, n=max(100, period * 4))
    if not candles:
        return None, None, None
    highs  = [c[2] for c in candles]
    lows   = [c[3] for c in candles]
    closes = [c[4] for c in candles]
    adx_val = _adx(highs, lows, closes, period)
    if adx_val is None:
        return None, None, None
    ema20 = _ema_last(closes, 20)
    ema50 = _ema_last(closes, 50)
    if ema20 is None or ema50 is None:
        direction = None
    else:
        direction = "bull" if ema20 > ema50 else "bear"
    return direction, round(adx_val, 1), _regime_label(adx_val)
