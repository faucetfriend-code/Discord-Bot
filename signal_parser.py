"""
Three-stage signal parser with message classification.

Stage 1: regex fast-path for new entries and common update phrases.
Stage 2: local Qwen via LM Studio (fallback and classifier for ambiguous messages).
Stage 3: vision LLM — if a chart image is attached and entry/SL/TP still missing,
         ask the vision model to read price levels off the chart.

MessageType routing:
  NEW    → open a new position
  UPDATE → amend SL/TP on an existing position
  CLOSE  → close an existing position
  NONE   → not a trade message (ignore)
"""

import base64
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import requests as _http
from openai import OpenAI
from logger import log


class MessageType(Enum):
    """What a parsed message asks the bot to do: open (NEW), amend (UPDATE),
    exit (CLOSE), or nothing (NONE)."""
    NEW = "new"
    UPDATE = "update"
    CLOSE = "close"
    NONE = "none"


_llm_client: Optional[OpenAI] = None


def _get_llm() -> OpenAI:
    """Lazily build the OpenAI-compatible client pointed at the local LM Studio server."""
    global _llm_client
    if _llm_client is None:
        base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
        _llm_client = OpenAI(base_url=base_url, api_key="local")
    return _llm_client


@dataclass
class Signal:
    """The structured result of parsing one message. `source` distinguishes an
    analyst follow-trade ("") from the RSI/OracleAlgo strategies; the vision_*
    and signal_tf fields carry extra context those strategies need downstream."""
    message_type: MessageType
    symbol: str
    side: str           # "buy" or "sell" — may be empty string for UPDATE/CLOSE
    analyst: str
    raw_text: str
    entry: Optional[float] = None    # new signals only; None = market order (CMP)
    sl: Optional[float] = None       # new signal SL
    tp: Optional[float] = None       # new signal TP (final/validation target)
    tps: Optional[list] = None       # analyst-stated multi-TP ladder from text (nearest-first)
    new_sl: Optional[float] = None   # update: new stop loss value
    new_tp: Optional[float] = None   # update: new take profit value
    new_avg_entry: Optional[float] = None  # update: DCA/averaging shifted the entry basis
    is_market_order: bool = False    # True when analyst says "at CMP" / no limit price
    source: str = ""                 # "" = analyst follow-trade; "rsi_extreme"; "oraclealgo"
    vision_tps: Optional[list] = None    # multiple TP targets read off a chart, if any
    vision_levels: Optional[list] = None # ALL price levels read off a chart (roles assigned by geometry)
    signal_tf: str = ""              # OracleAlgo signal timeframe: "4h" (bias) or "1h" (entry)
    rsi_value: Optional[float] = None      # RSI Extreme: the RSI reading from the alert (e.g. 90.3)
    change_24h: Optional[float] = None     # RSI Extreme: 24h % change from the alert (e.g. +14.84)
    oracle_signal_type: str = ""           # OracleAlgo: alert type token (MSS / BOS / Supertrend / Delta)
    soft_stop: bool = False                # SL only triggers on a candle CLOSE beyond it, not a wick
    alert_price: Optional[float] = None    # price quoted IN the alert text (RSI "Price$X") — survives
                                           # even when the symbol isn't listed on BloFin (shadow/backtest)


# ---------------------------------------------------------------------------
# Regex — new entry patterns (ordered most-to-least specific)
# ---------------------------------------------------------------------------
_NEW_PATTERNS = [
    # Unity embed format: "BTC/USDT — LONG ... Entry $71,010 ... Stop Loss $68,000"
    # Most specific — tried first. Requires /USDT and dollar signs (embed always has them).
    # TP label numbers ("Take Profit 1 $72,000") are skipped via (?:[1-9]\s*)? before $.
    r'(?P<sym>[A-Z]{2,10})/USDT\s*[—–\-]+\s*(?P<side>long|short|buy|sell)'
    r'.*?(?:entr(?:y|:)?)[\s:]*\$(?P<entry>[\d,.]+)'
    r'.*?(?:s\.?l\.?|stop\s+loss)[\s:]*\$(?P<sl>[\d,.]+)'
    r'(?:.*?(?:t\.?p\.?|take\s*profit)\s*(?:[1-9]\s*)?\$(?P<tp>[\d,.]+))?',

    # "LONG BTC | Entry: 45000 | SL: 44000 | TP: 47000"
    r'(?P<side>long|short|buy|sell)\s+(?P<sym>[A-Z]{2,10})(?:[/-]USDT?)?\b'
    r'.*?(?:entr(?:y|:)|cmp/?)[\s:]*\$?(?P<entry>[\d,.]+)'
    r'.*?(?:s\.?l\.?|stop\s+loss)[\s:]*\$?(?P<sl>[\d,.]+)'
    r'.*?(?:t\.?p\.?|take\s*profit)\s*(?:[1-9](?=[\s:$]))?\s*[\s:]*\$?(?P<tp>[\d,.]+)',

    # "BUY BTC-USDT @ 45000, SL 44000, TP 47000"
    r'(?P<side>buy|sell)\s+(?P<sym>[A-Z]{2,10})(?:[/-]USDT?)?\s*@\s*\$?(?P<entry>[\d,.]+)'
    r'.*?(?:s\.?l\.?|stop\s+loss)[\s:]*\$?(?P<sl>[\d,.]+)'
    r'.*?(?:t\.?p\.?|take\s*profit)\s*(?:[1-9](?=[\s:$]))?\s*[\s:]*\$?(?P<tp>[\d,.]+)',

    # "⚡ ASTER/USDT LONG  Entry 0.67  SL 0.63  TP 0.70"
    r'(?P<sym>[A-Z]{2,10})(?:[/-]USDT?)?\s+(?P<side>long|short|buy|sell)'
    r'.*?(?:entr(?:y|:)|cmp/?)[\s:]*\$?(?P<entry>[\d,.]+)'
    r'.*?(?:s\.?l\.?|stop\s+loss)[\s:]*\$?(?P<sl>[\d,.]+)'
    r'.*?(?:t\.?p\.?|take\s*profit)\s*(?:[1-9](?=[\s:$]))?\s*[\s:]*\$?(?P<tp>[\d,.]+)',

    # "VVV/USDT — SHORT ... Entry $17.33 ... Stop Loss $18.65" (non-BTC embed, optional TP)
    r'(?P<sym>[A-Z]{2,10})(?:[/-]USDT?)?\s*[—–\-]+\s*(?P<side>long|short|buy|sell)'
    r'.*?(?:entr(?:y|:)?)[\s:]*\$?(?P<entry>[\d,.]+)'
    r'.*?(?:s\.?l\.?|stop\s+loss)[\s:]*\$?(?P<sl>[\d,.]+)'
    r'(?:.*?(?:t\.?p\.?|take\s*profit)\s*(?:[1-9](?=[\s:$]))?\s*[\s:]*\$?(?P<tp>[\d,.]+))?',

    # "CHZ LONG CMP/ 0.3622 SL: 0.03514" — no TP, no dollar signs
    r'(?P<sym>[A-Z]{2,10})(?:[/-]USDT?)?\s+(?P<side>long|short|buy|sell)'
    r'.*?(?:entr(?:y|:)|cmp/?)[\s:]*\$?(?P<entry>[\d,.]+)'
    r'.*?(?:s\.?l\.?|stop\s+loss)[\s:]*\$?(?P<sl>[\d,.]+)'
    r'(?:.*?(?:t\.?p\.?|take\s*profit)\s*(?:[1-9](?=[\s:$]))?\s*[\s:]*\$?(?P<tp>[\d,.]+))?',
]

