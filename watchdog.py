"""
watchdog.py - external liveness monitor for the Discord Signal Bot.

bot.py has its own internal _alert() (bot.py ~line 1869) that logs and,
if ALERT_WEBHOOK_URL is set, posts to a Discord webhook. It only runs while
the bot process is alive: if the bot crashes, hangs, is killed, or Chrome
drags it down, nothing alerts at all.

watchdog.py is a SEPARATE process that watches from the outside. It has no
webhook URL to rely on, so its primary alert path is a Windows toast (with a
MessageBoxW fallback) plus a local log and a machine-readable status file.
It never writes to bot.db - all DB access is read-only.

Checks (per monitored instance directory):
  1. process_alive       - is a "python bot.py" process mapped to this dir?
  2. log_freshness        - bot.log mtime within WATCHDOG_LOG_STALE_MIN
  3. recent_errors         - ERROR-line count within WATCHDOG_ERROR_WINDOW_MIN
     bot_internal_alerts   - any "ALERT:" lines in that same window
  4. signal_drought        - a new signals row within WATCHDOG_SIGNAL_DROUGHT_H
  5. disk_space            - free space on the drive holding bot.db
  6. stale_positions       - an open position whose last_price hasn't moved
                              across watchdog runs for WATCHDOG_POSITION_STALE_H

Usage:
    python watchdog.py                  loop forever (WATCHDOG_INTERVAL_SEC)
    python watchdog.py --once           single pass; exit 0 pass / 1 alert
    python watchdog.py --test-alert     fire a test alert through every sink
    python watchdog.py --dir PATH ...   monitor one or more bot directories
                                         (repeatable). Default: this script's
                                         own directory.

Each instance's thresholds come from ITS OWN .env via dotenv_values() (never
load_dotenv(), which would leak one instance's settings into another's) -
this is built to monitor several duplicated bot directories at once.
"""

import argparse
import base64
import ctypes
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values

try:
    import requests
except ImportError:
    requests = None

try:
    import psutil
except ImportError:
    psutil = None


SCRIPT_DIR = Path(__file__).resolve().parent
WATCHDOG_LOG_PATH = SCRIPT_DIR / "watchdog.log"
WATCHDOG_STATUS_PATH = SCRIPT_DIR / "watchdog_status.json"
WATCHDOG_STATE_PATH = SCRIPT_DIR / "watchdog_state.json"

DEFAULTS = {
    "WATCHDOG_LOG_STALE_MIN": "10",
    "WATCHDOG_ERROR_WINDOW_MIN": "30",
    "WATCHDOG_ERROR_MAX": "5",
    "WATCHDOG_SIGNAL_DROUGHT_H": "12",
    "WATCHDOG_MIN_FREE_GB": "10",
    "WATCHDOG_POSITION_STALE_H": "2",
    "WATCHDOG_COOLDOWN_MIN": "60",
    "WATCHDOG_INTERVAL_SEC": "120",
}

_LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\w+) (.*)$")


