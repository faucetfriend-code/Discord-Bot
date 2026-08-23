"""
Go-live preflight for the Discord Signal Bot.

Read-only checks against the BloFin endpoint the bot will trade on, to run
BEFORE you flip BLOFIN_BASE_URL to live. It verifies:
  - the API keys for that endpoint authenticate, and shows the balance
  - how many instruments live lists (vs demo's limited set)
  - account position mode matches HEDGE_MODE, margin mode is cross
  - which of your recent signal symbols are actually tradeable on live
  - config safety (execution mode, risk %, leverage band, keys present)
  - the external watchdog is alive and its status file is fresh
  - .env vs .env.example key hygiene (names only, never values)
  - shadow strategy log vs outcomes freshness
  - working-tree parser vs committed parser (corpus diff flip count)

It places NO orders and changes NO settings. Run it manually when ready:

    python preflight.py          # mode from .env (PAPER_MODE / BLOFIN_BASE_URL)
    python preflight.py --demo   # force the DEMO endpoint + Demo-* keys
    python preflight.py --live   # force the LIVE endpoint + live keys

The default validates whatever .env resolves to (see exec_mode.py) and says so.
--demo/--live never touch .env: BLOFIN_BASE_URL is overridden in this process only.
The full console output is also written to reports/preflight-<YYYYMMDD-HHMM>.txt
and the path is printed at the end.

Going live afterwards is two manual steps:
    1. BLOFIN_BASE_URL=https://openapi.blofin.com   (in .env)
    2. restart the bot
DRY_RUN is derived from the URL now (everything not live is tagged dry_run=1).
The bot auto-uses your LIVE keys on the live endpoint (Demo-* keys are ignored).
"""

import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

load_dotenv()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import exec_mode  # noqa: E402
import net_prefs  # noqa: E402  - pin BloFin HTTPS to IPv4 before any client exists

net_prefs.force_ipv4()

LIVE_URL = "https://openapi.blofin.com"
DEMO_URL = "https://demo-trading-openapi.blofin.com"
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "bot.db"
REPORTS_DIR = ROOT / "reports"
WATCHDOG_STATUS_PATH = ROOT / "watchdog_status.json"
WATCHDOG_INTERVAL_DEFAULT = 120  # mirrors watchdog.DEFAULTS["WATCHDOG_INTERVAL_SEC"]

PASS, WARN, FAIL = "[PASS]", "[WARN]", "[FAIL]"
_PLACEHOLDERS = {"", "your_blofin_api_key", "your_live_blofin_api_key",
                 "your_blofin_secret_key", "your_blofin_passphrase"}

# Collected across blocks so the verdict can list them.
_FAILS: list[str] = []
_WARNS: list[str] = []


def _fail(msg: str) -> None:
    _FAILS.append(msg)
    print(f"  {FAIL} {msg}")


def _warn(msg: str) -> None:
    _WARNS.append(msg)
    print(f"  {WARN} {msg}")


def _pass(msg: str) -> None:
    print(f"  {PASS} {msg}")


class _Tee:
    """Mirror every write to stdout into a report file (text only)."""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, data):
        self._stream.write(data)
        self._fh.write(data)
        return len(data)

    def flush(self):
        self._stream.flush()
        self._fh.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _recent_symbols(limit_signals: int = 600) -> list[str]:
    """
    Distinct ticker symbols the bot actually TRIED to trade recently - i.e. signals
    whose outcome was a real entry attempt (dry-run, executed, sized, rejected,
    not-listed, or a resolved win/loss). Excludes parser noise (blocklist words,
    analyst names from old mis-parses, empty bases).
    """
    try:
        from signal_parser import _SYMBOL_BLOCKLIST
    except Exception:
        _SYMBOL_BLOCKLIST = set()
    trade_outcomes = (
        "dry_run", "executed", "size_too_small", "symbol_not_listed",
        "rejected", "balance_unavailable", "win:", "loss:", "scaled_tp",
        "incomplete_signal", "levels_implausible",
    )
    try:
        con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT symbol, outcome FROM signals ORDER BY id DESC LIMIT ?",
            (limit_signals,),
        ).fetchall()
        con.close()
    except Exception as e:
        print(f"  (could not read recent symbols: {e})")
        return []

    out = set()
    for sym, outcome in rows:
        if not sym or sym == "UNKNOWN-USDT":
            continue
        outcome = outcome or ""
        if not any(outcome.startswith(t) or t in outcome for t in trade_outcomes):
            continue
        base = sym.replace("-USDT", "").strip()
        if not base or base.upper() in _SYMBOL_BLOCKLIST:
            continue
        out.add(sym)
    return sorted(out)


