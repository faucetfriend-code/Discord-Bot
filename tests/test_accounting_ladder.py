"""won-derivation helper and leverage-ladder replay (pure functions + in-memory
sqlite through tools/rebuild_analyst_stats.py)."""

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import position_tracker as pt  # noqa: E402
import rebuild_analyst_stats as rb  # noqa: E402


# -- derive_won ---------------------------------------------------------------

@pytest.mark.parametrize("net,expected", [
    (8.019669, True),     # trade 63: time_stop, exit above entry, net positive
    (-1.100818, False),   # trade 103: trail_stop at BE, fees+funding made it a loss
    (0.0, False),         # flat after fees is not a win
    (-0.0, False),
    (1e-9, True),
    ("2.5", True),        # tolerates numeric strings straight from sqlite
])
def test_derive_won_follows_net_pnl(net, expected):
    assert pt.derive_won(net) is expected


# -- step_leverage / replay_ladder -------------------------------------------

def test_step_leverage_clamps_both_ends():
    assert pt.step_leverage(75, True, 10, 50, 125) == 85
    assert pt.step_leverage(75, False, 10, 50, 125) == 65
    assert pt.step_leverage(120, True, 10, 50, 125) == 125
    assert pt.step_leverage(55, False, 10, 50, 125) == 50


def test_replay_matches_record_outcome_rule():
    # W W L W L L L L L -> 85 95 85 95 85 75 65 55 50(clamped from 45)
    out = pt.replay_ladder([1, 1, 0, 1, 0, 0, 0, 0, 0], 75, 50, 125, 10)
    assert out == {"leverage": 50, "wins": 3, "losses": 6}


def test_replay_is_path_dependent_not_additive():
    # Same W/L tally, different order, different leverage because of the clamp.
    a = pt.replay_ladder([0] * 4 + [1] * 4, 75, 50, 125, 10)   # hits floor first
    b = pt.replay_ladder([1] * 4 + [0] * 4, 75, 50, 125, 10)   # never clamps
    assert (a["wins"], a["losses"]) == (b["wins"], b["losses"]) == (4, 4)
    assert a["leverage"] == 90 and b["leverage"] == 75


def test_replay_empty_sequence_returns_start():
    assert pt.replay_ladder([], 75, 50, 125, 10) == {
        "leverage": 75, "wins": 0, "losses": 0}


# -- rebuild tool against an in-memory db -------------------------------------

def _seed_db(path=":memory:"):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, analyst TEXT, "
                "closed_at TEXT, won INTEGER, net_pnl REAL)")
    con.execute("CREATE TABLE analyst_stats (analyst TEXT PRIMARY KEY, leverage INTEGER, "
                "wins INTEGER, losses INTEGER, updated_at TEXT, realized_pnl REAL)")
    trades = [
        ("Soul", "2026-07-01", 1, 5.0),
        ("Soul", "2026-07-02", 0, -3.0),
        ("Soul", "2026-07-03", 0, 2.0),    # stale flag: says loss, net is a win
        ("RSI", "2026-07-01", 1, 1.0),
    ]
    con.executemany("INSERT INTO trades (analyst, closed_at, won, net_pnl) "
                    "VALUES (?, ?, ?, ?)", trades)
    con.executemany("INSERT INTO analyst_stats VALUES (?, ?, ?, ?, '', ?)", [
        ("Soul", 60, 20, 35, -10.0),      # drifted
        ("RSI", 85, 1, 0, 1.0),           # in sync
        ("Grasady", 65, 0, 1, -10.0),     # no trades at all
    ])
    return con


PARAMS = {"LEVERAGE_START": 75, "LEVERAGE_MIN": 50,
          "LEVERAGE_MAX": 125, "LEVERAGE_STEP": 10}


def test_rebuild_from_stored_won():
    con = _seed_db()
    target = rb.rebuild(rb.read_current(con), rb.read_outcomes(con, False), PARAMS)
    assert target["Soul"] == {"leverage": 65, "wins": 1, "losses": 2,
                              "realized_pnl": 4.0}
    assert target["RSI"] == {"leverage": 85, "wins": 1, "losses": 0,
                             "realized_pnl": 1.0}
    # analyst with stats but no trades resets to the start of the band
    assert target["Grasady"] == {"leverage": 75, "wins": 0, "losses": 0,
                                 "realized_pnl": 0.0}


def test_rebuild_won_from_pnl_repairs_stale_flag():
    con = _seed_db()
    target = rb.rebuild(rb.read_current(con), rb.read_outcomes(con, True), PARAMS)
    assert target["Soul"] == {"leverage": 85, "wins": 2, "losses": 1,
                              "realized_pnl": 4.0}


def test_print_table_counts_drift(capsys):
    con = _seed_db()
    current = rb.read_current(con)
    target = rb.rebuild(current, rb.read_outcomes(con, False), PARAMS)
    drift = rb.print_table(current, target)
    assert drift == 2  # Soul and Grasady; RSI is in sync
    out = capsys.readouterr().out
    assert "DRIFT" in out and "ok" in out


def test_apply_writes_one_transaction(tmp_path):
    db = tmp_path / "bot.db"
    con = _seed_db(db)
    con.commit()
    target = rb.rebuild(rb.read_current(con), rb.read_outcomes(con, False), PARAMS)
    con.close()
    backup = rb.apply(db, target)
    assert backup.exists() and "statsrebuild" in backup.name
    con = sqlite3.connect(db)
    rows = dict((r[0], r[1:]) for r in con.execute(
        "SELECT analyst, leverage, wins, losses FROM analyst_stats"))
    con.close()
    assert rows["Soul"] == (65, 1, 2)
    assert rows["Grasady"] == (75, 0, 0)
    # the backup still holds the pre-rebuild row
    bak = sqlite3.connect(backup)
    old = bak.execute(
        "SELECT leverage, wins, losses FROM analyst_stats WHERE analyst='Soul'").fetchone()
    bak.close()
    assert old == (60, 20, 35)


def test_load_leverage_params_reads_only_ladder_keys(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LEVERAGE_START=80\nLEVERAGE_STEP=5\nBLOFIN_API_KEY=secret\n")
    params = rb.load_leverage_params(env)
    assert params == {"LEVERAGE_START": 80, "LEVERAGE_MIN": 50,
                      "LEVERAGE_MAX": 125, "LEVERAGE_STEP": 5}
    assert "BLOFIN_API_KEY" not in os.environ  # dotenv_values never exports