# ---------------------------------------------------------------------------
# watchdog's own log (never bot.log)
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str):
    """Print to console and append to watchdog.log (own file, not bot.log)."""
    line = f"{_now_utc_iso()} {msg}"
    print(line)
    with _log_lock:
        try:
            with open(WATCHDOG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            print(f"{_now_utc_iso()} [watchdog] could not write watchdog.log: {e}")


# ---------------------------------------------------------------------------
# Check result model
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    """One check's outcome: pass/fail, its measured value vs threshold, and a
    human-readable detail string used in alerts and the status file."""
    key: str
    label: str
    passed: bool
    value: str
    threshold: str
    detail: str
    level: str = "warning"  # "warning" or "error" - picks the MessageBox icon


def _safe_check(key: str, label: str, fn, *fn_args) -> CheckResult:
    """Run one check, catching anything it raises so a broken check can never
    take down the loop or the other checks."""
    try:
        return fn(*fn_args)
    except Exception as e:
        log(f"[watchdog] check '{key}' raised: {type(e).__name__}: {e}")
        return CheckResult(key, label, False, "error", "n/a",
                            f"check raised {type(e).__name__}: {e}")


def _safe_check_list(key: str, fn, *fn_args) -> list[CheckResult]:
    """Same as _safe_check but for checks that return more than one result."""
    try:
        return fn(*fn_args)
    except Exception as e:
        log(f"[watchdog] check '{key}' raised: {type(e).__name__}: {e}")
        return [CheckResult(key, key, False, "error", "n/a",
                             f"check raised {type(e).__name__}: {e}")]


# ---------------------------------------------------------------------------
# Alert sinks - tried in order, no external dependency required
# ---------------------------------------------------------------------------

def _ps_quote(s: str) -> str:
    """Escape a string for embedding as a single-quoted PowerShell literal."""
    return "'" + s.replace("'", "''") + "'"


def _run_powershell(script: str, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a PowerShell script via -EncodedCommand (UTF-16LE/base64), which
    sidesteps all shell-quoting issues regardless of message content."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=timeout,
    )


def _toast_alert(title: str, message: str) -> bool:
    """Windows toast via PowerShell WinRT (Windows.UI.Notifications). No module
    install required - this is Win10 Home so it uses the "Windows PowerShell"
    notifier identity, which does not require a registered AUMID/shortcut.
    Returns True only if the script ran to completion without raising."""
    script = (
        '$ErrorActionPreference = "Stop"\n'
        "try {\n"
        "    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null\n"
        "    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime] > $null\n"
        "    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n"
        "    $textNodes = $template.GetElementsByTagName(\"text\")\n"
        f"    $textNodes.Item(0).AppendChild($template.CreateTextNode({_ps_quote(title)})) > $null\n"
        f"    $textNodes.Item(1).AppendChild($template.CreateTextNode({_ps_quote(message)})) > $null\n"
        "    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)\n"
        '    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('
        '"Windows PowerShell").Show($toast)\n'
        '    Write-Output "WATCHDOG_TOAST_OK"\n'
        "} catch {\n"
        '    Write-Output "WATCHDOG_TOAST_FAIL: $($_.Exception.Message)"\n'
        "    exit 1\n"
        "}\n"
    )
    try:
        result = _run_powershell(script)
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        log(f"[watchdog] toast invocation failed: {type(e).__name__}: {e}")
        return False
    ok = result.returncode == 0 and "WATCHDOG_TOAST_OK" in (result.stdout or "")
    if not ok:
        log(f"[watchdog] toast did not report success (rc={result.returncode}): "
            f"{(result.stdout or '').strip()} {(result.stderr or '').strip()}")
    return ok


def _messagebox_alert(title: str, message: str, is_error: bool = False,
                       blocking: bool = False):
    """Fallback: ctypes MessageBoxW. Runs on a daemon thread by default so a
    modal box can never block the monitor loop (--loop / --once, where the
    process may exit or re-check regardless of whether the box was seen).
    blocking=True is used only by --test-alert, where the whole point is for
    the operator to see it - a daemon thread would otherwise be killed the
    instant the short-lived test process exits, before the box could render."""
    def _show():
        try:
            mb_icon = 0x10 if is_error else 0x30  # MB_ICONERROR / MB_ICONWARNING
            mb_systemmodal = 0x1000
            ctypes.windll.user32.MessageBoxW(None, message, title, mb_icon | mb_systemmodal)
        except OSError as e:
            log(f"[watchdog] MessageBoxW failed: {type(e).__name__}: {e}")

    if blocking:
        _show()
        return
    threading.Thread(target=_show, daemon=True).start()


def _scrub(exc: Exception, url: str) -> str:
    """Exception text with the webhook URL (and its token) masked out.

    requests embeds the full request URL in connection-error messages, e.g.
    "Max retries exceeded with url: /api/webhooks/<id>/<token>". Anyone holding
    that token can post to the channel, so it must never reach watchdog.log.
    """
    text = str(exc)
    if url:
        text = text.replace(url, "<webhook redacted>")
        # Connection errors carry only the path, not the full URL, so mask the
        # trailing /<id>/<token> segments independently.
        tail = url.split("/api/webhooks/")[-1]
        if tail and tail != url:
            text = text.replace(tail, "<redacted>")
            for part in tail.split("/"):
                if len(part) > 8:
                    text = text.replace(part, "<redacted>")
    return text


def _webhook_alert(url: str, message: str):
    """Optional: POST to ALERT_WEBHOOK_URL (same var bot.py uses), if set."""
    if not requests:
        return
    try:
        requests.post(url, json={"content": f"[Watchdog] {message}"}, timeout=8)
    except requests.RequestException as e:
        log(f"[watchdog] webhook post failed: {type(e).__name__}: {_scrub(e, url)}")
    except OSError as e:
        log(f"[watchdog] webhook post failed: {type(e).__name__}: {_scrub(e, url)}")


def fire_sinks(title: str, message: str, webhook_url: str = "", is_error: bool = False,
               blocking_fallback: bool = False):
    """Push one notification through every sink: log -> toast -> (fallback)
    MessageBox -> optional webhook. Console+file logging always happens."""
    log(f"ALERT {title}: {message}")
    toast_ok = _toast_alert(title, message)
    if not toast_ok:
        _messagebox_alert(title, message, is_error=is_error, blocking=blocking_fallback)
    if webhook_url:
        _webhook_alert(webhook_url, message)


# ---------------------------------------------------------------------------
# Persisted state - alert cooldowns, prior pass/fail (for recovery notices),
# and the position price-tracker. Lives in the watchdog's own directory, so
# it survives across `--once` invocations from Task Scheduler.
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if WATCHDOG_STATE_PATH.exists():
        try:
            with open(WATCHDOG_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log(f"[watchdog] could not read {WATCHDOG_STATE_PATH.name}, starting fresh: {e}")
            data = {}
    else:
        data = {}
    data.setdefault("positions", {})
    data.setdefault("cooldown", {})
    data.setdefault("check_status", {})
    return data


def save_state(state: dict):
    try:
        with open(WATCHDOG_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        log(f"[watchdog] could not write {WATCHDOG_STATE_PATH.name}: {e}")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_process_alive(dir_path: Path, instance: str) -> CheckResult:
    """Is there a 'python bot.py' process whose cwd maps to dir_path? Uses
    psutil if it happens to be installed (checked at import time - never
    added as a new dependency); otherwise falls back to a system-wide wmic
    scan that cannot map a process to a specific directory, and is reported
    as best-effort so it never fires a false alert on its own - log_freshness
    is the authoritative liveness signal in that case."""
    target = os.path.normcase(str(dir_path.resolve()))

    if psutil is not None:
        matches = []
        for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
            try:
                info = proc.info
                cmdline = info.get("cmdline") or []
                if not any("bot.py" in (part or "").lower() for part in cmdline):
                    continue
                cwd = info.get("cwd")
                if cwd and os.path.normcase(str(Path(cwd).resolve())) == target:
                    matches.append(info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        passed = bool(matches)
        detail = (f"pid(s) {matches} running bot.py in {dir_path}" if matches
                  else f"no 'python bot.py' process found with cwd={dir_path}")
        return CheckResult("process_alive", "Bot process alive", passed,
                            str(len(matches)), ">=1", detail,
                            level="error" if not passed else "warning")

    # Fallback: psutil not installed. wmic gives CommandLine but not cwd, so
    # this can only confirm "some bot.py process exists somewhere" - it can't
    # tell instances apart. Always reported as passing/best-effort.
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "name='python.exe' or name='pythonw.exe'",
             "get", "CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=15,
        )
        found_any = "bot.py" in (result.stdout or "").lower()
        detail = ("psutil not installed - system-wide wmic scan only, cannot confirm "
                  f"this process belongs to {instance}; a bot.py process exists "
                  f"somewhere: {found_any}. log_freshness is the authoritative signal.")
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        detail = (f"psutil not installed and wmic query failed ({type(e).__name__}: {e}); "
                  "relying on log_freshness instead")
    return CheckResult("process_alive", "Bot process alive (best-effort)", True,
                        "n/a", "n/a", detail)


def check_log_freshness(dir_path: Path, now: datetime, stale_min: int) -> CheckResult:
    """bot.log mtime older than stale_min minutes => the bot has stopped
    writing => most reliable liveness signal (it logs an inbox poll ~60s)."""
    log_path = dir_path / "bot.log"
    if not log_path.exists():
        return CheckResult("log_freshness", "Log freshness", False, "missing",
                            f"<={stale_min}m", f"{log_path} does not exist",
                            level="error")
    mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=now.tzinfo)
    age_min = (now - mtime).total_seconds() / 60.0
    passed = age_min <= stale_min
    return CheckResult("log_freshness", "Log freshness", passed,
                        f"{age_min:.1f}m", f"<={stale_min}m",
                        f"bot.log last written {age_min:.1f} min ago",
                        level="error" if not passed else "warning")


def _tail_lines(path: Path, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    """Read up to the last max_bytes of a text file (bounds the work on a
    log file that can grow to LOG_MAX_MB before rotating) and split to lines."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()  # drop the partial first line from the mid-file seek
        data = f.read()
    # utf-8-sig strips a leading BOM if one is present. Python's logging module
    # does not write one, but a log recreated by a PowerShell redirect or an
    # editor can carry it, and the BOM would break _LOG_LINE_RE on the first
    # line only - silently dropping one ERROR from the count.
    return data.decode("utf-8-sig", errors="replace").splitlines()


def check_recent_errors(dir_path: Path, now: datetime, window_min: int,
                         error_max: int) -> list[CheckResult]:
    """Two results: ERROR-line count in the window (vs error_max), and any
    bot-internal 'ALERT:' lines in the same window (those go nowhere when the
    bot has no webhook configured - this is exactly what surfaces them)."""
    log_path = dir_path / "bot.log"
    if not log_path.exists():
        detail = f"{log_path} does not exist"
        return [
            CheckResult("recent_errors", "Recent ERROR lines", True, "0",
                        f"<={error_max}", detail),
            CheckResult("bot_internal_alerts", "Bot internal ALERT: lines", True,
                        "0", "0", detail),
        ]

    cutoff = now - timedelta(minutes=window_min)
    error_msgs = []
    alert_lines = []
    for line in _tail_lines(log_path):
        m = _LOG_LINE_RE.match(line)
        if not m:
            continue
        ts_str, level, rest = m.groups()
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if level == "ERROR":
            error_msgs.append(rest.strip())
        if "ALERT:" in rest:
            alert_lines.append(rest.strip())

    err_count = len(error_msgs)
    err_passed = err_count <= error_max
    if error_msgs:
        top_msg, top_n = Counter(error_msgs).most_common(1)[0]
        err_detail = (f"{err_count} ERROR line(s) in the last {window_min}m; "
                      f"most common ({top_n}x): {top_msg[:200]}")
    else:
        err_detail = f"0 ERROR lines in the last {window_min}m"
    err_result = CheckResult("recent_errors", "Recent ERROR lines", err_passed,
                              str(err_count), f"<={error_max}", err_detail,
                              level="error" if not err_passed else "warning")

    alert_passed = not alert_lines
    if alert_lines:
        top_alert, top_alert_n = Counter(alert_lines).most_common(1)[0]
        alert_detail = (f"{len(alert_lines)} bot-internal ALERT: line(s) in the last "
                        f"{window_min}m (these go nowhere without ALERT_WEBHOOK_URL set); "
                        f"most common ({top_alert_n}x): {top_alert[:200]}")
    else:
        alert_detail = f"no bot-internal ALERT: lines in the last {window_min}m"
    alert_result = CheckResult("bot_internal_alerts", "Bot internal ALERT: lines",
                                alert_passed, str(len(alert_lines)), "0", alert_detail)

    return [err_result, alert_result]


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Read-only sqlite connection - the bot writes bot.db, this never does."""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def check_signal_drought(dir_path: Path, now: datetime, drought_h: float) -> CheckResult:
    """No new row in the signals table for drought_h hours => possible dead
    Discord tab."""
    db_path = dir_path / "bot.db"
    if not db_path.exists():
        return CheckResult("signal_drought", "Signal drought", False, "missing",
                            f"<={drought_h}h", f"{db_path} does not exist")
    con = _connect_ro(db_path)
    try:
        row = con.execute("SELECT MAX(ts) FROM signals").fetchone()
    finally:
        con.close()

    last_ts_raw = row[0] if row else None
    if not last_ts_raw:
        return CheckResult("signal_drought", "Signal drought", False, "none",
                            f"<={drought_h}h", "no rows in the signals table yet")
    try:
        last_ts = datetime.fromisoformat(last_ts_raw)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=now.tzinfo)
    except ValueError:
        return CheckResult("signal_drought", "Signal drought", False, "unparseable",
                            f"<={drought_h}h", f"could not parse ts '{last_ts_raw}'")

    age_h = (now - last_ts).total_seconds() / 3600.0
    passed = age_h <= drought_h
    return CheckResult("signal_drought", "Signal drought", passed, f"{age_h:.2f}h",
                        f"<={drought_h}h", f"last signal {age_h:.2f}h ago ({last_ts_raw})")


def check_disk_space(dir_path: Path, min_free_gb: float) -> CheckResult:
    """Free space on the drive holding this instance below min_free_gb =>
    ALERT (SQLite throws 'database or disk is full' well before 0 bytes)."""
    usage = shutil.disk_usage(str(dir_path))
    free_gb = usage.free / (1024 ** 3)
    passed = free_gb >= min_free_gb
    return CheckResult("disk_space", "Disk space", passed, f"{free_gb:.2f}GB",
                        f">={min_free_gb}GB",
                        f"{free_gb:.2f} GB free on the drive holding {dir_path}",
                        level="error" if not passed else "warning")


def check_stale_positions(dir_path: Path, instance: str, now: datetime,
                           stale_h: float, state: dict) -> CheckResult:
    """An open position whose last_price hasn't moved AND whose row is older
    than stale_h hours => position management may have stopped even though
    the process lives. bot.db has no "last-updated" column, so the watchdog
    tracks each order_id's price across its OWN run history (persisted in
    watchdog_state.json) - a position only becomes stale once THIS watchdog
    has observed an unchanged price for the full window."""
    db_path = dir_path / "bot.db"
    if not db_path.exists():
        return CheckResult("stale_positions", "Stale open positions", True, "0",
                            f"unchanged >={stale_h}h", f"{db_path} does not exist")

    con = _connect_ro(db_path)
    try:
        rows = con.execute(
            "SELECT order_id, symbol, last_price, opened_at FROM positions "
            "WHERE status='open'"
        ).fetchall()
    finally:
        con.close()

    tracker = state.setdefault("positions", {})
    prefix = f"{instance}:"
    live_keys = set()
    stale = []

    for order_id, symbol, last_price, opened_at in rows:
        key = f"{prefix}{order_id}"
        live_keys.add(key)
        price_str = "" if last_price is None else f"{last_price:.10g}"
        entry = tracker.get(key)

        if entry is None or entry.get("price") != price_str:
            tracker[key] = {"price": price_str, "first_seen": now.isoformat()}
            continue

        try:
            first_seen = datetime.fromisoformat(entry["first_seen"])
        except (ValueError, KeyError, TypeError):
            tracker[key] = {"price": price_str, "first_seen": now.isoformat()}
            continue

        unchanged_h = (now - first_seen).total_seconds() / 3600.0
        try:
            opened = datetime.fromisoformat(opened_at) if opened_at else None
        except ValueError:
            opened = None
        age_h = (now - opened).total_seconds() / 3600.0 if opened else unchanged_h

        if unchanged_h >= stale_h and age_h >= stale_h:
            stale.append(f"{symbol} ({order_id}) price unchanged {unchanged_h:.1f}h, "
                        f"open {age_h:.1f}h")

    # Drop tracker entries for positions that are no longer open.
    for key in [k for k in tracker if k.startswith(prefix) and k not in live_keys]:
        del tracker[key]

    passed = not stale
    detail = "; ".join(stale) if stale else f"{len(rows)} open position(s), none stale"
    return CheckResult("stale_positions", "Stale open positions", passed, str(len(stale)),
                        f"0 (unchanged >={stale_h}h)", detail)


# ---------------------------------------------------------------------------
# Config resolution per instance
# ---------------------------------------------------------------------------

def _cfg_int(cfg: dict, key: str, default: str) -> int:
    try:
        return int(cfg.get(key) or default)
    except (TypeError, ValueError):
        return int(default)


def _cfg_float(cfg: dict, key: str, default: str) -> float:
    try:
        return float(cfg.get(key) or default)
    except (TypeError, ValueError):
        return float(default)


def _resolve_thresholds(cfg: dict) -> tuple[dict, timezone]:
    thresholds = {
        "log_stale_min": _cfg_int(cfg, "WATCHDOG_LOG_STALE_MIN", DEFAULTS["WATCHDOG_LOG_STALE_MIN"]),
        "error_window_min": _cfg_int(cfg, "WATCHDOG_ERROR_WINDOW_MIN", DEFAULTS["WATCHDOG_ERROR_WINDOW_MIN"]),
        "error_max": _cfg_int(cfg, "WATCHDOG_ERROR_MAX", DEFAULTS["WATCHDOG_ERROR_MAX"]),
        "signal_drought_h": _cfg_float(cfg, "WATCHDOG_SIGNAL_DROUGHT_H", DEFAULTS["WATCHDOG_SIGNAL_DROUGHT_H"]),
        "min_free_gb": _cfg_float(cfg, "WATCHDOG_MIN_FREE_GB", DEFAULTS["WATCHDOG_MIN_FREE_GB"]),
        "position_stale_h": _cfg_float(cfg, "WATCHDOG_POSITION_STALE_H", DEFAULTS["WATCHDOG_POSITION_STALE_H"]),
        "cooldown_min": _cfg_int(cfg, "WATCHDOG_COOLDOWN_MIN", DEFAULTS["WATCHDOG_COOLDOWN_MIN"]),
    }
    tz_hours = _cfg_float(cfg, "TZ_OFFSET_HOURS", "-5")
    return thresholds, timezone(timedelta(hours=tz_hours))


def _load_env(dir_path: Path) -> dict:
    """dotenv_values (not load_dotenv!) so one instance's .env never leaks
    into another's - each monitored directory is read independently."""
    env_path = dir_path / ".env"
    if not env_path.exists():
        return {}
    try:
        return {k: v for k, v in dotenv_values(str(env_path)).items()}
    except OSError as e:
        log(f"[watchdog] could not read {env_path}: {type(e).__name__}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Alert / recovery dispatch with per-check cooldown + dedupe
# ---------------------------------------------------------------------------

def dispatch_alert(state: dict, instance: str, result: CheckResult, cooldown_min: int,
                    webhook_url: str, bypass_cooldown: bool = False,
                    blocking_fallback: bool = False):
    full_key = f"{instance}:{result.key}"
    now_ts = time.time()
    last = state["cooldown"].get(full_key, 0)
    if not bypass_cooldown and (now_ts - last) < cooldown_min * 60:
        return
    state["cooldown"][full_key] = now_ts
    title = f"Discord Bot Watchdog: {instance}"
    message = f"{result.label}: {result.detail}"
    fire_sinks(title, message, webhook_url=webhook_url, is_error=(result.level == "error"),
               blocking_fallback=blocking_fallback)


def dispatch_recovery(instance: str, result: CheckResult, webhook_url: str):
    title = f"Discord Bot Watchdog: {instance} recovered"
    message = f"{result.label} is passing again ({result.detail})"
    fire_sinks(title, message, webhook_url=webhook_url, is_error=False)


def process_results(state: dict, instance: str, results: list[CheckResult],
                     cooldown_min: int, webhook_url: str) -> bool:
    """Fire alerts for failing checks (cooldown-gated) and recovery notices
    for checks that just went from failing to passing. Returns whether any
    check for this instance is currently failing."""
    any_failed = False
    status = state["check_status"]
    for result in results:
        full_key = f"{instance}:{result.key}"
        prev = status.get(full_key)
        if result.passed:
            if prev == "fail":
                dispatch_recovery(instance, result, webhook_url)
        else:
            any_failed = True
            dispatch_alert(state, instance, result, cooldown_min, webhook_url)
        status[full_key] = "pass" if result.passed else "fail"
    return any_failed


def write_status_json(all_results: dict[str, list[CheckResult]], generated_at: str):
    payload = {"generated_at": generated_at, "instances": {}}
    for instance, results in all_results.items():
        payload["instances"][instance] = [
            {
                "check": r.key,
                "label": r.label,
                "passed": r.passed,
                "value": r.value,
                "threshold": r.threshold,
                "detail": r.detail,
            }
            for r in results
        ]
    try:
        with open(WATCHDOG_STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        log(f"[watchdog] could not write {WATCHDOG_STATUS_PATH.name}: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# One full pass over every monitored instance
# ---------------------------------------------------------------------------

def run_cycle(dirs: list[Path], state: dict) -> bool:
    """Run all checks for all instances once. Returns True iff everything
    currently passes across every instance."""
    all_results: dict[str, list[CheckResult]] = {}
    all_ok = True

    for dir_path in dirs:
        instance = dir_path.name
        if not dir_path.exists():
            log(f"[watchdog] WARNING: monitored directory does not exist: {dir_path}")
            all_ok = False
            continue

        cfg = _load_env(dir_path)
        thresholds, tz = _resolve_thresholds(cfg)
        now = datetime.now(tz)

        results = [
            _safe_check("process_alive", "Bot process alive",
                        check_process_alive, dir_path, instance),
            _safe_check("log_freshness", "Log freshness",
                        check_log_freshness, dir_path, now, thresholds["log_stale_min"]),
        ]
        results.extend(_safe_check_list(
            "recent_errors", check_recent_errors, dir_path, now,
            thresholds["error_window_min"], thresholds["error_max"]))
        results.append(_safe_check(
            "signal_drought", "Signal drought",
            check_signal_drought, dir_path, now, thresholds["signal_drought_h"]))
        results.append(_safe_check(
            "disk_space", "Disk space",
            check_disk_space, dir_path, thresholds["min_free_gb"]))
        results.append(_safe_check(
            "stale_positions", "Stale open positions",
            check_stale_positions, dir_path, instance, now,
            thresholds["position_stale_h"], state))

        webhook_url = (cfg.get("ALERT_WEBHOOK_URL") or "").strip()
        failed = process_results(state, instance, results, thresholds["cooldown_min"],
                                  webhook_url)
        all_ok = all_ok and not failed
        all_results[instance] = results

        summary = ", ".join(f"{r.key}={'OK' if r.passed else 'FAIL'}" for r in results)
        log(f"[watchdog] {instance}: {summary}")

    write_status_json(all_results, _now_utc_iso())
    save_state(state)
    return all_ok


def run_test_alert(dirs: list[Path], state: dict):
    """Fire a synthetic alert through every sink so the operator can confirm
    notifications actually appear on screen."""
    dir_path = dirs[0] if dirs else SCRIPT_DIR
    instance = dir_path.name
    cfg = _load_env(dir_path)
    webhook_url = (cfg.get("ALERT_WEBHOOK_URL") or "").strip()
    result = CheckResult("test_alert", "Test alert", False, "n/a", "n/a",
                          "This is a watchdog.py --test-alert firing every sink "
                          "(toast, MessageBox fallback, log, webhook if configured).")
    log(f"[watchdog] --test-alert: firing through all sinks for instance '{instance}'")
    dispatch_alert(state, instance, result, cooldown_min=0, webhook_url=webhook_url,
                   bypass_cooldown=True, blocking_fallback=True)
    save_state(state)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="External process-liveness watchdog for the Discord Signal Bot.")
    parser.add_argument(
        "--dir", dest="dirs", action="append", default=[],
        help="Bot instance directory to monitor (repeatable). "
             "Default: this script's own directory.")
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single pass and exit (0 = all checks pass, 1 = any alert).")
    parser.add_argument(
        "--test-alert", action="store_true",
        help="Fire a test alert through every sink, then exit.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    dirs = [Path(d).resolve() for d in args.dirs] if args.dirs else [SCRIPT_DIR]
    for d in dirs:
        if not d.exists():
            log(f"[watchdog] WARNING: monitored directory does not exist: {d}")

    state = load_state()

    if args.test_alert:
        run_test_alert(dirs, state)
        return 0

    if args.once:
        ok = run_cycle(dirs, state)
        return 0 if ok else 1

    first_cfg = _load_env(dirs[0]) if dirs else {}
    interval = _cfg_int(first_cfg, "WATCHDOG_INTERVAL_SEC", DEFAULTS["WATCHDOG_INTERVAL_SEC"])
    log(f"[watchdog] starting loop, interval={interval}s, monitoring: "
        f"{', '.join(d.name for d in dirs)}")
    while True:
        try:
            run_cycle(dirs, state)
        except Exception as e:  # the monitor loop itself must never die
            log(f"[watchdog] run_cycle raised unexpectedly: {type(e).__name__}: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