# ---------------------------------------------------------------------------
# Regex — update fast-path keywords
# ---------------------------------------------------------------------------
# These phrases strongly indicate a position update, not a new entry.
_UPDATE_KEYWORDS = re.compile(
    r'moving\s+s\.?l|move\s+s\.?l|moved\s+s\.?l'
    r'|s\.?l\.?\s+to\s+(?:break\s*even|b/?e|\d)'
    r'|stop\s+to\s+(?:break\s*even|b/?e|\d)'
    r'|break\s*even|breakeven'
    r'|t\.?p\.?\s*[12345]\s+hit|take\s+profit\s+hit'
    r'|trailing\s+stop'
    r'|partial\s+(?:close|profit|tp)'
    r'|stop\s+loss\s+updated|targets?\s+updated'
    r'|hard\s+s\.?l\b|soft\s+s\.?l\b'
    r'|full\s+tp\s+at|setting\s+(?:full\s+)?tp',
    re.IGNORECASE,
)

_CLOSE_KEYWORDS = re.compile(
    r'\b(?:close|closed|closing|exit|exiting|exited|out\s+of)\b'
    r'(?!\s+(?:long|short|buy|sell))',  # not "close long" (that's a new entry direction)
    re.IGNORECASE,
)

# DCA / averaging analysts (e.g. Ajmal) post position-management updates:
#   "New average entry: $1.9895"  → shifts the entry basis (PnL/SL/TP measured from here)
#   "DCA1 filled at $1.914"        → a DCA add (we track the basis, not the size)
#   "Limit Entry Reached", "Limit order should be filling", "New Entry Added"
#                                  → pure info — acknowledge as a no-op, never "incomplete"
_AVG_ENTRY = re.compile(
    r'(?:new\s+)?(?:avg|average)\s+entry[:\s]*(?:is\s*)?\$?([\d,.]+)', re.IGNORECASE)
_DCA_FILL = re.compile(r'\bdca\w*\s+filled\s+at\s*\$?([\d,.]+)', re.IGNORECASE)
_POSITION_INFO = re.compile(
    r'limit\s+entry\s+reached|limit\s+order\s+should\s+be\s+filling|'
    r'entry\s+(?:reached|filled)|new\s+entry\s+added|\bdca\w*\s+filled',
    re.IGNORECASE,
)

# Extract a number after SL/stop keywords in an update message.
# Arrow variants ("SL: $old -> $new") are kept separate so _try_update_regex
# can prefer them — the plain extractor would capture the OLD value.
_UPDATE_SL_EXTRACT = re.compile(
    r'(?:hard\s+|soft\s+)?(?:s\.?l\.?|stop(?:\s+loss)?)(?:\s+at|\s+to|\s*=|:)?\s*\$?([\d,.]+)',
    re.IGNORECASE,
)
_UPDATE_TP_EXTRACT = re.compile(
    r'(?:full\s+)?(?:t\.?p\.?\b|take\s*profit)(?:\s+at|\s+to|\s*=|:)?\s*\$?([\d,.]+)',
    re.IGNORECASE,
)
# Discord embed update cards write "SL: $old -> $new"; capture the NEW value.
_SL_ARROW = re.compile(
    r'(?:s\.?l\.?|stop(?:\s+loss)?)\s*:\s*\$?[\d,.]+\s*[-=>→]+\s*\$?([\d,.]+)',
    re.IGNORECASE,
)
_TP_ARROW = re.compile(
    r'(?:t\.?p\.?\b|take\s*profit)\s*:\s*\$?[\d,.]+\s*[-=>→]+\s*\$?([\d,.]+)',
    re.IGNORECASE,
)
_SYMBOL_EXTRACT = re.compile(
    r'\b([A-Z]{2,10})(?:[/-]USDT?)?\b',
)
# Common English/trading words that must NOT be treated as ticker symbols.
_SYMBOL_BLOCKLIST = frozenset({
    'STOP', 'LOSS', 'TRADE', 'LONG', 'SHORT', 'TAKE', 'ENTRY', 'SIGNAL',
    'UNITY', 'ALERT', 'ALERTS', 'NEW', 'OLD', 'MAX', 'MIN', 'RSI', 'EMA',
    'SMA', 'THE', 'BOS', 'BREAK', 'ADDED', 'MOVED', 'UPDATED', 'CLOSED',
    'OPEN', 'FROM', 'INTO', 'OVER', 'UNDER', 'BEEN', 'WITH', 'YOUR', 'THIS',
    'THAT', 'HAVE', 'WILL', 'CALL', 'FULL', 'MARK', 'BULL', 'BEAR', 'SELL',
    'BUYS', 'HITS', 'BACK', 'NEXT', 'LAST', 'MOVE', 'DOWN', 'PUSH', 'PAIR',
    'SIZE', 'RISK', 'BIAS', 'JUST', 'ONLY', 'SOME', 'ALSO', 'VERY', 'HERE',
    'TOOK', 'VERY', 'GOLD', 'FORE', 'INFO', 'DATA', 'NOTE', 'IDEA', 'GOOD',
    # words seen in DCA / limit-fill / update messages
    'LIMIT', 'REACHED', 'PRICE', 'CURRENT', 'FILLED', 'FILLING', 'ORDER',
    'AVERAGE', 'AVG', 'DCA', 'CMP', 'REQUIRED', 'LATER', 'PROFIT', 'PROFITS',
    'NOTES', 'HARD', 'LEVERAGE', 'TAKEN', 'ENTRIES', 'BEING', 'WHITE',
    # freeform-note words (e.g. "Market long BLESS … 4H close under X for stops")
    'MARKET', 'CLOSE', 'EXIT', 'STOPS', 'CHART', 'PLAY', 'WEEKEND',
    # quote currencies — never the traded symbol itself (e.g. "H/USDT" -> "USDT")
    'USDT', 'USDC', 'USD',
})

