"""Live-epoch filter helpers (pure functions + in-memory sqlite)."""

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import live_epoch  # noqa: E402
import set_live_epoch  # noqa: E402

EPOCH = "2026-08-23T11:00:36-05:00"


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, analyst TEXT,
            closed_at TEXT, net_pnl REAL, dry_run INTEGER DEFAULT 1);
        CREATE TABLE positions (id INTEGER PRIMARY KEY, symbol TEXT, analyst TEXT,
            order_id TEXT, status TEXT);
        INSERT INTO trades VALUES
            (1, 'ETH-USDT', 'A', '2026-08-20T10:00:00-05:00', 5.0, 1),
            (2, 'BTC-USDT', 'Soul Alerts', '2026-08-23T10:59:13-05:00', 3.4, 0),
            (3, 'BTC-USDT', 'B', '2026-08-23T12:00:00-05:00', -2.0, 0),
            (4, 'SOL-USDT', 'C', '2026-08-23T13:00:00-05:00', 1.0, 1);
        INSERT INTO positions VALUES
            (1, 'BTC-USDT', 'Soul Alerts', 'DRYRUN-BTC-USDT-1', 'closed'),
            (2, 'BTC-USDT', 'B', '1234567890', 'closed');
        """
    )
    return c


def test_is_live_trade_requires_dry_run_zero_and_after_epoch():
    ep = live_epoch.parse_ts(EPOCH)
    assert live_epoch.is_live_trade(0, "2026-08-23T12:00:00-05:00", ep)
    assert not live_epoch.is_live_trade(0, "2026-08-23T10:59:13-05:00", ep)
    assert not live_epoch.is_live_trade(1, "2026-08-23T12:00:00-05:00", ep)
    assert not live_epoch.is_live_trade(0, None, ep)
    # exact epoch instant counts as live; no epoch -> dry_run alone decides
    assert live_epoch.is_live_trade(0, EPOCH, ep)
    assert live_epoch.is_live_trade(0, "2020-01-01T00:00:00+00:00", None)


def test_split_trades_partitions_rows():
    ep = live_epoch.parse_ts(EPOCH)
    rows = [
        {"id": 1, "dry_run": 1, "closed_at": "2026-08-20T10:00:00-05:00"},
        {"id": 2, "dry_run": 0, "closed_at": "2026-08-23T10:59:13-05:00"},
        {"id": 3, "dry_run": 0, "closed_at": "2026-08-23T12:00:00-05:00"},
    ]
    live = live_epoch.split_trades(rows, ep, "live")
    paper = live_epoch.split_trades(rows, ep, "paper")
    assert [r["id"] for r in live] == [3]
    assert [r["id"] for r in paper] == [1, 2]


def test_position_and_view_helpers():
    assert not live_epoch.is_live_position("DRYRUN-BTC-USDT-1")
    assert live_epoch.is_live_position("1234567890")
    assert live_epoch.is_live_position(None)
    assert live_epoch.normalize_view("paper") == "paper"
    assert live_epoch.normalize_view("PAPER ") == "paper"
    assert live_epoch.normalize_view("live") == "live"
    assert live_epoch.normalize_view("garbage") == "live"
    assert live_epoch.normalize_view(None) == "live"


def test_meta_resolution_env_first(con, monkeypatch):
    monkeypatch.delenv("LIVE_EPOCH_START", raising=False)
    monkeypatch.delenv("LIVE_START_BALANCE", raising=False)
    assert live_epoch.get_epoch(con) is None  # no meta table yet
    assert live_epoch.get_start_balance(con) is None
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO meta VALUES ('live_epoch_start', ?)", (EPOCH,))
    con.execute("INSERT INTO meta VALUES ('live_start_balance', '1494.08')")
    assert live_epoch.get_epoch(con) == live_epoch.parse_ts(EPOCH)
    assert live_epoch.get_start_balance(con) == 1494.08
    monkeypatch.setenv("LIVE_EPOCH_START", "2026-01-01T00:00:00+00:00")
    assert live_epoch.get_epoch(con).year == 2026
    assert live_epoch.get_epoch(con).month == 1


def test_plan_relabel_only_touches_dryrun_positions(con):
    ok, refused = set_live_epoch.plan_relabel(con, [1, 2, 3, 99])
    assert ok == [2]
    assert any("already dry_run=1" in r for r in refused)  # trade 1
    assert any("no closed DRYRUN-" in r for r in refused)  # trade 3 (real order)
    assert any("not found" in r for r in refused)  # trade 99
