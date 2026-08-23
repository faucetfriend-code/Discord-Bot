"""Exact JSON bodies blofin_client sends to BloFin's order endpoints (no network).

Every test replaces blofin_client._send_request with a recorder so the bodies
can be compared field-by-field against https://docs.blofin.com (Trading > REST).
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import blofin_client as bf  # noqa: E402

LIVE = "https://openapi.blofin.com"
DEMO = "https://demo-trading-openapi.blofin.com"
BID = bf.DEFAULT_BROKER_ID


class Recorder:
    """Fake transport: records calls, answers from a per-(method,path) script."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, method, path, auth, params=None, data=None, authenticate=False):
        self.calls.append(dict(method=method, path=path, params=params, data=data,
                               authenticate=authenticate))
        r = self.responses.get((method, path))
        if callable(r):
            return r(params, data)
        if isinstance(r, list):
            return r.pop(0) if r else {"code": "0", "data": []}
        return r if r is not None else {"code": "0", "msg": "", "data": [{"orderId": "42"}]}

    def bodies(self, path):
        return [c["data"] for c in self.calls if c["path"] == path]


@pytest.fixture
def rec(monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE)
    monkeypatch.delenv("BLOFIN_BROKER_ID", raising=False)
    monkeypatch.delenv("ATTACH_EXCHANGE_TP", raising=False)
    monkeypatch.setattr(bf, "_client", SimpleNamespace(auth=object()))
    monkeypatch.setattr(bf, "_position_mode_cache", "long_short_mode")
    monkeypatch.setattr(bf, "_broker_id_logged", True)
    r = Recorder()
    monkeypatch.setattr(bf, "_send_request", r)
    return r


def _sig(side="buy", entry=150.0, sl=140.0, tp=170.0):
    return SimpleNamespace(symbol="XMR", side=side, entry=entry, sl=sl, tp=tp)


# ---- entries ---------------------------------------------------------------

def test_limit_entry_with_attached_sl(rec):
    bf.place_order(_sig(), 1.5)
    call = rec.calls[0]
    assert call["method"] == "POST" and call["path"] == "/api/v1/trade/order"
    assert call["authenticate"] is True
    assert call["data"] == {
        "instId": "XMR-USDT",
        "marginMode": "cross",
        "positionSide": "long",
        "side": "buy",
        "orderType": "limit",
        "price": "150.0",
        "size": "1.5",
        "reduceOnly": "false",
        "slTriggerPrice": "140.0",
        "slOrderPrice": "-1",
        "slTriggerPriceType": "last",
        "brokerId": BID,
    }


def test_limit_entry_short_maps_position_side(rec):
    bf.place_order(_sig(side="sell", entry=150.0, sl=160.0, tp=130.0), 1.0)
    body = rec.calls[0]["data"]
    assert body["side"] == "sell" and body["positionSide"] == "short"
    assert body["slTriggerPrice"] == "160.0"


def test_entry_tp_only_attached_when_enabled(rec, monkeypatch):
    bf.place_order(_sig(), 1.0)
    assert "tpTriggerPrice" not in rec.calls[0]["data"]
    monkeypatch.setenv("ATTACH_EXCHANGE_TP", "true")
    bf.place_order(_sig(), 1.0)
    body = rec.calls[1]["data"]
    assert body["tpTriggerPrice"] == "170.0"
    assert body["tpOrderPrice"] == "-1"
    assert body["tpTriggerPriceType"] == "last"


def test_market_entry_has_no_price_field(rec):
    bf.place_market_order(_sig(entry=None), 3.0)
    body = rec.calls[0]["data"]
    assert body["orderType"] == "market"
    assert "price" not in body
    assert body["size"] == "3.0"
    assert body["slTriggerPrice"] == "140.0" and body["slOrderPrice"] == "-1"
    assert body["reduceOnly"] == "false"


def test_market_entry_without_sl_omits_sl_fields(rec):
    bf.place_market_order(_sig(entry=None, sl=None, tp=None), 3.0)
    body = rec.calls[0]["data"]
    assert not any(k.startswith(("sl", "tp")) for k in body)