# Detects "at CMP", "market long/short", "longing X at CMP" — no limit entry price.
_CMP_PATTERN = re.compile(
    r'\bat\s+cmp\b'
    r'|market\s+(?:long|short|buy|sell|order)'
    r'|(?:long|short)ing\b[^.]*\bat\s+cmp\b',
    re.IGNORECASE,
)

# "4H close under 0.0939", "close below 0.09" — stop loss phrasing without "SL:" keyword.
_CLOSE_UNDER_SL = re.compile(
    r'close[sd]?\s+(?:under|below)\s*([\d,.]+)',
    re.IGNORECASE,
)

# Entry-less directional calls where the analyst entered at market and the
# numeric levels live only on an attached chart, e.g.:
#   "Took this HYPE short, .5R size ... TP's in gold"
#   "shorted SOL here", "longing ME at CMP"
_ENTRY_VERB = re.compile(
    r'\b(?:took|taken|tak(?:e|ing)|shorted|longed|shorting|longing|entered|'
    r'grabbed|sniped|scal(?:e|ed|ing)\s+in)\b',
    re.IGNORECASE,
)
# "HYPE short" / "HYPE long"  (ticker before direction)
_TICKER_DIR = re.compile(r'\b([A-Za-z]{2,10})\s+(short|long)\b', re.IGNORECASE)
# "shorted HYPE" / "longing this ME"  (direction before ticker)
_DIR_TICKER = re.compile(
    r'\b(short|long)(?:ed|ing)?\s+(?:this\s+|the\s+)?([A-Za-z]{2,10})\b',
    re.IGNORECASE,
)

# OracleAlgo "RSI Extreme" alerts, e.g.:
#   "RSI Extreme — OverboughtSIGN/USDTRSI90.3Price$0.0135 24h Change+14.84%"
# Overbought → mean-reversion SHORT; Oversold → mean-reversion LONG.
_RSI_EXTREME = re.compile(
    r'RSI\s*Extreme\s*[—–\-]+\s*'
    r'(?P<dir>Overbought|Oversold)\s*'
    r'(?P<sym>[A-Z0-9]{2,12})/USDT',
    re.IGNORECASE,
)
# The RSI reading and 24h change ride along in the same alert. Captured
# separately (the symbol/RSI/price text runs together with no delimiters) so the
# strategy can filter on extremity + momentum instead of discarding the numbers.
_RSI_VALUE = re.compile(r'RSI\s*(?P<rsi>[0-9]{1,3}(?:\.[0-9]+)?)', re.IGNORECASE)
_RSI_CHANGE = re.compile(r'24h\s*Change\s*(?P<chg>[+\-]?[0-9.]+)\s*%', re.IGNORECASE)
# The alert's quoted price ("Price$0.3697"). In the run-together card text the
# price digits butt straight against "24h" ("Price$0.369724h Change"), so require
# the number to NOT be followed by another digit or letter — the deduplicated
# repeats later in the card ("Price $0.3697 ") then match cleanly.
_RSI_PRICE = re.compile(r'Price\s*\$\s*(?P<px>[0-9,]+(?:\.[0-9]+)?)(?![0-9A-Za-z.,])')

# OracleAlgo BTC structure/momentum alerts (MSS / BOS / Supertrend / Delta), e.g.:
#   "Bearish 1H MSS  BTCUSDT 1H bearish market structure shift confirmed
#    LTF Bias: Shorts > Longs ... Timeframe 1H  Price 73602.17  Signal Bearish MSS"
# Direction from the "Signal" field (or LTF Bias); timeframe = bias (4H) vs entry (1H).
_ORACLE_HINT = re.compile(r'LTF\s*Bias', re.IGNORECASE)
_ORACLE_DIR_SIGNAL = re.compile(r'Signal\s*(Bullish|Bearish)', re.IGNORECASE)
# The alert *type* token after the direction, e.g. "Signal Bullish MSS" → "MSS".
# Used for the confluence filter (require N distinct aligned types before entry).
_ORACLE_TYPE = re.compile(r'Signal\s+(?:Bullish|Bearish)\s+([A-Za-z]+)', re.IGNORECASE)
_ORACLE_DIR_BIAS = re.compile(
    r'LTF\s*Bias[:\s]*(Longs|Shorts)\s*>\s*(Longs|Shorts)', re.IGNORECASE)
_ORACLE_TICKER = re.compile(r'Ticker[:\s]*([A-Z]{2,10})USDT', re.IGNORECASE)
_ORACLE_TF = re.compile(r'Timeframe[:\s]*([0-9]{1,3}H?)', re.IGNORECASE)

# Pre-filter: if NONE of these appear in a message it cannot be a trade signal.
# Avoids sending pure chatter / memes / announcements through the LLM.
_TRADE_HINT = re.compile(
    r'\b(?:long(?:ed|ing)?|short(?:ed|ing)?|buy|sell|entry|cmp|sl|tp|stop|target|'
    r'lev(?:erage)?|usdt|futures|perp|trade|signal|alert|close|exit|scalp|swing|'
    r'position|took|grabbed|sniped|bullish|bearish|bias)\b',
    re.IGNORECASE,
)


