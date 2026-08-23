"""Sherlock Alerts structured-entry parsing.

Sherlock posts "$TICKERUSDT LONG  Entry: MARKET|LIMIT PRICE ($x)  Stoploss:
4H CLOSE ABOVE|BELOW $y  DCA: ...  TP1: ...  TARGET: $z  RATING: n/10".
The "CLOSE" inside the soft-stop phrasing used to hijack the close fast-path.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signal_parser as sp  # noqa: E402
from signal_parser import MessageType  # noqa: E402

AUTHOR = "Sherlock | Defi Researcher"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(sp, "_llm_parse", lambda text, msg: None)
    monkeypatch.setattr(sp, "_vision_fill", lambda sig, msg: sig)
    monkeypatch.setattr(sp, "_vision_parse", lambda *a, **k: None)


def _parse(text):
    return sp.parse(
        {
            "id": "x",
            "author": AUTHOR,
            "content": text,
            "time": "",
            "image_url": "",
            "server": "",
        }
    )


def test_market_long_with_tp1_and_target():
    sig = _parse(
        "$ONDSUSDT LONG  Entry: MARKET PRICE ($9.26)  "
        "Stoploss: 4H CLOSE BELOW $8.61  DCA: $8.82  TP1: $9.72 (MOVE B.E)  "
        "TARGET: $12.94  RATING: 6/10  REASONING: strong trend  @Sherlock Alerts"
    )
    assert sig is not None
    assert sig.message_type == MessageType.NEW
    assert sig.symbol == "ONDS-USDT"
    assert sig.side == "buy"
    assert sig.entry == 9.26
    assert sig.is_market_order is True
    assert sig.sl == 8.61
    assert sig.soft_stop is True
    assert sig.tp == 12.94
    assert sig.tps == [9.72, 12.94]


def test_market_short_close_above():
    sig = _parse(
        "$ARPAUSDT SHORT  Entry: MARKET PRICE ($0.008422)  "
        "Stoploss: 4H CLOSE ABOVE $0.008747  DCA: $0.008638  TARGET: $0.007214  "
        "RATING: 6/10  REASONING: ...  @Sherlock Alerts"
    )
    assert sig.message_type == MessageType.NEW
    assert sig.symbol == "ARPA-USDT"
    assert sig.side == "sell"
    assert sig.entry == 0.008422
    assert sig.is_market_order is True
    assert sig.sl == 0.008747
    assert sig.soft_stop is True
    assert sig.tp == 0.007214
    assert sig.tps is None


def test_limit_short_is_not_market_order():
    sig = _parse(
        "$RSRUSDT SHORT  Entry: LIMIT PRICE ($0.001159)  "
        "Stoploss: 4H CLOSE ABOVE $0.001222  DCA: $0.001200  TARGET: $0.000901"
    )
    assert sig.message_type == MessageType.NEW
    assert sig.symbol == "RSR-USDT"
    assert sig.side == "sell"
    assert sig.entry == 0.001159
    assert sig.is_market_order is False
    assert sig.sl == 0.001222
    assert sig.soft_stop is True
    assert sig.tp == 0.000901


def test_limit_long_with_tp1_ladder():
    sig = _parse(
        "$XAIUSDT LONG  Entry: LIMIT PRICE ($0.006191)  "
        "Stoploss: 4H CLOSE ABOVE $0.005888  DCA: $0.006002  TP1: $0.006504  "
        "TARGET: $0.008170  RATING: 5/10"
    )
    assert sig.message_type == MessageType.NEW
    assert sig.symbol == "XAI-USDT"
    assert sig.side == "buy"
    assert sig.entry == 0.006191
    assert sig.sl == 0.005888
    assert sig.tp == 0.00817
    assert sig.tps == [0.006504, 0.00817]


def test_ticker_without_dollar_and_with_digits():
    sig = _parse(
        "LAYERUSDT SHORT  Entry: MARKET PRICE ($0.05893)  "
        "Stoploss: 4H CLOSE ABOVE $0.06558  DCA: $0.06280  TARGET: $0.0383"
    )
    assert sig.message_type == MessageType.NEW
    assert sig.symbol == "LAYER-USDT"
    sig = _parse(
        "$BANANAS31USDT LONG  Entry: LIMIT PRICE ($0.009257)  "
        "Stoploss: 4H CLOSE BELOW $0.008364  DCA: $0.008706  TARGET: $0.0125"
    )
    assert sig.message_type == MessageType.NEW
    assert sig.symbol == "BANANAS31-USDT"
    assert sig.side == "buy"


def test_other_sherlock_messages_keep_classification():
    # "moving stops to entry" has no regex fast-path (same as before this change);
    # the regex stages must not turn it into a NEW/CLOSE, so it falls to the LLM.
    upd = _parse("$RSR UPDATE X SHORT  Taking TP1 here and moving stops to entry.")
    assert upd is None or upd.message_type == MessageType.UPDATE

    closed = _parse("ZBT UPDATE x SHORT  Entry filled, closed in small profit.")
    assert closed is not None
    assert closed.message_type in (MessageType.CLOSE, MessageType.UPDATE)

    # "Closing" is not in the _TRADE_HINT pre-filter (unchanged from before this
    # change), so this one is left to the LLM fallback; it must never become NEW.
    closing = _parse("XAGUSD UPDATE   Closing it here its moving very fast")
    assert closing is None or closing.message_type == MessageType.CLOSE


def test_close_under_sl_regex_widened():
    assert sp._CLOSE_UNDER_SL.search("4H CLOSE ABOVE $8.61").group(1) == "8.61"
    assert sp._CLOSE_UNDER_SL.search("close under 0.0939").group(1) == "0.0939"
    assert sp._CLOSE_UNDER_SL.search("closes over $1,200").group(1) == "1,200"