_AUTH_ERROR_HINTS = {
    "152401": "key does not exist (wrong key, or a DEMO key on the live endpoint)",
    "152402": "API key expired - regenerate on blofin.com",
    "152406": "IP not whitelisted",
    "152407": "insufficient API key permissions - enable Read/Trade",
    "152408": "invalid passphrase",
    "401": "invalid signature/passphrase (HTTP 401)",
}


def _describe_auth_error(err: dict | None) -> str:
    """Turn blofin_client.last_balance_error into a one-line human diagnosis."""
    if not err:
        return "Auth - live keys rejected (no error detail captured)"
    code, msg = str(err.get("code", "")), str(err.get("msg", ""))
    if code == "152406":
        m = re.search(r"Request IP:\s*([0-9a-fA-F:.]+)", msg)
        seen = m.group(1) if m else "(not reported)"
        return (f"Auth - IP not whitelisted; request IP seen by BloFin: {seen} "
                f"(add it to the key's whitelist, or keep FORCE_IPV4=true so it is IPv4)")
    if code == "exception" and "401" in msg:
        return f"Auth - {_AUTH_ERROR_HINTS['401']}: {msg}"
    hint = _AUTH_ERROR_HINTS.get(code)
    if hint:
        return f"Auth - {hint} [code {code}: {msg}]"
    return f"Auth - live keys rejected [code {code}: {msg}]"


def _egress_ip(url: str, timeout: float = 6.0) -> str | None:
    """Return the public IP that `url` (ipify) sees for this process, or None."""
    try:
        import requests
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text.strip()
    except Exception:
        return None


def check_egress_ip() -> None:
    """Report the public address BloFin will see, so it can be compared with the whitelist."""
    print("\n[Egress IP]")
    forced = net_prefs.force_ipv4_enabled()
    v4 = _egress_ip("https://api4.ipify.org")
    if v4:
        _pass(f"IPv4 (api4.ipify.org) ... {v4}   <- this must be on the BloFin key whitelist")
    else:
        _warn("IPv4 (api4.ipify.org) ... unreachable - could not determine public IPv4")
    if forced:
        _pass("FORCE_IPV4=true - outbound HTTPS pinned to IPv4, IPv6 egress not used")
    else:
        v6 = _egress_ip("https://api6.ipify.org")
        if v6:
            _warn(f"FORCE_IPV4=false - IPv6 egress {v6} may be preferred and is NOT whitelisted")
        else:
            _pass("FORCE_IPV4=false but no IPv6 egress detected (api6.ipify.org unreachable)")


def check_position_mode(bf) -> None:
    """Read-only: the account's position mode must match HEDGE_MODE, and the
    margin mode should be cross (bot calls set_leverage(..., margin_mode='cross'))."""
    print("\n[Position mode]")
    hedge = _truthy(os.getenv("HEDGE_MODE"), False)  # mirrors bot.py default
    pos_mode = bf.get_position_mode()
    if pos_mode is None:
        _warn("positionMode ....... could not read (get_position_mode returned None)")
    elif hedge and pos_mode != "long_short_mode":
        _fail("HEDGE_MODE=true but BloFin account is in net_mode - enable "
              "Long/Short (hedge) position mode on blofin.com")
    elif not hedge and pos_mode != "net_mode":
        _fail(f"HEDGE_MODE=false but BloFin account is in {pos_mode} - switch to "
              f"One-way (net) position mode on blofin.com, or set HEDGE_MODE=true")
    else:
        _pass(f"positionMode ....... {pos_mode} (matches HEDGE_MODE={str(hedge).lower()})")

    margin_mode = bf.get_margin_mode()
    if margin_mode is None:
        _warn("marginMode ......... could not read (get_margin_mode returned None)")
    elif margin_mode == "cross":
        _pass("marginMode ......... cross (bot sets leverage with margin_mode='cross')")
    else:
        _warn(f"marginMode ......... {margin_mode} - bot sets leverage with "
              f"margin_mode='cross'; verify on blofin.com")