# POI / "future area of interest" detection — an analyst flags a level to watch
# but hasn't entered yet. Future-intent phrasing (not "I took the long").
_POI_HINT = re.compile(
    r'point[s]?\s+of\s+interest|\bpoi[s]?\b|area[s]?\s+of\s+interest|'
    r'looking\s+to\s+(?:enter|long|short|buy|sell|get\s+in|very\s+soon|take)|'
    r'haven.?t\s+(?:entered|taken)|\beyeing\b|keep\s+(?:a\s+)?(?:close\s+)?eye|'
    r'htf\s+zone|watching\s+(?:for|this|these|it)|close\s+eye',
    re.IGNORECASE,
)
_POI_BULL = re.compile(
    r'\b(?:bounce|long|buy|support|reclaim|accumulat|demand|dip|bullish|bid)\b', re.IGNORECASE)
_POI_BEAR = re.compile(
    r'\b(?:short|reject|resistance|sell|supply|breakdown|bearish)\b', re.IGNORECASE)


def detect_poi(msg: dict) -> Optional[dict]:
    """
    Detect a 'future area of interest' / POI watch message (analyst is eyeing a
    level but hasn't entered). Returns {symbol, direction, note} or None.

    Direction is a lean from bull/bear vocabulary ('' if ambiguous). The trigger
    LEVEL is resolved separately in bot.py (vision off the attached chart, since
    these zones usually live on a chart, not in the text). Called only when the
    message isn't already a tradeable signal, so real entries take precedence.
    """
    text = msg.get("content", "")
    if not _POI_HINT.search(text):
        return None
    # Skip if it reads like an entry already taken (that's a trade, not a watch).
    if _ENTRY_VERB.search(text) and (_TICKER_DIR.search(text) or _DIR_TICKER.search(text)):
        return None
    symbol = _extract_symbol(text)
    if symbol == "UNKNOWN-USDT":
        return None
    bull, bear = bool(_POI_BULL.search(text)), bool(_POI_BEAR.search(text))
    direction = "buy" if (bull and not bear) else "sell" if (bear and not bull) else ""
    return {"symbol": symbol, "direction": direction, "note": text[:200]}


def _normalise_symbol(raw: str) -> str:
    """Normalise a ticker to BloFin's `BASE-USDT` form (e.g. 'btc/usdt' → 'BTC-USDT').
    Returns 'UNKNOWN-USDT' if nothing's left after stripping the quote (e.g. 'USDT')."""
    raw = raw.upper().replace("/", "-").replace("USDT", "").rstrip("-")
    return f"{raw}-USDT" if raw else "UNKNOWN-USDT"


def _to_float(s: str) -> float:
    """Parse a price string, tolerating leading '$' and thousands commas."""
    return float(str(s).lstrip("$").replace(",", ""))


# A "soft" stop is one the analyst only honours on a candle CLOSE beyond the level
# (e.g. Sveezy's "Stop Loss $X … Soft (1h close close)"), not an intrabar wick.
# Detected from the word "soft" or an explicit candle-close timeframe phrasing.
_SOFT_STOP = re.compile(
    r'\bsoft\b|(?:\d+\s*[hmdHMD]|candle|hourly|daily|weekly)\s+close', re.IGNORECASE)

# Numbered take-profit ladder in the message text, e.g.
#   "Take ProfitsTP1: $55.00 (+11.11%) TP2: $65.00 TP3: $75.00 TP4: $100.00"
# The single-TP groups in _NEW_PATTERNS only catch the first target (and miss the
# "TPn: $price" colon form entirely), so this collects the whole ladder separately.
_TP_LADDER = re.compile(
    r'(?:tp|take\s*profit)\s*([1-9])\b[^$\d]*\$?\s*([\d][\d,]*(?:\.\d+)?)',
    re.IGNORECASE)


def _enrich_new(sig: Optional[Signal], text: str) -> Optional[Signal]:
    """Post-process a freshly built NEW signal:
      • mark it a soft (candle-close) stop when the analyst said so, so the
        resolver waits for a 1h close beyond SL instead of stopping on a wick;
      • for a multi-entry scale-in ("Entry 1 … Entry 2 … Avg Entry $Z"), use the
        analyst's stated AVERAGE entry as the position basis, so SL/TP/PnL match
        the plan the analyst is actually trading."""
    if sig is None:
        return sig
    if _SOFT_STOP.search(text):
        sig.soft_stop = True
    if _ENTRY_LADDER.search(text):
        avg = _AVG_ENTRY.search(text)
        if avg:
            try:
                sig.entry = _to_float(avg.group(1))
            except (TypeError, ValueError):
                pass
    # Capture a numbered TP ladder ("TP1 … TP2 … TP3 …") so stated targets aren't
    # lost — the regex patterns only grab the first TP. Kept in listed order (the
    # analyst always lists the nearest target first).
    ladder, seen = [], set()
    for m in _TP_LADDER.finditer(text):
        try:
            v = _to_float(m.group(2))
        except (TypeError, ValueError):
            continue
        if v not in seen:
            seen.add(v)
            ladder.append(v)
    if len(ladder) >= 2:
        sig.tps = ladder
    # Backfill a single TP too: the "TPn: $price" colon form defeats the single-TP
    # groups in _NEW_PATTERNS, so without this a lone "TP1: $64000" would auto-TP.
    if sig.tp is None and ladder:
        sig.tp = ladder[0]
    return sig


def _build_new_signal(m: re.Match, msg: dict) -> Optional[Signal]:
    """Turn a matched entry-pattern regex into a NEW Signal (or None if it doesn't parse)."""
    try:
        gd = m.groupdict()
        side_raw = gd["side"].lower()
        side = "buy" if side_raw in ("buy", "long") else "sell"
        # The pattern can grab a leading prose word as the "ticker" (e.g.
        # "Close long SOL …" → "CLOSE"). If so, fall back to a blocklist-aware
        # scan of the whole message for the real all-caps symbol.
        sym_raw = gd["sym"]
        if sym_raw.upper() in _SYMBOL_BLOCKLIST:
            symbol = _extract_symbol(msg.get("content", ""))
        else:
            symbol = _normalise_symbol(sym_raw)
        entry = _to_float(gd["entry"])
        sl = _to_float(gd["sl"])
        # tp group is optional in the 4th pattern — may be None
        tp_raw = gd.get("tp")
        tp = _to_float(tp_raw) if tp_raw is not None else None
        sig = Signal(
            message_type=MessageType.NEW,
            symbol=symbol, side=side, entry=entry, sl=sl, tp=tp,
            analyst=msg.get("author", ""), raw_text=msg.get("content", ""),
        )
        return _enrich_new(sig, msg.get("content", ""))
    except Exception as e:
        log.debug(f"Could not build new signal from regex match: {e}")
        return None


