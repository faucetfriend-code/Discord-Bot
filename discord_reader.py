"""
Reads Unity Academy Discord notification inbox via direct Chrome CDP.

Chrome exposes a DevTools Protocol API on port 9222:
  - HTTP  http://localhost:9222/json   → list of open tabs
  - WS    tab["webSocketDebuggerUrl"]  → Runtime.evaluate, etc.

Discord uses obfuscated class names that change with updates. The JS selectors
here are best-effort; update them when Discord pushes a class-name rotation.
"""

import json
import time
import requests
import websocket
from logger import log

CDP_BASE = "http://localhost:9222"


def _get_tabs() -> list[dict]:
    resp = requests.get(f"{CDP_BASE}/json", timeout=5)
    return resp.json()


def _get_discord_ws_url() -> str | None:
    """Return the WebSocket debugger URL for the first Discord tab, or None."""
    try:
        for tab in _get_tabs():
            if "discord.com" in tab.get("url", ""):
                return tab.get("webSocketDebuggerUrl")
    except Exception:
        pass
    return None


def _eval_js(ws_url: str, js: str, timeout: int = 10):
    """
    Execute JS in the tab via CDP Runtime.evaluate.
    Returns the result value (Python object), or None on any failure.
    """
    ws = None
    try:
        ws = websocket.create_connection(ws_url, timeout=timeout)
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": js,
                "returnByValue": True,
                "awaitPromise": False,
            }
        }))
        raw = ws.recv()
        data = json.loads(raw)
        result = data.get("result", {}).get("result", {})
        if result.get("subtype") == "null" or result.get("type") == "undefined":
            return None
        return result.get("value")
    except Exception as e:
        log.warning(f"CDP eval failed: {e}")
        return None
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass


def verify_connected() -> bool:
    """Returns True if Chrome is reachable on the CDP port."""
    try:
        return requests.get(f"{CDP_BASE}/json", timeout=3).status_code == 200
    except Exception:
        return False


def ensure_discord_open() -> bool:
    """Navigate to Discord if it's not already the active page."""
    try:
        if _get_discord_ws_url():
            return True
        # No Discord tab found — navigate the first available tab
        tabs = _get_tabs()
        if not tabs:
            return False
        ws_url = tabs[0].get("webSocketDebuggerUrl")
        if not ws_url:
            return False
        log.info("Discord not in active tab — navigating there now")
        _eval_js(ws_url, "window.location.href = 'https://discord.com/channels/@me'", timeout=15)
        time.sleep(4)
        return bool(_get_discord_ws_url())
    except Exception as e:
        log.warning(f"ensure_discord_open failed: {e}")
        return False


def open_inbox() -> bool:
    """Opens the Discord notification inbox panel. No-ops if already open."""
    ws_url = _get_discord_ws_url()
    if not ws_url:
        log.warning("No Discord tab found for inbox click")
        return False
    js = """(function() {
      var btn = document.querySelector('[aria-label="Inbox"]')
             || document.querySelector('[aria-label="inbox"]');
      if (!btn) return 'not_found';
      if (btn.getAttribute('aria-expanded') === 'true') return 'already_open';
      btn.click();
      return 'clicked';
    })()"""
    try:
        result = _eval_js(ws_url, js, timeout=10)
        if result == 'not_found':
            log.warning("Inbox button not found in DOM (aria-label='Inbox' missing)")
            return False
        if result == 'already_open':
            log.debug("Inbox already open — skipping click")
        return True
    except Exception as e:
        log.warning(f"Could not click inbox button: {e}")
        return False


def _parse_notifications_js() -> list[dict]:
    """
    Extracts notification cards from the open inbox panel via JS eval.
    Returns list of {id, author, content, time} dicts.
    """
    ws_url = _get_discord_ws_url()
    if not ws_url:
        return []

    js = r"""
    (function() {
      var cards = Array.from(document.querySelectorAll(
        '[class*="notificationItem"], [class*="notification-item"], [class*="container_"]'
      )).filter(function(el) {
        return el.querySelector('[id^="message-content-"], [class*="messageContent"]');
      }).slice(0, 40);

      return cards.map(function(el) {
        var id = el.getAttribute('data-list-item-id')
               || el.getAttribute('id')
               || (el.querySelector('[id]') && el.querySelector('[id]').id)
               || '';

        var author = (
          (el.querySelector('[class*="username"], [class*="author"]') || {}).textContent
          || (el.querySelector('[class*="headerText"] span') || {}).textContent
          || ''
        ).trim();

        var content = (
          (el.querySelector('[id^="message-content-"]') || {}).textContent
          || (el.querySelector('[class*="messageContent"]') || {}).textContent
          || ''
        ).trim();

        var timeEl = el.querySelector('time');
        var time = timeEl ? timeEl.getAttribute('datetime') : '';

        return {id: id, author: author, content: content, time: time};
      }).filter(function(m) { return m.content; });
    })()
    """
    try:
        result = _eval_js(ws_url, js, timeout=15)
        if result is None:
            return []
        if isinstance(result, list):
            return result
        return []
    except Exception as e:
        log.warning(f"JS eval for notifications failed: {e}")
        return []


def _dump_inbox_panel_html() -> str:
    """
    Diagnostic: returns truncated outerHTML of the inbox panel so we can
    inspect Discord's actual DOM structure and update selectors as needed.
    """
    ws_url = _get_discord_ws_url()
    if not ws_url:
        return "no_discord_tab"
    js = r"""
    (function() {
      var panel = document.querySelector('[class*="inbox"]')
               || document.querySelector('[aria-label*="nbox"]')
               || document.querySelector('[class*="notif"]');
      if (!panel) return "NO_PANEL_FOUND";
      return panel.outerHTML.substring(0, 4000);
    })()
    """
    result = _eval_js(ws_url, js, timeout=10)
    return str(result or "eval_returned_none")


def poll_inbox(seen_ids: set[str], whitelist: list[str]) -> list[dict]:
    """
    Opens the inbox, scrapes notification cards, filters by analyst whitelist,
    and returns only messages not already in seen_ids.

    Each returned dict: {id, author, content, time}
    """
    ensure_discord_open()

    if not open_inbox():
        log.warning("Failed to open Discord inbox — skipping poll")
        return []

    time.sleep(1.0)  # allow panel animation to settle

    messages = _parse_notifications_js()
    if not messages:
        log.warning("No notifications found — selectors may be stale. Dumping panel HTML for diagnosis:")
        log.warning(_dump_inbox_panel_html())
        return []

    whitelist_lower = {name.lower() for name in whitelist}
    # Log unique authors seen so we can verify whitelist matches
    seen_authors = {msg.get("author", "") for msg in messages if msg.get("author")}
    if seen_authors:
        log.info(f"Authors seen in inbox: {sorted(seen_authors)}")
    else:
        log.warning("No author field extracted from any notification card — JS author selector may need updating")

    new_msgs = []
    for msg in messages:
        if not msg.get("id"):
            continue
        if msg["id"] in seen_ids:
            continue
        # Analyst name appears as a @mention in content (e.g. "@Soul Alerts"),
        # not necessarily as the author field — check both.
        haystack = (msg.get("author", "") + " " + msg.get("content", "")).lower()
        if not any(w in haystack for w in whitelist_lower):
            continue
        new_msgs.append(msg)

    log.info(f"Inbox poll: {len(messages)} total, {len(new_msgs)} new from whitelisted analysts")
    return new_msgs