def test_net_mode_account_uses_net_position_side(rec, monkeypatch):
    monkeypatch.setattr(bf, "_position_mode_cache", "net_mode")
    bf.place_order(_sig(), 1.0)
    assert rec.calls[0]["data"]["positionSide"] == "net"
    bf.reduce_position("XMR", "buy", 0.5)
    assert rec.calls[1]["data"]["positionSide"] == "net"
    bf.close_position_api("XMR-USDT", "long")
    assert rec.calls[2]["data"]["positionSide"] == "net"


def test_demo_endpoint_omits_broker_id(rec, monkeypatch):
    monkeypatch.setenv("BLOFIN_BASE_URL", DEMO)
    bf.place_order(_sig(), 1.0)
    assert "brokerId" not in rec.calls[0]["data"]


# ---- response validation ---------------------------------------------------

def test_order_id_read_from_documented_orderId_key(rec):
    resp = bf.place_order(_sig(), 1.0)
    assert bf._order_id_from_resp(resp) == "42"


def test_order_id_legacy_ordId_still_accepted():
    assert bf._order_id_from_resp({"code": "0", "data": [{"ordId": "7"}]}) == "7"


def test_per_order_code_rejection_raises(rec):
    rec.responses[("POST", "/api/v1/trade/order")] = {
        "code": "0", "msg": "", "data": [{"orderId": "", "code": "152401",
                                          "msg": "insufficient balance"}]}
    with pytest.raises(bf.OrderRejected) as ei:
        bf.place_order(_sig(), 1.0)
    assert ei.value.code == "152401"


def test_top_level_code_rejection_raises(rec):
    rec.responses[("POST", "/api/v1/trade/order")] = {"code": "152012", "msg": "broker"}
    with pytest.raises(bf.OrderRejected):
        bf.place_market_order(_sig(), 1.0)


# ---- reduce-only slice / close -------------------------------------------

def test_reduce_only_slice_closes_long_with_sell_plus_long(rec):
    bf.reduce_position("XMR", "buy", 0.7)
    assert rec.calls[0]["path"] == "/api/v1/trade/order"
    assert rec.calls[0]["data"] == {
        "instId": "XMR-USDT",
        "marginMode": "cross",
        "positionSide": "long",
        "side": "sell",
        "orderType": "market",
        "size": "0.7",
        "reduceOnly": "true",
        "brokerId": BID,
    }


def test_reduce_only_slice_closes_short_with_buy_plus_short(rec):
    bf.reduce_position("XMR-USDT", "sell", 0.7)
    body = rec.calls[0]["data"]
    assert body["side"] == "buy" and body["positionSide"] == "short"
    assert "price" not in body


def test_close_position_body(rec):
    bf.close_position_api("XMR-USDT", "short")
    assert rec.calls[0]["path"] == "/api/v1/trade/close-position"
    assert rec.calls[0]["data"] == {
        "instId": "XMR-USDT", "marginMode": "cross",
        "positionSide": "short", "brokerId": BID,
    }


# ---- amend SL ----------------------------------------------------------------

def test_amend_sl_on_live_entry_order(rec):
    rec.responses[("POST", "/api/v1/trade/amend-order")] = {
        "code": "0", "data": {"orderId": "42", "code": "0", "msg": "Order modified"}}
    out = bf.amend_order("XMR", "42", new_sl=145.0, side="buy")
    assert out["code"] == "0"
    assert [c["path"] for c in rec.calls] == ["/api/v1/trade/amend-order"]
    assert rec.calls[0]["data"] == {
        "instId": "XMR-USDT", "orderId": "42",
        "newSlTriggerPrice": "145.0", "newSlOrderPrice": "-1",
    }


def test_amend_sl_and_tp_fields(rec):
    rec.responses[("POST", "/api/v1/trade/amend-order")] = {"code": "0", "data": {}}
    bf.amend_order("XMR-USDT", "42", new_sl=145.0, new_tp=180.0)
    body = rec.calls[0]["data"]
    assert body["newTpTriggerPrice"] == "180.0" and body["newTpOrderPrice"] == "-1"
    assert body["newSlTriggerPrice"] == "145.0"