_SYM_USDT = re.compile(r'\b([A-Z0-9]{2,10})[/-]USDT?\b')  # case-sensitive: real tickers are upper


def _extract_symbol(text: str) -> str:
    """
    Find the ticker symbol in a message. Prefers an explicit `SYMBOL/USDT` pairing
    (how update/info messages name the coin, e.g. "TON/USDT LONG") — matched
    case-sensitively so prose words aren't mistaken for tickers. Falls back to the
    first uppercase-ish token that isn't a common English/trading word.
    """
    m = _SYM_USDT.search(text)
    if m and m.group(1) not in _SYMBOL_BLOCKLIST:
        return _normalise_symbol(m.group(1))
    # Fallback: the first bare ALL-CAPS token. Matched case-sensitively against the
    # ORIGINAL text (not .upper()) because tickers are always posted in all-caps to
    # set them apart — so mixed-case prose and names ("Sveezy", "Caleb", "Entries")
    # can never be mistaken for a symbol. The blocklist still filters all-caps words.
    for m in _SYMBOL_EXTRACT.finditer(text):
        candidate = m.group(1)
        if candidate not in _SYMBOL_BLOCKLIST:
            return _normalise_symbol(candidate)
    return "UNKNOWN-USDT"


def _try_directional_market(text: str, msg: dict) -> Optional[Signal]:
    """
    Catch entry-less directional calls ("Took this HYPE short") where the analyst
    entered at market and the numeric levels are only on an attached chart.
    Returns a market-order NEW signal with symbol+side; entry is fetched live and
    SL/TP are filled by the vision model (or left missing → skipped) downstream.
    Requires an entry verb AND an uppercase ticker, to avoid matching prose.
    """
    if not _ENTRY_VERB.search(text):
        return None

    sym = side = None
    m = _TICKER_DIR.search(text)
    if m and m.group(1).isupper() and m.group(1) not in _SYMBOL_BLOCKLIST:
        sym = m.group(1)
        side = "sell" if m.group(2).lower().startswith("short") else "buy"
    if sym is None:
        m = _DIR_TICKER.search(text)
        if m and m.group(2).isupper() and m.group(2) not in _SYMBOL_BLOCKLIST:
            sym = m.group(2)
            side = "sell" if m.group(1).lower().startswith("short") else "buy"
    if sym is None:
        return None

    return Signal(
        message_type=MessageType.NEW,
        symbol=_normalise_symbol(sym),
        side=side,
        analyst=msg.get("author", ""),
        raw_text=text,
        entry=None,                 # entered at market — entry fetched live
        is_market_order=True,
    )


def _oracle_timeframe(raw: str) -> str:
    """Map an OracleAlgo timeframe token ("1H", "4H", "240") to "1h" / "4h"."""
    v = raw.upper().strip()
    try:
        if v.endswith("H"):
            hours = int(v[:-1])
        else:
            mins = int(v)
            hours = mins // 60 if mins >= 60 else mins
    except ValueError:
        return "1h"
    return "4h" if hours >= 4 else "1h"


def _try_oraclealgo(text: str, msg: dict) -> Optional[Signal]:
    """
    Detect an OracleAlgo BTC structure/momentum alert. Returns a market-order
    NEW signal tagged source="oraclealgo" with signal_tf "4h" (bias) or "1h"
    (entry). The 4H/1H state machine + SL/TP are applied in bot.py.
    """
    if not _ORACLE_HINT.search(text):
        return None

    side = None
    m = _ORACLE_DIR_SIGNAL.search(text)
    if m:
        side = "buy" if m.group(1).lower() == "bullish" else "sell"
    if side is None:
        m = _ORACLE_DIR_BIAS.search(text)
        if m:  # "Longs > Shorts" = bullish; "Shorts > Longs" = bearish
            side = "buy" if m.group(1).lower() == "longs" else "sell"
    if side is None:
        return None

    sym = "BTC-USDT"
    mt = _ORACLE_TICKER.search(text)
    if mt:
        sym = _normalise_symbol(mt.group(1))

    tf = "1h"
    mtf = _ORACLE_TF.search(text)
    if mtf:
        tf = _oracle_timeframe(mtf.group(1))

    sig_type = ""
    mtype = _ORACLE_TYPE.search(text)
    if mtype:
        sig_type = mtype.group(1).upper()

    return Signal(
        message_type=MessageType.NEW,
        symbol=sym,
        side=side,
        analyst=msg.get("author", ""),
        raw_text=text,
        entry=None,                 # market entry — live price
        is_market_order=True,
        source="oraclealgo",
        signal_tf=tf,
        oracle_signal_type=sig_type,
    )


def _try_rsi_extreme(text: str, msg: dict) -> Optional[Signal]:
    """
    Detect an OracleAlgo "RSI Extreme" alert and build a mean-reversion market order:
      Oversold   → LONG  (expect bounce up)
      Overbought → SHORT (expect pullback down)
    Entry/SL/TP are resolved at execution time in bot.py from the live price.
    """
    m = _RSI_EXTREME.search(text)
    if not m:
        return None
    direction = m.group("dir").lower()
    side = "buy" if direction == "oversold" else "sell"
    symbol = _normalise_symbol(m.group("sym"))

    # Pull the RSI reading + 24h change so the strategy can filter on extremity
    # and momentum (e.g. skip a "fade" against a coin already trending hard).
    rsi_m = _RSI_VALUE.search(text)
    chg_m = _RSI_CHANGE.search(text)
    px_m = _RSI_PRICE.search(text)
    rsi_value = None
    change_24h = None
    alert_price = None
    try:
        if rsi_m:
            rsi_value = float(rsi_m.group("rsi"))
        if chg_m:
            change_24h = float(chg_m.group("chg"))
        if px_m:
            alert_price = float(px_m.group("px").replace(",", ""))
    except (TypeError, ValueError):
        pass

    return Signal(
        message_type=MessageType.NEW,
        symbol=symbol,
        side=side,
        analyst=msg.get("author", ""),
        raw_text=text,
        entry=None,                 # market order — entry fetched live
        is_market_order=True,
        source="rsi_extreme",
        rsi_value=rsi_value,
        change_24h=change_24h,
        alert_price=alert_price,
    )


