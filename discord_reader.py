"""
Reads Unity Academy Discord notification inbox via agent-browser CDP.

Discord uses obfuscated class names that change with updates. The JS selectors
here are best-effort; update them when Discord pushes a class-name rotation.
`snapshot` mode is available as a fallback if JS eval stops working.
"""

import json
import subprocess
import time
from logger import log

CDP_PORT = 9222
AGENT_BROWSER = "agent-browser"


def _run(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        [AGENT_BROWSER, "--cdp", str(CDP_PORT)] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def verify_connected() -> bool:
    """Returns True if agent-browser can reach the browser on CDP port."""
    try:
        result = _run(["get", "url"], timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def ensure_discord_open() -> bool:
    """Navigate to Discord if it's not already the active page."""
    try:
        result = _run(["get", "url"], timeout=5)
        if "discord.com" not in result.stdout.lower():
            log.info("Discord not in active tab — navigating there now")
            _run(["navigate", "https://discord.com/channels/@me"], timeout=15)
            time.sleep(4)
        return True
    except Exception as e:
        log.warning(f"ensure_discord_open failed: {e}")
        return False


def open_inbox() -> bool:
    """Clicks the Discord notification inbox button (bell icon)."""
    try:
        result = _run(["find", "role", "button", "click", "--name", "Inbox"], timeout=10)
        return result.returncode == 0
    except Exception as e:
        log.warning(f"Could not click inbox button: {e}")
        return False


def _parse_notifications_js() -> list[dict]:
    """
    Extracts notification cards from the open inbox panel via JS eval.
    Returns list of {id, author, content, time} dicts.
    """
    js = r"""
    (function() {
      // Notification cards sit inside the inbox panel.
      // Discord obfuscates class names; we target structural selectors.
      var cards = Array.from(document.querySelectorAll(
        '[class*="notificationItem"], [class*="notification-item"], [class*="container_"]'
      )).filter(function(el) {
        // Keep only elements that contain message-content children
        return el.querySelector('[id^="message-content-"], [class*="messageContent"]');
      }).slice(0, 40);

      return cards.map(function(el) {
        // Try multiple selector strategies for each field
        var id = el.getAttribute('data-list-item-id')
               || el.getAttribute('id')
               || el.querySelector('[id]')?.id
               || '';

        var author = (
          el.querySelector('[class*="username"], [class*="author"]')?.textContent
          || el.querySelector('[class*="headerText"] span')?.textContent
          || ''
        ).trim();

        var content = (
          el.querySelector('[id^="message-content-"]')?.textContent
          || el.querySelector('[class*="messageContent"]')?.textContent
          || ''
        ).trim();

        var time = el.querySelector('time')?.getAttribute('datetime') || '';

        return {id: id, author: author, content: content, time: time};
      }).filter(function(m) { return m.content; });
    })()
    """
    try:
        result = _run(["eval", js], timeout=15)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        return json.loads(result.stdout)
    except Exception as e:
        log.warning(f"JS eval for notifications failed: {e}")
        return []


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
        log.debug("No notifications found in inbox (panel may be empty or selectors stale)")
        return []

    whitelist_lower = {name.lower() for name in whitelist}
    new_msgs = []
    for msg in messages:
        if not msg.get("id"):
            continue
        if msg["id"] in seen_ids:
            continue
        author_lower = msg.get("author", "").lower()
        if not any(w in author_lower for w in whitelist_lower):
            continue
        new_msgs.append(msg)

    log.debug(f"Inbox poll: {len(messages)} total, {len(new_msgs)} new from whitelisted analysts")
    return new_msgs
