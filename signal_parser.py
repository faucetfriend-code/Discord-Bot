"""
Two-stage signal parser with message classification.

Stage 1: regex fast-path for new entries and common update phrases.
Stage 2: local Qwen via LM Studio (fallback and classifier for ambiguous messages).

MessageType routing:
  NEW    → open a new position
  UPDATE → amend SL/TP on an existing position
  CLOSE  → close an existing position
  NONE   → not a trade message (ignore)
"""

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from openai import OpenAI
from logger import log


class MessageType(Enum):
    NEW = "new"
    UPDATE = "update"
    CLOSE = "close"
    NONE = "none"


_llm_client: Optional[OpenAI] = None


def _get_llm() -> OpenAI:
    global _llm_client
    if _llm_client is None:
        base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
        _llm_client = OpenAI(base_url=base_url, api_key="local")
    return _llm_client


@dataclass
class Signal:
    message_type: MessageType
    symbol: str
    side: str           # "buy" or "sell" — may be empty string for UPDATE/CLOSE
    analyst: str
    raw_text: str
    entry: Optional[float] = None   # new signals only
    sl: Optional[float] = None      # new signal SL
    tp: Optional[float] = None      # new signal TP
    new_sl: Optional[float] = None  # update: new stop loss value
    new_tp: Optional[float] = None  # update: new take profit value


# ---------------------------------------------------------------------------
# Regex — new entry patterns (ordered most-to-least specific)
# ---------------------------------------------------------------------------
_NEW_PATTERNS = [
    # "LONG BTC | Entry: 45000 | SL: 44000 | TP: 47000"
    r'(?P<side>long|short|buy|sell)\s+(?P<sym>[A-Z]{2,10})(?:[/-]USDT?)?\b'
    r'.*?entr(?:y|:)[\s:]*(?P<entry>[\d,.]+)'
    r'.*?s\.?l\.?[\s:]*(?P<sl>[\d,.]+)'
    r'.*?t\.?p\.?[\s:]*(?P<tp>[\d,.]+)',

    # "BUY BTC-USDT @ 45000, SL 44000, TP 47000"
    r'(?P<side>buy|sell)\s+(?P<sym>[A-Z]{2,10})(?:[/-]USDT?)?\s*@\s*(?P<entry>[\d,.]+)'
    r'.*?s\.?l\.?[\s:]*(?P<sl>[\d,.]+)'
    r'.*?t\.?p\.?[\s:]*(?P<tp>[\d,.]+)',

    # "⚡ ASTER/USDT LONG  Entry 0.67  SL 0.63  TP 0.70"
    r'(?P<sym>[A-Z]{2,10})(?:[/-]USDT?)?\s+(?P<side>long|short|buy|sell)'
    r'.*?entr(?:y|:)[\s:]*(?P<entry>[\d,.]+)'
    r'.*?s\.?l\.?[\s:]*(?P<sl>[\d,.]+)'
    r'.*?t\.?p\.?[\s:]*(?P<tp>[\d,.]+)',
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
    r'|partial\s+(?:close|profit|tp)',
    re.IGNORECASE,
)

_CLOSE_KEYWORDS = re.compile(
    r'\b(?:close|closed|closing|exit|exiting|exited|out\s+of)\b'
    r'(?!\s+(?:long|short|buy|sell))',  # not "close long" (that's a new entry direction)
    re.IGNORECASE,
)

# Extract a number after SL/stop keywords in an update message
_UPDATE_SL_EXTRACT = re.compile(
    r'(?:s\.?l|stop)(?:\s+to|\s*=|:)?\s*([\d,.]+)',
    re.IGNORECASE,
)
_UPDATE_TP_EXTRACT = re.compile(
    r'(?:t\.?p\b|take\s*profit)(?:\s+to|\s*=|:)?\s*([\d,.]+)',
    re.IGNORECASE,
)
_SYMBOL_EXTRACT = re.compile(
    r'\b([A-Z]{2,10})(?:[/-]USDT?)?\b',
)