def _try_update_regex(text: str, msg: dict) -> Optional[Signal]:
    """Fast-path: detect common update phrases and extract new SL/TP if present."""
    if not _UPDATE_KEYWORDS.search(text):
        return None

    symbol = _extract_symbol(text)
    if symbol == "UNKNOWN-USDT":
        return None  # no symbol visible; let the LLM try

    # Arrow format ("SL: $old -> $new") takes priority — the plain extractor
    # would grab the OLD value that sits before the arrow.
    new_sl_m = _SL_ARROW.search(text) or _UPDATE_SL_EXTRACT.search(text)
    new_tp_m = _TP_ARROW.search(text) or _UPDATE_TP_EXTRACT.search(text)
    new_sl = _to_float(new_sl_m.group(1)) if new_sl_m else None
    new_tp = _to_float(new_tp_m.group(1)) if new_tp_m else None

    return Signal(
        message_type=MessageType.UPDATE,
        symbol=symbol, side="", new_sl=new_sl, new_tp=new_tp,
        analyst=msg.get("author", ""), raw_text=text,
    )


# A fresh multi-entry / DCA NEW signal (e.g. Sveezy) restates an "Avg Entry" as
# part of its INITIAL setup — "Entry 1: $X (50%)  Entry 2: $Y (50%)  Avg Entry $Z".
# That must not be mistaken for an averaging UPDATE on an existing position. A real
# new setup carries the full anatomy: a direction + an entry price + a stop loss, OR
# an explicit numbered-entry ladder. (A genuine averaging note — "new average entry
# $X" — has none of that ladder/SL scaffolding.)
_DIR_WORD = re.compile(r'\b(?:long|short|buy|sell)\b', re.IGNORECASE)
_HAS_STOP = re.compile(r'stop\s*loss|\bs\.?l\.?\b', re.IGNORECASE)
_ENTRY_PRICE = re.compile(r'entr\w*[\s:$]*[\d.,]+|\$[\d.,]+\s*\(limit\)', re.IGNORECASE)
_ENTRY_LADDER = re.compile(r'entry\s*[12]\s*[:\-]|\bentries\b', re.IGNORECASE)


def _looks_like_new_setup(text: str) -> bool:
    """True when the message is a fresh entry setup (so the averaging-update
    detector should defer to the NEW parser instead of hijacking it)."""
    if _ENTRY_LADDER.search(text):
        return True
    return bool(_DIR_WORD.search(text) and _ENTRY_PRICE.search(text) and _HAS_STOP.search(text))


def _try_position_update(text: str, msg: dict) -> Optional[Signal]:
    """
    Position-management updates from DCA/averaging analysts:
      • "New average entry: $X" → UPDATE that shifts the entry basis (new_avg_entry).
      • "Limit Entry Reached" / "DCA filled" / "New Entry Added" (no avg given)
        → recognised INFO; returned as an UPDATE with no numeric changes so it's
        acknowledged cleanly instead of failing as an incomplete NEW signal.
    Returns None if the text isn't one of these management messages, OR if it's
    actually a fresh entry setup that merely quotes an avg entry (let NEW handle it).
    """
    avg = _AVG_ENTRY.search(text)
    info = _POSITION_INFO.search(text)
    if not avg and not info:
        return None
    # A new multi-entry setup quotes "Avg Entry" too — don't classify it as an
    # averaging UPDATE. Defer to the NEW entry parser / LLM.
    if _looks_like_new_setup(text):
        return None
    return Signal(
        message_type=MessageType.UPDATE,
        symbol=_extract_symbol(text), side="",
        analyst=msg.get("author", ""), raw_text=text,
        new_avg_entry=_to_float(avg.group(1)) if avg else None,
    )


def _try_close_regex(text: str, msg: dict) -> Optional[Signal]:
    """Fast-path: detect explicit close/exit phrases."""
    # "4H close under X" / "close below X" is stop-loss phrasing, NOT a close
    # instruction — strip those occurrences before testing for a genuine close
    # keyword (otherwise a new setup's soft-stop note hijacks the close path).
    probe = _CLOSE_UNDER_SL.sub(" ", text)
    if not _CLOSE_KEYWORDS.search(probe):
        return None
    # A new-entry directional call ("Market long BLESS", "shorting ETH") is an
    # OPEN, not a close — even when its stop note mentions a candle "close".
    if re.search(r'\bmarket\s+(?:long|short|buy|sell)\b|\b(?:long|short)ing\b',
                 text, re.IGNORECASE):
        return None
    # Only treat as CLOSE if there's no entry price (otherwise it's a new signal direction)
    if re.search(r'entr(?:y|:)[\s:]*[\d,.]+', text, re.IGNORECASE):
        return None

    symbol = _extract_symbol(text)

    return Signal(
        message_type=MessageType.CLOSE,
        symbol=symbol, side="",
        analyst=msg.get("author", ""), raw_text=text,
    )