def check_broker_id(bf) -> None:
    """Show the brokerId the bot will put on live orders (Broker/MCP-type
    keys reject orders without one: code 152012)."""
    bid = bf._broker_id()
    print(f"  [INFO] brokerId ......... {bid if bid else 'none'}")
    if TARGET_DEMO or _validated_mode() is not exec_mode.ExecMode.LIVE:
        return  # only LIVE orders carry a brokerId
    if bid is None and bf._is_live_url(bf._active_base_url()):
        _warn("brokerId is none on the live endpoint - Broker/MCP keys need a "
              "brokerId; set BLOFIN_BROKER_ID or leave unset for the default")


# Endpoint under test. Default (None) follows .env via exec_mode; --demo / --live
# force one endpoint for this process only.
TARGET_DEMO = False
TARGET_FORCED: str | None = None  # "demo" | "live" | None


def _validated_mode() -> exec_mode.ExecMode:
    """The execution mode this run validates: the forced one, else .env's."""
    if TARGET_FORCED == "demo":
        return exec_mode.ExecMode.DEMO
    if TARGET_FORCED == "live":
        return exec_mode.ExecMode.LIVE
    return exec_mode.resolve_exec_mode()


def _target_url() -> str:
    return DEMO_URL if TARGET_DEMO else LIVE_URL


def _switch_to_live():
    """Point blofin_client at the target endpoint (live by default, demo with
    --demo) with the matching key set. Process-local env only; .env untouched."""
    import blofin_client as bf
    os.environ["BLOFIN_BASE_URL"] = _target_url()
    bf._client = None              # force re-init against live
    bf._instruments_cache = None   # force re-fetch live instruments
    return bf


# ---------------------------------------------------------------------------
# Block: Watchdog
# ---------------------------------------------------------------------------