def _normalise_symbol(raw: str) -> str:
    raw = raw.upper().replace("/", "-").replace("USDT", "").rstrip("-")
    return f"{raw}-USDT"


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def _build_new_signal(m: re.Match, msg: dict) -> Optional[Signal]:
    try:
        side_raw = m.group("side").lower()
        side = "buy" if side_raw in ("buy", "long") else "sell"
        symbol = _normalise_symbol(m.group("sym"))
        entry = _to_float(m.group("entry"))
        sl = _to_float(m.group("sl"))
        tp = _to_float(m.group("tp"))
        return Signal(
            message_type=MessageType.NEW,
            symbol=symbol, side=side, entry=entry, sl=sl, tp=tp,
            analyst=msg.get("author", ""), raw_text=msg.get("content", ""),
        )
    except Exception as e:
        log.debug(f"Could not build new signal from regex match: {e}")
        return None


def _try_update_regex(text: str, msg: dict) -> Optional[Signal]:
    """Fast-path: detect common update phrases and extract new SL/TP if present."""
    if not _UPDATE_KEYWORDS.search(text):
        return None

    # Try to find a symbol
    sym_match = _SYMBOL_EXTRACT.search(text.upper())
    symbol = _normalise_symbol(sym_match.group(1)) if sym_match else "UNKNOWN-USDT"

    new_sl_m = _UPDATE_SL_EXTRACT.search(text)
    new_tp_m = _UPDATE_TP_EXTRACT.search(text)
    new_sl = _to_float(new_sl_m.group(1)) if new_sl_m else None
    new_tp = _to_float(new_tp_m.group(1)) if new_tp_m else None

    return Signal(
        message_type=MessageType.UPDATE,
        symbol=symbol, side="", new_sl=new_sl, new_tp=new_tp,
        analyst=msg.get("author", ""), raw_text=text,
    )


def _try_close_regex(text: str, msg: dict) -> Optional[Signal]:
    """Fast-path: detect explicit close/exit phrases."""
    if not _CLOSE_KEYWORDS.search(text):
        return None
    # Only treat as CLOSE if there's no entry price (otherwise it's a new signal direction)
    if re.search(r'entr(?:y|:)[\s:]*[\d,.]+', text, re.IGNORECASE):
        return None

    sym_match = _SYMBOL_EXTRACT.search(text.upper())
    symbol = _normalise_symbol(sym_match.group(1)) if sym_match else "UNKNOWN-USDT"

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
        '    "new"    = opening a new position (has entry price)\n'
        '    "update" = modifying SL/TP on an existing position '
        '(break even, trailing stop, partial TP hit, SL move)\n'
        '    "close"  = fully exiting/closing a position\n'
        '    "none"   = not a trade message\n'
        '  "symbol": string e.g. "BTC-USDT"  (null if absent)\n'
        '  "side":   "buy" | "sell"           (null if absent)\n'
        '  "entry":  number                   (null if absent, new only)\n'
        '  "sl":     number                   (null if absent, new signal SL)\n'
        '  "tp":     number                   (null if absent, new signal TP)\n'
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

        return Signal(
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
    except Exception as e:
        log.debug(f"LLM parse failed: {e}")
        return None


def parse(msg: dict) -> Optional[Signal]:
    """
    Classify and parse a notification message dict into a Signal.
    Returns None if the message is not a recognisable trade message.

    Priority:
      1. Update keyword regex fast-path
      2. Close keyword regex fast-path
      3. New entry regex patterns
      4. LLM fallback (classifies type + extracts fields)
    """
    text = msg.get("content", "")

    # Stage 1a — update fast-path
    sig = _try_update_regex(text, msg)
    if sig:
        log.debug(f"Regex UPDATE: {sig.symbol} new_sl={sig.new_sl} new_tp={sig.new_tp}")
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
                log.debug(f"Regex NEW: {sig.side} {sig.symbol} @ {sig.entry}")
                return sig

    # Stage 2 — LLM
    sig = _llm_parse(text, msg)
    if sig:
        log.debug(f"LLM {sig.message_type.value}: {sig.symbol}")
    return sig