def _llm_parse(text: str, msg: dict) -> Optional[Signal]:
    """Ask local Qwen to classify the message and extract all signal fields."""
    model = os.getenv("LOCAL_LLM_MODEL", "qwen3.5-9b")
    prompt = (
        "You are a trading signal classifier. Analyse the message below and return ONLY "
        "a raw JSON object (no markdown, no explanation) with these exact keys:\n"
        '  "type": "new" | "update" | "close" | "none"\n'
        '    "new"    = opening a new position\n'
        '    "update" = modifying SL/TP on an existing position '
        '(break even, trailing stop, partial TP hit, SL move)\n'
        '    "close"  = fully exiting/closing a position\n'
        '    "none"   = not a trade message\n'
        '  "symbol": string e.g. "BTC-USDT"  (null if absent)\n'
        '  "side":   "buy" | "sell"           (null if absent)\n'
        '  "entry":  number or null — use null when analyst says "at CMP", "at market",\n'
        '            or gives no specific entry price (market order)\n'
        '  "sl":     number — also extract from "close under X", "close below X",\n'
        '            "4H close under X for stops" phrasing  (null if absent)\n'
        '  "tp":     number or null — use null when TP is described as "above in white",\n'
        '            "on chart", or no numeric TP is given\n'
        '  "new_sl": number                   (null if absent, update new SL)\n'
        '  "new_tp": number                   (null if absent, update new TP)\n\n'
        f"Message:\n{text[:800]}"
    )
    try:
        client = _get_llm()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.0,
        )
        raw = response.choices[0].message.content.strip()
        # Strip Qwen3 thinking blocks — <think>...</think> — before parsing JSON.
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
        data = json.loads(raw)

        msg_type_str = str(data.get("type", "none")).lower()
        try:
            msg_type = MessageType(msg_type_str)
        except ValueError:
            msg_type = MessageType.NONE

        if msg_type == MessageType.NONE:
            return None

        def _safe_float(key):
            v = data.get(key)
            return float(v) if v is not None else None

        symbol_raw = data.get("symbol") or "UNKNOWN"
        symbol = _normalise_symbol(str(symbol_raw).replace("-USDT", "").replace("/USDT", ""))
        side = str(data.get("side") or "").lower()

        sig = Signal(
            message_type=msg_type,
            symbol=symbol,
            side=side,
            entry=_safe_float("entry"),
            sl=_safe_float("sl"),
            tp=_safe_float("tp"),
            new_sl=_safe_float("new_sl"),
            new_tp=_safe_float("new_tp"),
            analyst=msg.get("author", ""),
            raw_text=text,
        )
        return _enrich_new(sig, text) if msg_type == MessageType.NEW else sig
    except Exception as e:
        log.debug(f"LLM parse failed: {e}")
        return None


_VISION_PROMPTS = {
    # Trade setup: the colored entry/SL/TP price tags on the right axis.
    "trade": (
        "This is a TradingView trade-setup chart. Read the EXACT price "
        "numbers from the COLORED PRICE TAGS on the right-hand price axis "
        "(not the grey axis gridline numbers, and not the candles).\n"
        "The tags are colour-coded:\n"
        "- A RED tag is the STOP LOSS (sl).\n"
        "- The highlighted current-price tag (often boxed and showing a "
        "time like 12:33:10) is the ENTRY / CMP (entry).\n"
        "- ORANGE/yellow tags are TAKE-PROFIT targets (tps). There are "
        "usually several — list ALL of them.\n"
        "Read each number digit-for-digit from its tag (e.g. 71.594, "
        "67.761, 64.931, 59.412, 45.773).\n"
        "Output ONLY this JSON, numbers only, no labels:\n"
        '{"entry": 0.0, "sl": 0.0, "tps": [0.0, 0.0, 0.0]}\n'
        "Use null for entry/sl if you cannot read them, and [] for tps "
        "if there are none."
    ),
    # POI: the marked level/zone the trader is watching (no entry/SL/TP needed).
    "poi": (
        "This chart marks one or more price LEVELS or ZONES the trader is watching "
        "as a future area of interest — drawn as a horizontal line, a shaded "
        "box/zone, or a labeled level (support, resistance, POC, liquidity, order "
        "block). Read the EXACT price of each marked level, digit-for-digit, from "
        "its price tag on the right-hand axis or its on-chart label. Ignore the "
        "grey axis gridline numbers and the candles.\n"
        "Output ONLY this JSON, numbers only, most prominent level first:\n"
        '{"levels": [0.0, 0.0]}\n'
        "Use [] if there are no marked levels."
    ),
}


def _vision_parse(image_url: str, task: str = "trade") -> dict:
    """
    Download a chart screenshot from Discord (FULL-resolution via the cdn host —
    thumbnails are unreadable) and read its price levels with the vision LLM.

    task="trade" → returns {"entry","sl","tps":[...],"levels":[...]} for a setup.
    task="poi"   → returns {"levels":[...]} for a marked area-of-interest zone.
    "levels" is every distinct price number read; roles are assigned by geometry
    in the bot (the model reads NUMBERS reliably but mislabels entry vs SL).

    Returns {} if LOCAL_VISION_MODEL is not configured or on any error.
    """
    vision_model = os.getenv("LOCAL_VISION_MODEL", "").strip()
    if not vision_model:
        return {}
    try:
        # media.discordapp.net is Discord's RESIZING PROXY — it serves a small
        # thumbnail (e.g. 490x350) where the axis price tags are unreadable.
        # cdn.discordapp.com serves the full-resolution original (same signature),
        # which is what makes the digits legible to the model.
        full_url = image_url.replace("media.discordapp.net", "cdn.discordapp.com")
        r = _http.get(full_url, timeout=20)
        if r.status_code != 200:   # fall back to the original URL if the swap fails
            r = _http.get(image_url, timeout=20)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "image/png").split(";")[0].strip()
        content = r.content

        # Discord's media proxy serves charts as WEBP, which LM Studio rejects
        # ("'url' field must be a base64 encoded image"). Convert anything that
        # isn't PNG/JPEG to PNG before sending.
        if ct not in ("image/png", "image/jpeg", "image/jpg"):
            try:
                import io
                from PIL import Image
                im = Image.open(io.BytesIO(content)).convert("RGB")
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                content = buf.getvalue()
                ct = "image/png"
            except Exception as conv_err:
                log.warning(f"Could not convert chart image ({ct}) to PNG: {conv_err}")
                return {}

        img_b64 = base64.b64encode(content).decode("utf-8")

        client = _get_llm()
        response = client.chat.completions.create(
            model=vision_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a price-extraction tool. "
                        "You output ONLY valid JSON. No prose, no analysis, no markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{ct};base64,{img_b64}"},
                        },
                        {
                            "type": "text",
                            "text": _VISION_PROMPTS.get(task, _VISION_PROMPTS["trade"]),
                        },
                    ],
                },
            ],
            max_tokens=160,
            temperature=0.0,
        )
        raw = response.choices[0].message.content.strip()
        # Strip Qwen3 thinking blocks and markdown fences if the model wraps the answer
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()

        # Try direct parse first
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Fallback: pull the first {...} blob from a verbose response
            m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
            if not m:
                log.debug(f"Vision parse: no JSON found in: {raw[:300]}")
                return {}
            data = json.loads(m.group())

        def _sf(k):
            v = data.get(k)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        # Take-profit targets: prefer the "tps" list, fall back to a single "tp".
        tps = []
        raw_tps = data.get("tps")
        if isinstance(raw_tps, list):
            for v in raw_tps:
                try:
                    if v is not None:
                        tps.append(float(v))
                except (TypeError, ValueError):
                    pass
        elif _sf("tp") is not None:
            tps.append(_sf("tp"))

        # Merge every number the model read into a single deduped, sorted list.
        # Roles are assigned later by geometry (entry/SL/TP) using the trade side.
        levels = []
        for v in [_sf("entry"), _sf("sl")] + tps:
            if v is not None and v > 0 and v not in levels:
                levels.append(v)
        levels.sort()

        result = {"entry": _sf("entry"), "sl": _sf("sl"), "tps": tps, "levels": levels}
        log.info(f"Vision parse result: {result}")
        return result
    except Exception as e:
        log.debug(f"Vision parse failed: {e}")
        return {}