def _watchdog_process_running() -> bool | None:
    """True/False if detectable, None if neither psutil nor tasklist worked."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        # Match only a python interpreter whose script argument is watchdog.py;
        # a loose substring match also hits shells whose command line merely
        # mentions the file (e.g. "py_compile preflight.py watchdog.py").
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                cmd = proc.info.get("cmdline") or []
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if not name.startswith("python"):
                continue
            if any(os.path.basename(p or "").lower() == "watchdog.py" for p in cmd[1:]):
                return True
        return False
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe' or name='pythonw.exe'",
             "get", "CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=15,
        ).stdout or ""
        return "watchdog.py" in out.lower()
    except (OSError, subprocess.SubprocessError):
        return None


def check_watchdog() -> None:
    print("\n[Watchdog]")
    try:
        interval = int(os.getenv("WATCHDOG_INTERVAL_SEC", "") or WATCHDOG_INTERVAL_DEFAULT)
    except ValueError:
        interval = WATCHDOG_INTERVAL_DEFAULT
    max_age = timedelta(seconds=3 * interval)

    if not WATCHDOG_STATUS_PATH.exists():
        _fail("watchdog_status.json missing - watchdog has never run here")
    else:
        try:
            with open(WATCHDOG_STATUS_PATH, "r", encoding="utf-8") as f:
                gen = json.load(f).get("generated_at", "")
            gen_dt = datetime.strptime(gen, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - gen_dt
            age_txt = f"{age.total_seconds() / 60:.1f} min"
            if age <= max_age:
                _pass(f"watchdog_status.json age ..... {age_txt} (limit 3x{interval}s)")
            else:
                _fail(f"watchdog_status.json stale: {age_txt} old (limit 3x{interval}s)")
        except (OSError, ValueError, json.JSONDecodeError) as e:
            _fail(f"watchdog_status.json unreadable: {type(e).__name__}: {e}")

    running = _watchdog_process_running()
    if running is True:
        _pass("watchdog.py process ......... running")
    elif running is False:
        _warn("watchdog.py process not running - start run_watchdog.bat")
    else:
        _warn("could not determine whether watchdog.py is running")


# ---------------------------------------------------------------------------
# Block: Config hygiene
# ---------------------------------------------------------------------------

def _src_key(source: str) -> str:
    """Mirror bot._src_key: 'Sveezy Alerts' -> 'SVEEZY_ALERTS'."""
    return re.sub(r"[^A-Za-z0-9]+", "_", source or "").strip("_").upper()


def _truthy(val: str | None, default: bool) -> bool:
    if val is None or val == "":
        return default
    return val.strip().lower() == "true"


def check_config_hygiene() -> None:
    print("\n[Config hygiene]")
    env_path = ROOT / ".env"
    ex_path = ROOT / ".env.example"
    if not env_path.exists() or not ex_path.exists():
        _warn(".env or .env.example missing - key comparison skipped")
        env_keys: set[str] = set()
    else:
        env_keys = set(dotenv_values(str(env_path)).keys())
        ex_keys = set(dotenv_values(str(ex_path)).keys())
        missing = sorted(ex_keys - env_keys)
        extra = sorted(env_keys - ex_keys)
        if not missing and not extra:
            _pass(".env and .env.example key sets match")
        for k in missing:
            _warn(f"in .env.example but not .env: {k}")
        for k in extra:
            _warn(f"in .env but not .env.example: {k}")

    # Hedge-mode exposure cap: every enabled source needs a >0 cap from either
    # <SRC>__MAX_OPEN or the global MAX_OPEN_PER_SOURCE (bot.py default 0 = unlimited).
    hedge = _truthy(os.getenv("HEDGE_MODE"), False)
    if hedge:
        sources = [_src_key(s) for s in os.getenv("ANALYST_WHITELIST", "").split(",")
                   if s.strip()]
        if _truthy(os.getenv("RSI_EXTREME_ENABLED"), True):
            sources.append("RSI_EXTREME")
        if _truthy(os.getenv("ORACLEALGO_ENABLED"), True):
            sources.append("ORACLEALGO")
        global_cap = os.getenv("MAX_OPEN_PER_SOURCE")
        uncapped = []
        for src in sources:
            if not _truthy(os.getenv(f"{src}__ENABLED"), True):
                continue
            raw = os.getenv(f"{src}__MAX_OPEN")
            if raw is None or raw == "":
                raw = global_cap
            try:
                cap = int(raw) if raw not in (None, "") else 0
            except ValueError:
                cap = 0
            if cap <= 0:
                uncapped.append(src)
        if uncapped:
            _fail(f"HEDGE_MODE=true with no per-source cap (MAX_OPEN_PER_SOURCE "
                  f"unset/0 and no <SRC>__MAX_OPEN) for: {', '.join(uncapped)} "
                  f"- exposure uncapped in hedge mode")
        else:
            _pass(f"HEDGE_MODE=true: all {len(sources)} enabled sources capped")
    else:
        _pass("HEDGE_MODE=false: MAX_OPEN_POSITIONS global cap applies")

    if os.getenv("RUNNER_MAX_AGE_HOURS") in (None, ""):
        _warn("RUNNER_MAX_AGE_HOURS unset (bot default 120h applies)")
    else:
        _pass(f"RUNNER_MAX_AGE_HOURS ....... {os.getenv('RUNNER_MAX_AGE_HOURS')}h")


# ---------------------------------------------------------------------------
# Block: Shadow data
# ---------------------------------------------------------------------------

def _csv_stats(path: Path) -> tuple[int, str | None]:
    """Row count (excluding header) and the max 'ts' value."""
    n = 0
    last = None
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            n += 1
            ts = (row.get("ts") or "").strip()
            if ts and (last is None or ts > last):
                last = ts
    return n, last


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_shadow_data() -> None:
    print("\n[Shadow data]")
    stats = {}
    for name in ("strategy_shadow.csv", "shadow_outcomes.csv"):
        p = ROOT / name
        if not p.exists():
            _warn(f"{name} missing")
            continue
        try:
            n, last = _csv_stats(p)
        except (OSError, csv.Error) as e:
            _warn(f"{name} unreadable: {type(e).__name__}: {e}")
            continue
        stats[name] = (n, last)
        print(f"  {name:22s} rows={n:<6d} last ts={last or 'n/a'}")

    shadow_last = _parse_ts(stats.get("strategy_shadow.csv", (0, None))[1])
    out_last = _parse_ts(stats.get("shadow_outcomes.csv", (0, None))[1])
    if shadow_last and out_last:
        lag = shadow_last - out_last
        if lag > timedelta(hours=24):
            _warn(f"shadow_outcomes.csv lags strategy_shadow.csv by "
                  f"{lag.total_seconds() / 3600:.1f}h (>24h)")
        else:
            _pass(f"outcomes lag ............... {max(lag, timedelta(0)).total_seconds() / 3600:.1f}h")

    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", "strategy_shadow.csv", "shadow_outcomes.csv"],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        ).stdout
        dirty = [line[3:].strip() for line in out.splitlines() if line.strip()]
        if dirty:
            _warn(f"uncommitted CSV changes (git backstop stale): {', '.join(dirty)}")
        else:
            _pass("shadow CSVs committed (git backstop current)")
    except (OSError, subprocess.SubprocessError) as e:
        _warn(f"git status unavailable: {type(e).__name__}")


# ---------------------------------------------------------------------------
# Block: Parser
# ---------------------------------------------------------------------------

def check_parser() -> None:
    print("\n[Parser]")
    tool = ROOT / "tools" / "parser_corpus_diff.py"
    if not tool.exists():
        _warn("tools/parser_corpus_diff.py missing - corpus diff skipped")
        return
    try:
        res = subprocess.run(
            [sys.executable, str(tool), "--ref", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except (OSError, subprocess.SubprocessError) as e:
        _warn(f"corpus diff could not run: {type(e).__name__}: {e}")
        return
    m = re.search(r"^flips:\s*(\d+)", res.stdout or "", re.M)
    rows = re.search(r"^rows replayed:\s*(\d+)", res.stdout or "", re.M)
    if m is None:
        tail = ((res.stderr or res.stdout or "").strip().splitlines() or ["no output"])[-1]
        _warn(f"corpus diff gave no flip count (rc={res.returncode}): {tail[:160]}")
        return
    flips = int(m.group(1))
    n_rows = rows.group(1) if rows else "?"
    if flips == 0:
        _pass(f"parser corpus diff vs HEAD .. 0 flips over {n_rows} rows")
    else:
        _warn(f"parser corpus diff vs HEAD .. {flips} flips over {n_rows} rows "
              f"(working-tree signal_parser.py differs from HEAD; expected until committed)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _extra_blocks() -> None:
    """Blocks that do not need live credentials - run on every path."""
    check_watchdog()
    check_config_hygiene()
    check_shadow_data()
    check_parser()


def main():
    vmode = _validated_mode()
    print("=" * 64)
    if TARGET_DEMO:
        print("  DEMO PREFLIGHT  -  read-only checks against DEMO BloFin")
    else:
        print("  GO-LIVE PREFLIGHT  -  read-only checks against LIVE BloFin")
    src = "forced by --demo/--live" if TARGET_FORCED else "resolved from .env"
    print(f"  Validating execution mode: {vmode.value} ({src})")
    print("=" * 64)

    go = True

    # ---- Config safety -------------------------------------------------
    print("\n[Config]")
    env_mode = exec_mode.resolve_exec_mode()
    env_note = {
        exec_mode.ExecMode.PAPER: "PAPER_MODE=true -> no exchange orders; "
                                  "flip PAPER_MODE=false for DEMO",
        exec_mode.ExecMode.DEMO: "real orders on the demo account; set "
                                 "BLOFIN_BASE_URL to live (after this passes) for LIVE",
        exec_mode.ExecMode.LIVE: "LIVE execution armed",
    }[env_mode]
    print(f"  {PASS if env_mode is exec_mode.ExecMode.LIVE else WARN} "
          f"EXEC MODE (.env) = {env_mode.value}   -> {env_note}")
    if env_mode is not exec_mode.ExecMode.LIVE:
        _WARNS.append(f".env resolves to {env_mode.value} (not live)")
    legacy = exec_mode.dry_run_warning()
    if legacy:
        _warn(legacy)
    elif os.getenv("DRY_RUN") is not None:
        print(f"  [INFO] DRY_RUN={os.getenv('DRY_RUN')} in .env is ignored "
              "(derived from BLOFIN_BASE_URL now) - safe to remove")

    if TARGET_DEMO:
        api = os.getenv("Demo-BloFinAPI", "")
        sec = os.getenv("Demo-Blofin_secret_key", "")
        pas = os.getenv("Demo-Passphrase", "")
        key_names = "Demo-BloFinAPI/Demo-Blofin_secret_key/Demo-Passphrase"
        label = "Demo"
    else:
        api = os.getenv("BloFinAPI", "")
        sec = os.getenv("Blofin_secret_key", "")
        pas = os.getenv("Passphrase", "")
        key_names = "BloFinAPI/secret/Passphrase"
        label = "Live"
    keys_ok = all(v and v not in _PLACEHOLDERS for v in (api, sec, pas))
    if keys_ok:
        _pass(f"{label} API credentials present")
    else:
        _fail(f"{label} API credentials MISSING/placeholder - fill {key_names}")
    go = go and keys_ok

    print(f"  {PASS} Risk per trade .... {float(os.getenv('RISK_PCT','0.01'))*100:.1f}%")
    print(f"  {PASS} Leverage band ..... {os.getenv('LEVERAGE_MIN','50')}-{os.getenv('LEVERAGE_MAX','125')}x "
          f"(start {os.getenv('LEVERAGE_START','75')}x)")
    print(f"  {PASS} Max positions ..... {os.getenv('MAX_OPEN_POSITIONS','3')}")
    print(f"  {PASS} Strategies ........ RSI={os.getenv('RSI_EXTREME_ENABLED','true')}, "
          f"OracleAlgo={os.getenv('ORACLEALGO_ENABLED','true')}")

    if not keys_ok:
        print(f"\nCannot test {label.lower()} auth without credentials. Fix .env and re-run.")
        _extra_blocks()
        _verdict(False)
        return

    # ---- Live auth + balance ------------------------------------------
    print(f"\n[{label} BloFin]  {_target_url()}")
    bf = _switch_to_live()
    balance = bf.get_balance()
    if balance is None:
        _fail(_describe_auth_error(getattr(bf, "last_balance_error", None)))
        check_egress_ip()
        _extra_blocks()
        _verdict(False)
        return
    _pass(f"Auth - {label.lower()} keys accepted")
    check_broker_id(bf)
    if balance > 0:
        _pass(f"Balance ........... ${balance:,.2f}")
    else:
        _fail(f"Balance ........... $0.00 - fund the {label.lower()} futures account before trading")
        go = False

    try:
        n_live = len(bf._load_instruments())
        _pass(f"Instruments listed  {n_live} on {label.lower()}")
    except Exception as e:
        _warn(f"Could not load live instruments: {e}")
    check_position_mode(bf)
    check_egress_ip()

    # ---- Recent signal symbols tradeable on live? ----------------------
    print("\n[Recent signal symbols - tradeable on live?]")
    syms = _recent_symbols()
    if not syms:
        print("  (no recent symbols in bot.db yet - run the bot in dry-run first)")
    else:
        tradeable = 0
        for s in syms:
            listed = bf.get_contract_specs(s) is not None
            tradeable += listed
            mark = "yes" if listed else "NO "
            print(f"    {mark}  {s}")
        print(f"\n  Tradeable on live: {tradeable}/{len(syms)} recent symbols"
              + ("  - the rest have no BloFin market (they'll keep skipping, which is fine)"
                 if tradeable < len(syms) else ""))

    _extra_blocks()
    _verdict(go)


def _verdict(go: bool):
    go = go and not _FAILS
    print("\n" + "=" * 64)
    if go:
        print(f"  VERDICT: GO   {_validated_mode().value} auth + funded, no FAILs.")
        if _validated_mode() is exec_mode.ExecMode.LIVE:
            print("    To trade live: 1) set BLOFIN_BASE_URL=https://openapi.blofin.com")
            print("                   2) restart the bot  (DRY_RUN is derived - leave it)")
        else:
            print("    Validated the DEMO endpoint. Re-run with --live before going live.")
    else:
        print("  VERDICT: NO-GO   Resolve the [FAIL] items below, then re-run.")
    if _FAILS:
        print(f"  FAILs ({len(_FAILS)}):")
        for m in _FAILS:
            print(f"    - {m}")
    if _WARNS:
        print(f"  WARNs ({len(_WARNS)}):")
        for m in _WARNS:
            print(f"    - {m}")
    print("=" * 64)


def _run_with_report() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    report_path = REPORTS_DIR / f"preflight-{stamp}.txt"
    real_stdout = sys.stdout
    with open(report_path, "w", encoding="utf-8") as fh:
        sys.stdout = _Tee(real_stdout, fh)
        try:
            main()
        finally:
            sys.stdout.flush()
            sys.stdout = real_stdout
    print(f"\nReport written to: {report_path}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--demo" in args:
        TARGET_FORCED = "demo"
    elif "--live" in args:
        TARGET_FORCED = "live"
    # Default: validate the endpoint .env resolves to (PAPER rides the demo URL).
    TARGET_DEMO = _validated_mode() is not exec_mode.ExecMode.LIVE
    _run_with_report()
