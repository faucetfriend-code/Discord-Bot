"""brokerId resolution and injection in blofin_client (no network)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import blofin_client as bf  # noqa: E402

LIVE = "https://openapi.blofin.com"
DEMO = "https://demo-trading-openapi.blofin.com"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("BLOFIN_BROKER_ID", raising=False)
    monkeypatch.setattr(bf, "_broker_id_logged", False)
    yield


# ---- _broker_id() resolution ---------------------------------------------

def test_demo_returns_none(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", DEMO)
    assert bf._broker_id() is None


def test_demo_ignores_env_override(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", DEMO)
    monkeypatch.setenv("BLOFIN_BROKER_ID", "abc123")
    assert bf._broker_id() is None


def test_live_default(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE)
    assert bf._broker_id() == bf.DEFAULT_BROKER_ID == "dd3511977f23cc87"


def test_live_trailing_slash_still_live(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE + "/")
    assert bf._broker_id() == bf.DEFAULT_BROKER_ID


def test_live_env_none_disables(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE)
    monkeypatch.setenv("BLOFIN_BROKER_ID", "none")
    assert bf._broker_id() is None
    monkeypatch.setenv("BLOFIN_BROKER_ID", " NONE ")
    assert bf._broker_id() is None


def test_live_env_custom(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE)
    monkeypatch.setenv("BLOFIN_BROKER_ID", "mybroker01")
    assert bf._broker_id() == "mybroker01"


def test_live_env_empty_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE)
    monkeypatch.setenv("BLOFIN_BROKER_ID", "")
    assert bf._broker_id() == bf.DEFAULT_BROKER_ID


# ---- injection into order bodies ------------------------------------------

class _FakeSend:
    """Stands in for blofin_client._send_request; records every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, path, auth, params=None, data=None, authenticate=False):
        self.calls.append(dict(method=method, path=path, params=params,
                               data=data, authenticate=authenticate))
        return {"code": "0", "msg": "", "data": [{"orderId": "1", "code": "0"}]}


def _install_fake_client(monkeypatch):
    fake = SimpleNamespace(auth=object())
    monkeypatch.setattr(bf, "_client", fake)
    monkeypatch.setattr(bf, "_position_mode_cache", "long_short_mode")
    send = _FakeSend()
    monkeypatch.setattr(bf, "_send_request", send)
    return send


def _signal():
    return SimpleNamespace(symbol="XMR", side="buy", entry=150.0,
                           sl=140.0, tp=170.0)


def test_place_order_live_includes_broker_id(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE)
    send = _install_fake_client(monkeypatch)
    bf.place_order(_signal(), 1.0)
    body = send.calls[0]["data"]
    assert body["brokerId"] == bf.DEFAULT_BROKER_ID
    assert body["instId"] == "XMR-USDT"


def test_place_order_demo_omits_broker_id(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", DEMO)
    send = _install_fake_client(monkeypatch)
    bf.place_order(_signal(), 1.0)
    assert "brokerId" not in send.calls[0]["data"]


def test_market_and_reduce_orders_live(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE)
    monkeypatch.setenv("BLOFIN_BROKER_ID", "custom1")
    send = _install_fake_client(monkeypatch)
    bf.place_market_order(_signal(), 2.0)
    bf.reduce_position("XMR", "buy", 1.0)
    assert [c["data"]["brokerId"] for c in send.calls] == ["custom1", "custom1"]
    assert send.calls[1]["data"]["reduceOnly"] == "true"


def test_live_env_none_omits_broker_id(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE)
    monkeypatch.setenv("BLOFIN_BROKER_ID", "none")
    send = _install_fake_client(monkeypatch)
    bf.place_market_order(_signal(), 2.0)
    assert "brokerId" not in send.calls[0]["data"]


def test_close_position_uses_send_request_with_broker_id(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE)
    send = _install_fake_client(monkeypatch)
    bf.close_position_api("XMR-USDT", "long")
    sent = send.calls[0]
    assert sent["method"] == "POST"
    assert sent["path"] == "/api/v1/trade/close-position"
    assert sent["authenticate"] is True
    assert sent["data"] == {
        "instId": "XMR-USDT",
        "marginMode": "cross",
        "positionSide": "long",
        "brokerId": bf.DEFAULT_BROKER_ID,
    }


def test_close_position_demo_omits_broker_id(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", DEMO)
    send = _install_fake_client(monkeypatch)
    bf.close_position_api("XMR-USDT", "long")
    assert "brokerId" not in send.calls[0]["data"]


def test_broker_id_logged_once(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE)
    msgs = []
    monkeypatch.setattr(bf.log, "info", lambda m, *a, **k: msgs.append(str(m)))
    bf._with_broker_id({})
    bf._with_broker_id({})
    assert sum("brokerId in use" in m for m in msgs) == 1