def _vision_fill(sig: Signal, msg: dict) -> Signal:
    """
    For a NEW signal missing entry/SL/TP, attempt to fill the gaps using the
    chart image attached to the Discord notification (if any).
    UPDATE and CLOSE signals are returned unchanged.
    """
    if sig.message_type != MessageType.NEW:
        return sig
    image_url = msg.get("image_url", "") or ""
    if not image_url:
        log.debug(f"No chart image for {sig.symbol} — cannot vision-fill missing levels")
        return sig
    # All three fields present — nothing to do
    if sig.entry is not None and sig.sl is not None and sig.tp is not None:
        return sig

    log.info(f"Attempting vision parse for {sig.symbol} from chart: {image_url[:70]}")
    vision = _vision_parse(image_url)
    if not vision:
        log.info(f"Vision returned no usable levels for {sig.symbol}")
        return sig

    # Stash ALL levels read off the chart. Roles (entry/SL/TP) are assigned later
    # in bot._process_new by geometry using the trade side + live entry — the model
    # reads the numbers reliably but often mislabels which is entry vs SL.
    levels = vision.get("levels") or []
    if levels:
        sig.vision_levels = levels
        log.info(f"Vision read {len(levels)} price level(s) for {sig.symbol}: {levels}")
    return sig


def _apply_cmp_flags(sig: Signal, text: str) -> Signal:
    """
    Post-process a NEW signal:
    - Mark is_market_order=True if CMP phrasing detected and entry is still None.
    - Backfill SL from "close under/below X" phrasing if sl is still None.
    """
    if sig.message_type != MessageType.NEW:
        return sig
    if sig.entry is None and _CMP_PATTERN.search(text):
        sig.is_market_order = True
    if sig.sl is None:
        m = _CLOSE_UNDER_SL.search(text)
        if m:
            try:
                sig.sl = _to_float(m.group(1))
                log.debug(f"Extracted SL from 'close under' phrasing: {sig.sl}")
            except Exception:
                pass
    return sig


def parse(msg: dict) -> Optional[Signal]:
    """
    Classify and parse a notification message dict into a Signal.
    Returns None if the message is not a recognisable trade message.

    Priority:
      1. Update keyword regex fast-path
      2. Close keyword regex fast-path
      3. New entry regex patterns  [+ CMP flags + Stage 3 vision fill]
      4. LLM fallback              [+ CMP flags + Stage 3 vision fill]
    """
    text = msg.get("content", "")

    # Stage 0 — RSI Extreme alerts (OracleAlgo). Checked FIRST, before the chatter
    # pre-filter: these alerts have a very specific anchor ("RSI Extreme") and the
    # ticker can run together with adjacent text ("…USDTRSI90.3"), which would defeat
    # the word-boundary trade-hint gate. It's a market-order strategy distinct from
    # analyst follow-trades.
    sig = _try_rsi_extreme(text, msg)
    if sig:
        log.debug(f"RSI Extreme: {sig.side} {sig.symbol} (market)")
        return sig

    # Stage 0b — OracleAlgo BTC structure/momentum alerts (MSS/BOS/Supertrend/Delta).
    # Also anchored ("LTF Bias") and exempt from the pre-filter for the same reason.
    sig = _try_oraclealgo(text, msg)
    if sig:
        log.debug(f"OracleAlgo: {sig.side} {sig.symbol} tf={sig.signal_tf}")
        return sig

    # Quick pre-filter: skip pure chatter with no trading vocabulary at all. Runs
    # after the strongly-anchored strategy parsers above so it can't drop them.
    if not _TRADE_HINT.search(text):
        return None

    # Stage 1a — update fast-path
    sig = _try_update_regex(text, msg)
    if sig:
        log.debug(f"Regex UPDATE: {sig.symbol} new_sl={sig.new_sl} new_tp={sig.new_tp}")
        return sig

    # Stage 1a2 — DCA/averaging position-management updates (avg entry, limit-filled info)
    sig = _try_position_update(text, msg)
    if sig:
        log.debug(f"Position update: {sig.symbol} new_avg_entry={sig.new_avg_entry}")
        return sig

    # Stage 1b — close fast-path
    sig = _try_close_regex(text, msg)
    if sig:
        log.debug(f"Regex CLOSE: {sig.symbol}")
        return sig

    # Stage 1c — new entry patterns
    for pattern in _NEW_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            sig = _build_new_signal(m, msg)
            if sig:
                sig = _apply_cmp_flags(sig, text)
                log.debug(f"Regex NEW: {sig.side} {sig.symbol} @ {sig.entry} "
                          f"sl={sig.sl} tp={sig.tp} market={sig.is_market_order}")
                return _vision_fill(sig, msg)

    # Stage 1d — entry-less directional market call ("Took this HYPE short").
    # Runs before the LLM so the ticker is extracted deterministically rather
    # than the LLM mistaking a ticker like HYPE for the English word.
    sig = _try_directional_market(text, msg)
    if sig:
        sig = _apply_cmp_flags(sig, text)
        log.debug(f"Directional market: {sig.side} {sig.symbol} (entry live, levels from chart)")
        return _vision_fill(sig, msg)

    # Stage 2 — LLM
    sig = _llm_parse(text, msg)
    if sig:
        sig = _apply_cmp_flags(sig, text)
        log.debug(f"LLM {sig.message_type.value}: {sig.symbol} market={sig.is_market_order}")
        return _vision_fill(sig, msg)
    return sig