def test_amend_sl_falls_back_to_pending_tpsl_after_fill(rec):
    rec.responses[("POST", "/api/v1/trade/amend-order")] = {
        "code": "102067", "msg": "Order modification failed as the order has been filled"}
    rec.responses[("GET", "/api/v1/trade/orders-tpsl-pending")] = {
        "code": "0", "data": [
            {"tpslId": "900", "instId": "XMR-USDT", "positionSide": "short",
             "slTriggerPrice": "160", "side": "buy", "size": "1"},
            {"tpslId": "901", "instId": "XMR-USDT", "positionSide": "long",
             "slTriggerPrice": "140", "side": "sell", "size": "1"},
        ]}
    rec.responses[("POST", "/api/v1/trade/amend-tpsl")] = {
        "code": "0", "data": {"tpslId": "901", "code": "0"}}
    out = bf.amend_order("XMR", "42", new_sl=150.0, side="buy")
    assert out["code"] == "0"
    paths = [c["path"] for c in rec.calls]
    assert paths == ["/api/v1/trade/amend-order", "/api/v1/trade/orders-tpsl-pending",
                     "/api/v1/trade/amend-tpsl"]
    assert rec.calls[1]["method"] == "GET"
    assert rec.calls[1]["params"]["instId"] == "XMR-USDT"
    # the LONG row (901) is picked, not the short one
    assert rec.calls[2]["data"] == {
        "instId": "XMR-USDT", "tpslId": "901",
        "newSlTriggerPrice": "150.0", "newSlOrderPrice": "-1",
    }


def test_amend_sl_replaces_tpsl_when_amend_tpsl_rejected(rec):
    rec.responses[("POST", "/api/v1/trade/amend-order")] = {"code": "102067", "msg": "filled"}
    rec.responses[("GET", "/api/v1/trade/orders-tpsl-pending")] = {
        "code": "0", "data": [{"tpslId": "901", "instId": "XMR-USDT",
                               "positionSide": "long", "slTriggerPrice": "140",
                               "side": "sell", "size": "2"}]}
    rec.responses[("POST", "/api/v1/trade/amend-tpsl")] = {"code": "1", "msg": "nope"}
    rec.responses[("POST", "/api/v1/trade/cancel-tpsl")] = {"code": "0", "data": []}
    rec.responses[("POST", "/api/v1/trade/order-tpsl")] = {
        "code": "0", "data": {"tpslId": "902", "code": "0"}}
    out = bf.amend_order("XMR", "42", new_sl=150.0, side="buy")
    assert out["data"]["tpslId"] == "902"
    assert rec.bodies("/api/v1/trade/cancel-tpsl") == [[{"instId": "XMR-USDT", "tpslId": "901"}]]
    assert rec.bodies("/api/v1/trade/order-tpsl") == [{
        "instId": "XMR-USDT", "marginMode": "cross", "positionSide": "long",
        "side": "sell", "size": "2", "reduceOnly": "true",
        "slTriggerPrice": "150.0", "slOrderPrice": "-1", "slTriggerPriceType": "last",
        "brokerId": BID,
    }]


def test_amend_sl_places_new_tpsl_when_none_pending(rec, monkeypatch):
    rec.responses[("POST", "/api/v1/trade/amend-order")] = {"code": "102067", "msg": "filled"}
    rec.responses[("GET", "/api/v1/trade/orders-tpsl-pending")] = {"code": "0", "data": []}
    rec.responses[("POST", "/api/v1/trade/order-tpsl")] = {
        "code": "0", "data": {"tpslId": "903", "code": "0"}}
    monkeypatch.setattr(bf, "get_live_positions", lambda: [
        {"symbol": "XMR-USDT", "side": "buy", "size": 1.3, "avg_price": 150, "liq_price": 0}])
    out = bf.amend_order("XMR", "42", new_sl=150.0, side="buy")
    assert out["data"]["tpslId"] == "903"
    body = rec.bodies("/api/v1/trade/order-tpsl")[0]
    assert body["size"] == "1.3" and body["side"] == "sell"
    assert body["positionSide"] == "long" and body["reduceOnly"] == "true"


