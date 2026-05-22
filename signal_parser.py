"""
Two-stage signal parser.
Stage 1: regex patterns for common analyst formats.
Stage 2: local Qwen via LM Studio OpenAI-compatible API (fallback).
Returns a Signal or None.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI
from logger import log

_llm_client: Optional[OpenAI] = None


def _get_llm() -> OpenAI:
    global _llm_client
    if _llm_client is None:
        base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
        _llm_client = OpenAI(base_url=base_url, api_key="local")  # LM Studio ignores api_key
    return _llm_client


@dataclass
class Signal:
    symbol: str    # normalised to "BTC-USDT" form
    side: str      # "buy" or "sell"
    entry: float
    sl: float
    tp: float
    analyst: str
    raw_text: str


# ---------------------------------------------------------------------------
# Regex patterns — ordered most-to-least specific
# ---------------------------------------------------------------------------
_PATTERNS = [
    # "LONG BTC | Entry: 45000 | SL: 44000 | TP: 47000"
    # "SHORT ETH Entry 3200 SL 3300 TP 3000"
    r'(?P<side>long|short|buy|sell)\s+(?P<sym>[A-Z]{2,10})(?:[/-]USDT?)?\b'
    r'.*?entr(?:y|:)[\s:]*(?P<entry>[\d,.]+)'
    r'.*?s\.?l\.?[\s:]*(?P<sl>[\d,.]+)'
    r'.*?t\.?p\.?[\s:]*(?P<tp>[\d,.]+)',

    # "BUY BTC-USDT @ 45000, SL 44000, TP 47000"
    r'(?P<side>buy|sell)\s+(?P<sym>[A-Z]{2,10})(?:[/-]USDT?)?\s*@\s*(?P<entry>[\d,.]+)'
    r'.*?s\.?l\.?[\s:]*(?P<sl>[\d,.]+)'
    r'.*?t\.?p\.?[\s:]*(?P<tp>[\d,.]+)',

    # "⚡ ASTER/USDT LONG  Entry 0.67  SL 0.63  TP 0.70 TP2 0.75"
    r'(?P<sym>[A-Z]{2,10})(?:[/-]USDT?)?\s+(?P<side>long|short|buy|sell)'
    r'.*?entr(?:y|:)[\s:]*(?P<entry>[\d,.]+)'
    r'.*?s\.?l\.?[\s:]*(?P<sl>[\d,.]+)'
    r'.*?t\.?p\.?[\s:]*(?P<tp>[\d,.]+)',
]


def _normalise_symbol(raw: str) -> str:
    raw = raw.upper().replace("/", "-").replace("USDT", "").rstrip("-")
    return f"{raw}-USDT"


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def _build_signal(m: re.Match, msg: dict) -> Optional[Signal]:
    try:
        side_raw = m.group("side").lower()
        side = "buy" if side_raw in ("buy", "long") else "sell"
        symbol = _normalise_symbol(m.group("sym"))
        entry = _to_float(m.group("entry"))
        sl = _to_float(m.group("sl"))
        tp = _to_float(m.group("tp"))
        return Signal(symbol=symbol, side=side, entry=entry, sl=sl, tp=tp,
                      analyst=msg.get("author", ""), raw_text=msg.get("content", ""))
    except Exception as e:
        log.debug(f"Could not build signal from regex match: {e}")
        return None


def _llm_parse(text: str, msg: dict) -> Optional[Signal]:
    """Ask local Qwen to extract a structured signal from unstructured text."""
    model = os.getenv("LOCAL_LLM_MODEL", "qwen3.5-9b")
    prompt = (
        "You are a trading signal parser. Extract the trade signal from the message below "
        "and return ONLY a raw JSON object (no markdown, no explanation) with these exact keys: "
        "symbol (string, e.g. BTC-USDT), side (string: buy or sell), "
        "entry (number), sl (number), tp (number). "
        'If no clear trade signal is present, return exactly: {"signal": null}\n\n'
        f"Message:\n{text[:600]}"
    )
    try:
        client = _get_llm()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.0,
        )
        raw = response.choices[0].message.content.strip()
        # Strip any accidental markdown fences
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
        data = json.loads(raw)
        if data.get("signal") is None or "symbol" not in data:
            return None
        return Signal(
            symbol=_normalise_symbol(str(data["symbol"])),
            side=str(data["side"]).lower(),
            entry=float(data["entry"]),
            sl=float(data["sl"]),
            tp=float(data["tp"]),
            analyst=msg.get("author", ""),
            raw_text=text,
        )
    except Exception as e:
        log.debug(f"LLM parse failed: {e}")
        return None


def parse(msg: dict) -> Optional[Signal]:
    """
    Attempt to parse a notification message dict into a Signal.
    Returns None if the message is not a recognisable trade call.
    """
    text = msg.get("content", "")

    # Stage 1 — regex
    for pattern in _PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            sig = _build_signal(m, msg)
            if sig:
                log.debug(f"Regex parsed signal: {sig.side} {sig.symbol} @ {sig.entry}")
                return sig

    # Stage 2 — local LLM
    sig = _llm_parse(text, msg)
    if sig:
        log.debug(f"LLM parsed signal: {sig.side} {sig.symbol} @ {sig.entry}")
    return sig
