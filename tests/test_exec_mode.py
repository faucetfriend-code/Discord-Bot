"""exec_mode resolution: PAPER / DEMO / LIVE from env, derived DRY_RUN, freeze."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import exec_mode as em  # noqa: E402

LIVE = "https://openapi.blofin.com"
DEMO = "https://demo-trading-openapi.blofin.com"


@pytest.fixture(autouse=True)
def _unfrozen():
    em.unfreeze()
    yield
    em.unfreeze()


# ---- resolution -----------------------------------------------------------

def test_paper_mode_wins_over_any_url():
    assert em.resolve_exec_mode({"PAPER_MODE": "true", "BLOFIN_BASE_URL": LIVE}) is em.ExecMode.PAPER
    assert em.resolve_exec_mode({"PAPER_MODE": "1", "BLOFIN_BASE_URL": DEMO}) is em.ExecMode.PAPER


def test_demo_url_is_demo():
    assert em.resolve_exec_mode({"BLOFIN_BASE_URL": DEMO}) is em.ExecMode.DEMO
    assert em.resolve_exec_mode({"BLOFIN_BASE_URL": DEMO + "/"}) is em.ExecMode.DEMO
    assert em.resolve_exec_mode({"PAPER_MODE": "false", "BLOFIN_BASE_URL": DEMO}) is em.ExecMode.DEMO


def test_unset_url_defaults_to_demo_like_blofin_client():
    assert em.resolve_exec_mode({}) is em.ExecMode.DEMO
    assert em.resolve_exec_mode({"BLOFIN_BASE_URL": ""}) is em.ExecMode.DEMO


def test_live_url_is_live():
    assert em.resolve_exec_mode({"BLOFIN_BASE_URL": LIVE}) is em.ExecMode.LIVE
    assert em.resolve_exec_mode({"PAPER_MODE": "false", "BLOFIN_BASE_URL": LIVE}) is em.ExecMode.LIVE


def test_unparseable_paper_mode_is_not_paper():
    assert em.resolve_exec_mode({"PAPER_MODE": "maybe", "BLOFIN_BASE_URL": DEMO}) is em.ExecMode.DEMO


# ---- helpers --------------------------------------------------------------

def test_calls_exchange_and_dry_run_per_mode():
    paper = {"PAPER_MODE": "true"}
    demo = {"BLOFIN_BASE_URL": DEMO}
    live = {"BLOFIN_BASE_URL": LIVE}
    assert not em.calls_exchange(paper)
    assert em.calls_exchange(demo)
    assert em.calls_exchange(live)
    assert em.is_dry_run(paper) and em.is_dry_run(demo) and not em.is_dry_run(live)
    assert em.is_live(live) and not em.is_live(demo)


def test_labels_name_the_endpoint_and_keys():
    assert em.banner({"PAPER_MODE": "true"}) == "EXEC MODE: PAPER (no exchange calls; simulated fills)"
    assert em.banner({"BLOFIN_BASE_URL": DEMO}) == (
        "EXEC MODE: DEMO (real orders on demo-trading-openapi.blofin.com, Demo-* keys)")
    assert em.banner({"BLOFIN_BASE_URL": LIVE}) == (
        "EXEC MODE: LIVE (real money on openapi.blofin.com, live keys)")
    assert em.dashboard_badge({"BLOFIN_BASE_URL": DEMO}) == "DEMO"


# ---- DRY_RUN is derived; disagreement warns ---------------------------------

def test_dry_run_unset_or_agreeing_gives_no_warning():
    assert em.dry_run_warning({"BLOFIN_BASE_URL": DEMO}) is None
    assert em.dry_run_warning({"BLOFIN_BASE_URL": DEMO, "DRY_RUN": "true"}) is None
    assert em.dry_run_warning({"BLOFIN_BASE_URL": LIVE, "DRY_RUN": "false"}) is None
    assert em.dry_run_warning({"BLOFIN_BASE_URL": DEMO, "DRY_RUN": ""}) is None


def test_dry_run_disagreement_warns_and_is_ignored():
    # DRY_RUN=false on the demo URL: the old flag would have armed "live"; now
    # it is ignored and the derived value (dry_run=True, DEMO) stands.
    msg = em.dry_run_warning({"BLOFIN_BASE_URL": DEMO, "DRY_RUN": "false"})
    assert msg is not None
    assert "DRY_RUN is derived from BLOFIN_BASE_URL now; ignoring DRY_RUN=false" in msg
    assert "mode=DEMO" in msg
    assert em.is_dry_run({"BLOFIN_BASE_URL": DEMO, "DRY_RUN": "false"}) is True

    # DRY_RUN=true on the live URL does NOT protect you any more - loud warning.
    msg = em.dry_run_warning({"BLOFIN_BASE_URL": LIVE, "DRY_RUN": "true"})
    assert msg is not None and "ignoring DRY_RUN=true" in msg and "mode=LIVE" in msg
    assert em.is_dry_run({"BLOFIN_BASE_URL": LIVE, "DRY_RUN": "true"}) is False


def test_dry_run_garbage_value_warns():
    assert em.dry_run_warning({"BLOFIN_BASE_URL": DEMO, "DRY_RUN": "banana"}) is not None


# ---- freeze: a later .env edit must not flip the trading path hot -----------

def test_freeze_pins_mode_until_unfreeze(monkeypatch):
    monkeypatch.delenv("PAPER_MODE", raising=False)
    monkeypatch.setenv("BLOFIN_BASE_URL", DEMO)
    assert em.freeze() is em.ExecMode.DEMO
    monkeypatch.setenv("BLOFIN_BASE_URL", LIVE)
    assert em.resolve_exec_mode() is em.ExecMode.LIVE  # pure view of the env
    assert em.mode() is em.ExecMode.DEMO  # effective mode stays pinned
    assert em.calls_exchange() and em.is_dry_run()
    em.unfreeze()
    assert em.mode() is em.ExecMode.LIVE


def test_explicit_env_bypasses_freeze(monkeypatch):
    monkeypatch.delenv("PAPER_MODE", raising=False)
    monkeypatch.setenv("BLOFIN_BASE_URL", DEMO)
    em.freeze()
    assert em.mode({"BLOFIN_BASE_URL": LIVE}) is em.ExecMode.LIVE