def test_amend_without_side_and_no_pending_returns_empty(rec):
    rec.responses[("POST", "/api/v1/trade/amend-order")] = {"code": "102067", "msg": "filled"}
    rec.responses[("GET", "/api/v1/trade/orders-tpsl-pending")] = {"code": "0", "data": []}
    assert bf.amend_order("XMR", "42", new_sl=150.0) == {}
    assert "/api/v1/trade/order-tpsl" not in [c["path"] for c in rec.calls]


def test_amend_noop_without_targets(rec):
    assert bf.amend_order("XMR", "42") == {}
    assert rec.calls == []


# ---- cancel-all batching ----------------------------------------------------

def test_cancel_all_batches_of_twenty(rec):
    orders = [{"orderId": str(i), "instId": "XMR-USDT"} for i in range(45)]
    tpsl = [{"tpslId": str(100 + i), "instId": "XMR-USDT"} for i in range(3)]
    rec.responses[("GET", "/api/v1/trade/orders-pending")] = {"code": "0", "data": orders}
    rec.responses[("GET", "/api/v1/trade/orders-tpsl-pending")] = {"code": "0", "data": tpsl}
    rec.responses[("POST", "/api/v1/trade/cancel-batch-orders")] = lambda p, d: {
        "code": "0", "data": [{"orderId": x["orderId"]} for x in d]}
    rec.responses[("POST", "/api/v1/trade/cancel-tpsl")] = lambda p, d: {
        "code": "0", "data": [{"tpslId": x["tpslId"]} for x in d]}

    out = bf.cancel_all_orders()

    batches = rec.bodies("/api/v1/trade/cancel-batch-orders")
    assert [len(b) for b in batches] == [20, 20, 5]
    assert batches[0][0] == {"instId": "XMR-USDT", "orderId": "0"}
    assert rec.bodies("/api/v1/trade/cancel-tpsl") == [
        [{"instId": "XMR-USDT", "tpslId": "100"}, {"instId": "XMR-USDT", "tpslId": "101"},
         {"instId": "XMR-USDT", "tpslId": "102"}]]
    assert out == {"orders_found": 45, "orders_cancelled": 45,
                   "tpsl_found": 3, "tpsl_cancelled": 3, "errors": []}
    # the pending GETs are not filtered when no inst_id is given
    assert rec.calls[0]["params"] == {"limit": "100"}


def test_cancel_all_inst_filter_and_partial_failure(rec):
    rec.responses[("GET", "/api/v1/trade/orders-pending")] = {
        "code": "0", "data": [{"orderId": "1", "instId": "XMR-USDT"},
                              {"orderId": "2", "instId": "XMR-USDT"}]}
    rec.responses[("GET", "/api/v1/trade/orders-tpsl-pending")] = {"code": "0", "data": []}
    rec.responses[("POST", "/api/v1/trade/cancel-batch-orders")] = {
        "code": "0", "data": [{"orderId": "1"},
                              {"orderId": "2", "code": "1000", "msg": "already filled"}]}
    out = bf.cancel_all_orders("XMR")
    assert rec.calls[0]["params"] == {"instId": "XMR-USDT", "limit": "100"}
    assert out["orders_found"] == 2 and out["orders_cancelled"] == 1
    assert out["errors"] == ["2: already filled"]
    assert "/api/v1/trade/cancel-tpsl" not in [c["path"] for c in rec.calls]


def test_cancel_all_paginates_pending(rec):
    page1 = [{"orderId": str(i), "instId": "XMR-USDT"} for i in range(100)]
    page2 = [{"orderId": "500", "instId": "XMR-USDT"}]
    rec.responses[("GET", "/api/v1/trade/orders-pending")] = [
        {"code": "0", "data": page1}, {"code": "0", "data": page2}]
    rec.responses[("GET", "/api/v1/trade/orders-tpsl-pending")] = {"code": "0", "data": []}
    rec.responses[("POST", "/api/v1/trade/cancel-batch-orders")] = lambda p, d: {
        "code": "0", "data": [{"orderId": x["orderId"]} for x in d]}
    out = bf.cancel_all_orders()
    gets = [c for c in rec.calls if c["path"] == "/api/v1/trade/orders-pending"]
    assert gets[1]["params"]["after"] == "99"
    assert out["orders_found"] == 101 and out["orders_cancelled"] == 101
