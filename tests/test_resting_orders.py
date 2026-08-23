"""Resting-limit lifecycle on DEMO/LIVE (mocked blofin_client, temp sqlite).

A LIMIT entry placed on the exchange must NOT become a DB position until the
exchange reports a fill; the resting record is polled, opened at the real avg
fill, cancelled when stale, and reconciled on startup.
"""

import os
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import exec_mode  # noqa: E402
import position_tracker as pt  # noqa: E402
import bot  # noqa: E402
from logger import now_local  # noqa: E402
from signal_parser import MessageType  # noqa: E402


class FakeExchange:
    """Scripted stand-in for blofin_client. `status` is what get_order_status
    answers; `cancels` / `placed` record what the bot asked for."""

    def __init__(self, price=100.0, balance=10_000.0):
        self.price = price
        self.balance = balance
        self.status = None
        self.fills = (0.0, 0.0)
        self.positions = []
        self.cancels = []
        self.placed = []
        self.cancel_ok = True
        self.specs = {"contract_value": 1.0, "lot_size": 1.0, "min_size": 1.0}

    # -- reads
    def get_market_price(self, symbol):
        return self.price

    def get_balance(self):
        return self.balance

    def get_contract_specs(self, symbol):
        return self.specs

    def get_order_status(self, symbol, order_id):
        return self.status

    def get_fills_for_order(self, symbol, order_id):
        return self.fills

    def get_live_positions(self):
        return list(self.positions)

    def get_recent_fills(self, symbol):
        return []

    # -- writes
    def set_leverage(self, symbol, lev, margin_mode="cross"):
        return lev

    def place_order(self, signal, size):
        self.placed.append(("limit", signal.symbol, size))
        return {"code": "0", "data": [{"orderId": "L1"}]}

    def place_market_order(self, signal, size):
        self.placed.append(("market", signal.symbol, size))
        return {"code": "0", "data": [{"orderId": "M1"}]}

    def cancel_order(self, symbol, order_id):
        self.cancels.append(order_id)
        return self.cancel_ok

    def _order_id_from_resp(self, resp):
        return resp["data"][0]["orderId"]

    OrderRejected = bot.bf.OrderRejected


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp DB, DEMO mode, every exchange call routed to FakeExchange, and the
    CSV/alert side channels stubbed so the suite never touches real files."""
    monkeypatch.setattr(pt, "DB_PATH", tmp_path / "test.db")
    pt.init_db()
    monkeypatch.setenv("PAPER_MODE", "false")
    monkeypatch.setenv("BLOFIN_BASE_URL", exec_mode.DEMO_BASE_URL)
    exec_mode.unfreeze()
    assert exec_mode.calls_exchange()
    fx = FakeExchange()
    monkeypatch.setattr(bot, "bf", fx)
    outcomes = []
    monkeypatch.setattr(bot._logger_mod, "log_signal",
                        lambda analyst, raw, **kw: outcomes.append((analyst, kw.get("outcome"))))
    monkeypatch.setattr(bot, "_alert", lambda *a, **k: None)
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    monkeypatch.setattr(bot, "_last_resting_check", 0.0)
    monkeypatch.setattr(bot.rm, "calculate_size", lambda balance, signal: 10.0)
    monkeypatch.setenv("LIMIT_STALE_PCT", "0.10")
    monkeypatch.setenv("LIMIT_STALE_HOURS", "48")
    fx.outcomes = outcomes
    yield fx
    exec_mode.unfreeze()


def _limit_signal(entry=90.0, sl=80.0, tp=120.0, side="buy"):
    return bot.sp.Signal(message_type=MessageType.NEW, symbol="XMR", side=side,
                         analyst="Sveezy", raw_text="XMR long 90 sl 80 tp 120",
                         entry=entry, sl=sl, tp=tp, is_market_order=False,
                         source="analyst")


def _place_limit(fx):
    bot._process_new(_limit_signal(), [], dry_run=True, leverage=20, analyst_key="Sveezy")


# -- 1. limit placed -> resting, no position --------------------------------

def test_limit_entry_rests_and_opens_no_position(env):
    fx = env
    _place_limit(fx)
    assert fx.placed == [("limit", "XMR", 10.0)]
    assert pt.get_open_positions() == []
    resting = bot._load_resting()
    assert len(resting) == 1
    rec = resting[0]
    assert rec["order_id"] == "L1" and rec["entry"] == 90.0 and rec["size"] == 10.0
    assert rec["sl"] == 80.0 and rec["tps"] == [120.0] and rec["leverage"] == 20
    assert rec["analyst_key"] == "Sveezy" and rec["created_price"] == 100.0
    assert ("Sveezy", "limit_resting") in fx.outcomes


def test_second_limit_same_source_symbol_is_rejected(env):
    fx = env
    _place_limit(fx)
    _place_limit(fx)
    assert len(fx.placed) == 1
    assert ("Sveezy", "rejected:limit_already_resting") in fx.outcomes


# -- 2. filled -> position at actual fill ------------------------------------

def test_filled_limit_opens_position_at_avg_fill(env):
    fx = env
    _place_limit(fx)
    fx.status = {"state": "filled", "pending": False, "filled_size": 10.0,
                 "avg_price": 89.7, "size": 10.0, "source": "orders-history"}
    bot._check_resting_orders(force=True)
    assert bot._load_resting() == []
    pos = pt.get_open_positions()
    assert len(pos) == 1
    p = pos[0]
    assert p.entry == 89.7 and p.size == 10.0 and p.orig_size == 10.0
    assert p.order_id == "L1" and p.analyst == "Sveezy" and p.tps == [120.0]
    assert p.sl == 80.0 and p.leverage == 20
    assert any(o[1].startswith("executed (limit fill @ 89.7") for o in fx.outcomes)


def test_still_resting_is_kept_and_not_opened(env):
    fx = env
    _place_limit(fx)
    fx.status = {"state": "live", "pending": True, "filled_size": 0.0,
                 "avg_price": 0.0, "size": 10.0, "source": "orders-pending"}
    bot._check_resting_orders(force=True)
    assert len(bot._load_resting()) == 1
    assert pt.get_open_positions() == []
    assert fx.cancels == []


def test_externally_cancelled_is_dropped(env):
    fx = env
    _place_limit(fx)
    fx.status = {"state": "canceled", "pending": False, "filled_size": 0.0,
                 "avg_price": 0.0, "size": 10.0, "source": "orders-history"}
    bot._check_resting_orders(force=True)
    assert bot._load_resting() == []
    assert pt.get_open_positions() == []
    assert ("Sveezy", "limit_cancelled_external XMR") in fx.outcomes


def test_vanished_order_needs_three_misses(env):
    fx = env
    _place_limit(fx)
    fx.status = {"state": "unknown", "pending": False, "filled_size": 0.0,
                 "avg_price": 0.0, "size": 0.0, "source": "none"}
    bot._check_resting_orders(force=True)
    bot._check_resting_orders(force=True)
    assert len(bot._load_resting()) == 1
    bot._check_resting_orders(force=True)
    assert bot._load_resting() == []
    assert ("Sveezy", "limit_vanished XMR") in fx.outcomes


def test_exchange_unreachable_keeps_resting(env):
    fx = env
    _place_limit(fx)
    fx.status = None
    bot._check_resting_orders(force=True)
    assert len(bot._load_resting()) == 1


# -- 3. stale -> cancelled ---------------------------------------------------

def test_stale_by_drift_cancels_on_exchange(env):
    fx = env
    _place_limit(fx)                       # buy @ 90, market 100 at placement
    fx.price = 115.0                       # ran > 10% further away
    fx.status = {"state": "live", "pending": True, "filled_size": 0.0,
                 "avg_price": 0.0, "size": 10.0, "source": "orders-pending"}
    bot._check_resting_orders(force=True)
    assert fx.cancels == ["L1"]
    assert bot._load_resting() == []
    assert pt.get_open_positions() == []
    assert ("Sveezy", "limit_stale XMR") in fx.outcomes


def test_stale_by_age_cancels_on_exchange(env):
    fx = env
    _place_limit(fx)
    items = bot._load_resting()
    items[0]["placed_at"] = (now_local() - timedelta(hours=49)).isoformat()
    bot._save_resting(items)
    fx.status = {"state": "live", "pending": True, "filled_size": 0.0,
                 "avg_price": 0.0, "size": 10.0, "source": "orders-pending"}
    bot._check_resting_orders(force=True)
    assert fx.cancels == ["L1"]
    assert bot._load_resting() == []


def test_stale_cancel_adopts_partial_fill(env):
    fx = env
    _place_limit(fx)
    fx.price = 115.0
    fx.status = {"state": "partially_filled", "pending": True, "filled_size": 2.0,
                 "avg_price": 90.0, "size": 10.0, "source": "orders-pending"}
    fx.fills = (2.0, 90.0)
    bot._check_resting_orders(force=True)
    assert fx.cancels == ["L1"]
    pos = pt.get_open_positions()
    assert len(pos) == 1 and pos[0].size == 2.0 and pos[0].entry == 90.0


def test_cancel_refused_keeps_resting_for_recheck(env):
    fx = env
    _place_limit(fx)
    fx.price = 115.0
    fx.cancel_ok = False
    fx.status = {"state": "live", "pending": True, "filled_size": 0.0,
                 "avg_price": 0.0, "size": 10.0, "source": "orders-pending"}
    bot._check_resting_orders(force=True)
    assert len(bot._load_resting()) == 1


# -- partial fill rule -------------------------------------------------------

def test_partial_below_half_keeps_resting(env):
    fx = env
    _place_limit(fx)
    fx.status = {"state": "partially_filled", "pending": True, "filled_size": 4.0,
                 "avg_price": 90.0, "size": 10.0, "source": "orders-pending"}
    bot._check_resting_orders(force=True)
    assert fx.cancels == []
    assert pt.get_open_positions() == []
    rec = bot._load_resting()[0]
    assert rec["filled_size"] == 4.0


def test_partial_at_or_above_half_cancels_remainder_and_opens(env):
    fx = env
    _place_limit(fx)
    fx.status = {"state": "partially_filled", "pending": True, "filled_size": 5.0,
                 "avg_price": 89.9, "size": 10.0, "source": "orders-pending"}
    fx.fills = (6.0, 89.95)               # one more lot filled during the cancel
    bot._check_resting_orders(force=True)
    assert fx.cancels == ["L1"]
    assert bot._load_resting() == []
    pos = pt.get_open_positions()
    assert len(pos) == 1 and pos[0].size == 6.0 and pos[0].entry == 89.95


def test_gone_with_dust_fill_is_dropped(env):
    fx = env
    _place_limit(fx)
    fx.specs = {"contract_value": 1.0, "lot_size": 1.0, "min_size": 1.0}
    fx.status = {"state": "canceled", "pending": False, "filled_size": 0.5,
                 "avg_price": 90.0, "size": 10.0, "source": "orders-history"}
    bot._check_resting_orders(force=True)
    assert pt.get_open_positions() == []
    assert bot._load_resting() == []


# -- startup reconcile -------------------------------------------------------

def test_startup_reconcile_adopts_fill_and_does_not_flag_external(env, caplog, monkeypatch):
    fx = env
    _place_limit(fx)
    # Bot was down: the limit filled and the exchange now shows the position.
    fx.status = {"state": "filled", "pending": False, "filled_size": 10.0,
                 "avg_price": 89.5, "size": 10.0, "source": "orders-history"}
    fx.positions = [{"symbol": "XMR-USDT", "side": "buy", "size": 10.0,
                     "avg_price": 89.5, "liq_price": 0.0}]
    settled = []
    monkeypatch.setattr(bot, "_settle_position", lambda *a, **k: settled.append(a))
    with caplog.at_level("WARNING"):
        bot._reconcile_with_exchange(dry_run=True)
    assert settled == []
    assert bot._load_resting() == []
    pos = pt.get_open_positions()
    assert len(pos) == 1 and pos[0].entry == 89.5
    assert "NOT tracked locally" not in caplog.text


def test_reconcile_ignores_resting_orders_as_positions(env, caplog):
    fx = env
    _place_limit(fx)
    fx.status = {"state": "live", "pending": True, "filled_size": 0.0,
                 "avg_price": 0.0, "size": 10.0, "source": "orders-pending"}
    fx.positions = []
    with caplog.at_level("WARNING"):
        bot._reconcile_with_exchange(dry_run=True)
    assert len(bot._load_resting()) == 1
    assert pt.get_open_positions() == []
    assert "exchange_closed" not in caplog.text


def test_reconcile_partial_resting_position_not_flagged_external(env, caplog):
    fx = env
    _place_limit(fx)
    fx.status = {"state": "partially_filled", "pending": True, "filled_size": 3.0,
                 "avg_price": 90.0, "size": 10.0, "source": "orders-pending"}
    fx.positions = [{"symbol": "XMR-USDT", "side": "buy", "size": 3.0,
                     "avg_price": 90.0, "liq_price": 0.0}]
    with caplog.at_level("WARNING"):
        bot._reconcile_with_exchange(dry_run=True)
    assert "NOT tracked locally" not in caplog.text


# -- 4. market entry confirms the fill --------------------------------------

def test_market_entry_opens_at_confirmed_fill(env):
    fx = env
    sig = bot.sp.Signal(message_type=MessageType.NEW, symbol="XMR", side="buy",
                        analyst="Sveezy", raw_text="XMR long cmp", entry=100.0,
                        sl=90.0, tp=130.0, is_market_order=True, source="analyst")
    fx.status = {"state": "filled", "pending": False, "filled_size": 10.0,
                 "avg_price": 100.3, "size": 10.0, "source": "orders-history"}
    bot._process_new(sig, [], dry_run=True, leverage=20, analyst_key="Sveezy")
    assert fx.placed == [("market", "XMR", 10.0)]
    assert bot._load_resting() == []
    pos = pt.get_open_positions()
    assert len(pos) == 1 and pos[0].entry == 100.3 and pos[0].order_id == "M1"


def test_market_entry_falls_back_to_signal_price(env, caplog):
    fx = env
    sig = bot.sp.Signal(message_type=MessageType.NEW, symbol="XMR", side="buy",
                        analyst="Sveezy", raw_text="XMR long cmp", entry=100.0,
                        sl=90.0, tp=130.0, is_market_order=True, source="analyst")
    fx.status = {"state": "unknown", "pending": False, "filled_size": 0.0,
                 "avg_price": 0.0, "size": 0.0, "source": "none"}
    with caplog.at_level("WARNING"):
        bot._process_new(sig, [], dry_run=True, leverage=20, analyst_key="Sveezy")
    pos = pt.get_open_positions()
    assert len(pos) == 1 and pos[0].entry == 100.0
    assert "fill not confirmed" in caplog.text


def test_market_entry_uses_live_position_when_order_lookup_empty(env):
    fx = env
    sig = bot.sp.Signal(message_type=MessageType.NEW, symbol="XMR", side="sell",
                        analyst="Sveezy", raw_text="XMR short cmp", entry=100.0,
                        sl=110.0, tp=70.0, is_market_order=True, source="analyst")
    fx.status = None
    fx.positions = [{"symbol": "XMR-USDT", "side": "sell", "size": 10.0,
                     "avg_price": 99.8, "liq_price": 0.0}]
    bot._process_new(sig, [], dry_run=True, leverage=20, analyst_key="Sveezy")
    pos = pt.get_open_positions()
    assert len(pos) == 1 and pos[0].entry == 99.8


# -- PAPER path untouched ----------------------------------------------------

def test_paper_mode_does_not_use_exchange_resting(env, monkeypatch):
    fx = env
    monkeypatch.setenv("PAPER_MODE", "true")
    exec_mode.unfreeze()
    _place_limit(fx)                       # market 100 > limit 90: rests in-process
    assert fx.placed == []
    assert bot._load_resting() == []
    assert len(bot._load_pending()) == 1
    assert bot._load_pending()[0]["condition"] == "limit_fill"


# -- blofin_client endpoint wiring (fake transport, no network) --------------

import blofin_client as bf  # noqa: E402


class _Transport:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, method, path, auth, params=None, data=None, authenticate=False):
        self.calls.append((method, path, params, data))
        r = self.script.get((method, path))
        return r(params, data) if callable(r) else (r or {"code": "0", "data": []})


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(bf, "_client", SimpleNamespace(auth=object()))
    holder = {}

    def install(script):
        t = _Transport(script)
        monkeypatch.setattr(bf, "_send_request", t)
        holder["t"] = t
        return t
    return install


def test_get_order_status_pending_row(transport):
    t = transport({("GET", bf.ORDERS_PENDING_PATH): {"code": "0", "data": [
        {"orderId": "77", "state": "partially_filled", "filledSize": "3",
         "averagePrice": "90.1", "size": "10"}]}})
    st = bf.get_order_status("XMR", "77")
    assert st["pending"] is True and st["state"] == "partially_filled"
    assert st["filled_size"] == 3.0 and st["avg_price"] == 90.1
    assert t.calls[0][1] == "/api/v1/trade/orders-pending"
    assert t.calls[0][2]["instId"] == "XMR-USDT"


def test_get_order_status_history_then_fills(transport):
    t = transport({
        ("GET", bf.ORDERS_PENDING_PATH): {"code": "0", "data": []},
        ("GET", bf.ORDERS_HISTORY_PATH): {"code": "0", "data": [
            {"orderId": "77", "state": "filled", "filledSize": "10",
             "averagePrice": "89.9", "size": "10"}]},
    })
    st = bf.get_order_status("XMR", "77")
    assert st["pending"] is False and st["state"] == "filled" and st["avg_price"] == 89.9
    assert [c[1] for c in t.calls] == ["/api/v1/trade/orders-pending",
                                       "/api/v1/trade/orders-history"]

    t = transport({
        ("GET", bf.ORDERS_PENDING_PATH): {"code": "0", "data": []},
        ("GET", bf.ORDERS_HISTORY_PATH): {"code": "0", "data": []},
        ("GET", bf.FILLS_HISTORY_PATH): {"code": "0", "data": [
            {"orderId": "77", "fillSize": "4", "fillPrice": "90"},
            {"orderId": "77", "fillSize": "6", "fillPrice": "89"}]},
    })
    st = bf.get_order_status("XMR", "77")
    assert st["state"] == "filled" and st["filled_size"] == 10.0
    assert abs(st["avg_price"] - 89.4) < 1e-9
    assert t.calls[-1][1] == "/api/v1/trade/fills-history"
    assert t.calls[-1][2]["orderId"] == "77"


def test_get_order_status_unknown_and_transport_error(transport):
    transport({})
    st = bf.get_order_status("XMR", "77")
    assert st["state"] == "unknown" and st["pending"] is False

    def boom(params, data):
        raise RuntimeError("down")
    transport({("GET", bf.ORDERS_PENDING_PATH): boom})
    assert bf.get_order_status("XMR", "77") is None


def test_cancel_order_body_and_result(transport):
    t = transport({("POST", bf.CANCEL_ORDER_PATH): {"code": "0", "data": [
        {"orderId": "77", "code": "0"}]}})
    assert bf.cancel_order("XMR", "77") is True
    assert t.calls[0][1] == "/api/v1/trade/cancel-order"
    assert t.calls[0][3] == {"instId": "XMR-USDT", "orderId": "77"}
    transport({("POST", bf.CANCEL_ORDER_PATH): {"code": "0", "data": [
        {"orderId": "77", "code": "51400", "msg": "order not exist"}]}})
    assert bf.cancel_order("XMR", "77") is False
