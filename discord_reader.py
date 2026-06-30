"""
Reads Unity Academy Discord notification inbox via direct Chrome CDP.

Chrome exposes a DevTools Protocol API on port 9222:
  - HTTP  http://localhost:9222/json   → list of open tabs
  - WS    tab["webSocketDebuggerUrl"]  → Runtime.evaluate, etc.

Two operation modes:
  poll_inbox()          — one-shot scrape of current inbox cards (used as fallback sweep)
  NotificationListener  — persistent CDP WebSocket + MutationObserver for real-time delivery

Discord uses obfuscated class names that change with updates. The JS selectors
here are best-effort; update them when Discord pushes a class-name rotation.
"""

import json
import queue
import threading
import time
import requests
import websocket
from logger import log

CDP_BASE = "http://localhost:9222"

# ---------------------------------------------------------------------------
# MutationObserver script — injected once, fires for every new inbox card
# ---------------------------------------------------------------------------
_OBSERVER_JS = r"""
(function() {
  if (window.__botObserver) {
    try { window.__botObserver.disconnect(); } catch(e) {}
  }

  function extractCard(el) {
    var id = el.getAttribute('data-list-item-id')
           || el.getAttribute('id')
           || (el.querySelector('[id]') ? el.querySelector('[id]').id : '')
           || '';

    // --- Author ---
    var author = (
      (el.querySelector('[class*="username"],[class*="author"]') || {}).textContent || ''
    ).trim();
    if (!author) {
      var roleMentionEl = el.querySelector(
        '[class*="roleMention"],[class*="mention"][role="button"]'
      );
      if (roleMentionEl) author = roleMentionEl.textContent.replace(/^@/, '').trim();
    }

    // --- Content ---
    var content = (
      (el.querySelector('[id^="message-content-"]') || {}).textContent
      || (el.querySelector('[class*="messageContent"]') || {}).textContent
      || ''
    ).trim();

    // Prepend role mention text so whitelist matching always finds the analyst name
    var roleMentionEls = el.querySelectorAll(
      '[class*="roleMention"],[class*="mention"][role="button"]'
    );
    if (roleMentionEls.length) {
      var roleText = Array.from(roleMentionEls)
        .map(function(r) { return r.textContent.trim(); })
        .filter(Boolean).join(' ');
      if (roleText && content.indexOf(roleText) === -1) {
        content = roleText + (content ? ' ' + content : '');
      }
    }

    // Always append embed text — signals arrive as an embed alongside a role ping.
    var embeds = el.querySelectorAll('[class*="embed"]');
    if (embeds.length) {
      var embedText = Array.from(embeds)
        .map(function(e) { return e.textContent; })
        .join(' ').replace(/\s+/g, ' ').trim();
      if (embedText) {
        content = content ? content + ' ' + embedText : embedText;
      }
    }

    if (!id || !content) return null;

    var timeEl = el.querySelector('time');
    var time   = timeEl ? timeEl.getAttribute('datetime') : '';
    var image_url = '';
    var imgs = el.querySelectorAll('img');
    for (var ii = 0; ii < imgs.length; ii++) {
      var s = imgs[ii].src
           || imgs[ii].getAttribute('data-safe-src')
           || imgs[ii].getAttribute('data-src') || '';
      if (s.indexOf('/attachments/') > -1 || s.indexOf('/external/') > -1) {
        image_url = s; break;
      }
    }
    if (!image_url) {
      var a = el.querySelector('a[href*="/attachments/"],a[href*="/external/"]');
      if (a) image_url = a.href || '';
    }

    // --- Server ---
    var headerEl = el.querySelector('[class*="headerText"]')
                || el.querySelector('[class*="guildName"]')
                || el.querySelector('[class*="serverName"]');
    var header   = headerEl ? headerEl.textContent.trim() : '';
    var server   = header.split(/\s[·•|]\s/)[0].trim();
    if (!server && content) {
      var sm = content.match(/([A-Za-z][\w ]{2,30})\s*[|·•]\s*#?\w/);
      if (sm) server = sm[1].trim();
    }

    return {id:id, author:author, content:content, time:time,
            image_url:image_url, server:server};
  }

  function isCard(node) {
    return node.nodeType === 1 &&
      (node.matches('[class*="notificationItem"],[class*="notification-item"]') ||
       (node.matches('[class*="container_"]') &&
        !!node.querySelector('[id^="message-content-"],[class*="messageContent"],[class*="embed"]')));
  }

  var observer = new MutationObserver(function(mutations) {
    mutations.forEach(function(mut) {
      mut.addedNodes.forEach(function(node) {
        var candidates = [];
        if (isCard(node)) {
          candidates.push(node);
        } else if (node.nodeType === 1) {
          var found = node.querySelectorAll(
            '[class*="notificationItem"],[class*="notification-item"],[class*="container_"]'
          );
          found.forEach(function(n) {
            if (n.querySelector('[id^="message-content-"],[class*="messageContent"],[class*="embed"]'))
              candidates.push(n);
          });
        }
        candidates.forEach(function(el) {
          var card = extractCard(el);
          if (card) { window.__botNotify(JSON.stringify(card)); }
        });
      });
    });
  });

  var panel = document.querySelector('[class*="inbox"]')
           || document.querySelector('[aria-label*="nbox"]')
           || document.querySelector('[class*="notif"]');

  if (!panel) return 'no_panel';
  observer.observe(panel, {childList: true, subtree: true});
  window.__botObserver = observer;
  return 'ok';
})()
"""


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

    # Anchor on each message-content node and walk up to the SMALLEST card that
    # still holds exactly one message. Discord's inbox nests `container_` divs, so
    # selecting containers directly grabs a giant wrapper whose text/embeds/images
    # bleed across every notification (one "card" came back as 10k chars spanning
    # the whole inbox). Anchoring per message keeps content, image and author
    # correctly scoped to a single notification.
    js = r"""
    (function() {
      var contents = Array.from(document.querySelectorAll(
        '[id^="message-content-"], [class*="messageContent"]'
      ));
      var seen = {};
      var out = [];

      contents.slice(0, 60).forEach(function(mc) {
        // Walk up until the parent would merge a second message into the card.
        var node = mc, card = mc.parentElement;
        for (var up = 0; up < 8 && node; up++) {
          node = node.parentElement;
          if (!node) break;
          var n = node.querySelectorAll(
            '[id^="message-content-"], [class*="messageContent"]'
          ).length;
          if (n >= 2) break;
          card = node;
        }

        var id = mc.id || ((card.querySelector('[id^="message-content-"]') || {}).id) || '';
        if (!id || seen[id]) return;
        seen[id] = 1;

        var content = (mc.textContent || '').trim();

        // Role mentions in THIS card (analyst ping) — prepend for whitelist match.
        var rms = card.querySelectorAll(
          '[class*="roleMention"], [class*="mention"][role="button"]'
        );
        if (rms.length) {
          var roleText = Array.from(rms)
            .map(function(r) { return r.textContent.trim(); })
            .filter(Boolean).join(' ');
          if (roleText && content.indexOf(roleText) === -1) {
            content = roleText + (content ? ' ' + content : '');
          }
        }

        // Embed text in THIS card (Unity signals arrive as an embed).
        var embeds = card.querySelectorAll('[class*="embed"]');
        if (embeds.length) {
          var et = Array.from(embeds)
            .map(function(e) { return e.textContent; })
            .join(' ').replace(/\s+/g, ' ').trim();
          if (et) content = content ? content + ' ' + et : et;
        }

        // Author: username/author element, else the card's role mention.
        var author = (
          (card.querySelector('[class*="username"], [class*="author"]') || {}).textContent || ''
        ).trim();
        if (!author && rms.length) author = rms[0].textContent.replace(/^@/, '').trim();

        var timeEl = card.querySelector('time');
        var time = timeEl ? timeEl.getAttribute('datetime') : '';

        // Chart image scoped to THIS card (/attachments/ upload or /external/ proxy).
        var image_url = '';
        var imgs = card.querySelectorAll('img');
        for (var i = 0; i < imgs.length; i++) {
          var s = imgs[i].src
               || imgs[i].getAttribute('data-safe-src')
               || imgs[i].getAttribute('data-src') || '';
          if (s.indexOf('/attachments/') > -1 || s.indexOf('/external/') > -1) {
            image_url = s; break;
          }
        }
        if (!image_url) {
          var a = card.querySelector('a[href*="/attachments/"], a[href*="/external/"]');
          if (a) image_url = a.href || '';
        }

        // Server/guild from the card header.
        var headerEl = card.querySelector('[class*="headerText"]')
                    || card.querySelector('[class*="guildName"]')
                    || card.querySelector('[class*="serverName"]');
        var header = headerEl ? headerEl.textContent.trim() : '';
        var server = header.split(/\s[·•|]\s/)[0].trim();

        if (content) {
          out.push({id: id, author: author, content: content,
                    time: time, image_url: image_url, server: server});
        }
      });
      return out;
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


# ---------------------------------------------------------------------------
# Inbox health tracking (sweep path)
# ---------------------------------------------------------------------------
# A sweep that parses zero cards means EITHER the selectors broke (the inbox
# DOM changed and we're now blind) OR the panel genuinely failed to open. Both
# are "we can't see signals" states. We count consecutive blind sweeps and the
# last time a sweep successfully parsed at least one card, so bot.py can force a
# hard recovery and fire a de-duped alert before signals go silently missing.
_consec_stale_polls = 0
_last_card_seen_ts: float | None = None


def inbox_health() -> dict:
    """Health of the sweep-path inbox scrape.

    Returns:
        consec_stale_polls: consecutive sweeps that parsed zero cards.
        last_card_seen_ts:  epoch of the last sweep that parsed >=1 card
                            (None if none yet this run).
    """
    return {
        "consec_stale_polls": _consec_stale_polls,
        "last_card_seen_ts": _last_card_seen_ts,
    }


def poll_inbox(seen_ids: set[str], server_filter: str = "") -> list[dict]:
    """
    Opens the inbox, scrapes notification cards, and returns all unseen messages.

    server_filter: if non-empty, only messages whose extracted 'server' field
    contains this string (case-insensitive) are returned.  Set via
    DISCORD_SERVER_FILTER in .env (e.g. "Unity Academy").
    Whitelist filtering is NOT done here — that happens in bot.py after parsing.

    Each returned dict: {id, author, content, time, image_url, server}
    """
    global _consec_stale_polls, _last_card_seen_ts

    ensure_discord_open()

    if not open_inbox():
        log.warning("Failed to open Discord inbox — skipping poll")
        _consec_stale_polls += 1
        return []

    time.sleep(1.0)  # allow panel animation to settle

    messages = _parse_notifications_js()
    if not messages:
        log.warning("No notifications found — selectors may be stale. Dumping panel HTML for diagnosis:")
        log.warning(_dump_inbox_panel_html())
        _consec_stale_polls += 1
        return []

    # Parsed at least one card -> selectors are healthy this sweep.
    _consec_stale_polls = 0
    _last_card_seen_ts = time.time()

    sf = server_filter.lower().strip()

    # Log unique server names seen so we can verify the filter value
    servers_seen = {m.get("server", "") for m in messages if m.get("server")}
    if servers_seen:
        log.info(f"Servers in inbox: {sorted(servers_seen)}")
    elif sf:
        log.warning("Server field empty on all cards — DISCORD_SERVER_FILTER may be blocking everything")

    new_msgs = []
    for msg in messages:
        if not msg.get("id"):
            continue
        if msg["id"] in seen_ids:
            continue
        if sf and sf not in msg.get("server", "").lower():
            continue
        new_msgs.append(msg)

    log.info(f"Inbox poll: {len(messages)} total, {len(new_msgs)} unseen"
             + (f" from '{server_filter}'" if sf else ""))
    return new_msgs


# ---------------------------------------------------------------------------
# Real-time listener
# ---------------------------------------------------------------------------

class NotificationListener:
    """
    Maintains a persistent CDP WebSocket and a MutationObserver injected into
    the Discord inbox panel.  New notification cards are delivered instantly
    via self.queue (a threading.Queue of message dicts).

    Usage:
        listener = NotificationListener(seen_ids, server_filter="Unity Academy")
        listener.start()
        while True:
            try:
                msg = listener.queue.get(timeout=30)
                process(msg)
            except queue.Empty:
                listener.reinject()   # re-inject observer periodically
    """

    def __init__(self, seen_ids: set, server_filter: str = ""):
        self.queue: queue.Queue = queue.Queue()
        self.seen_ids = seen_ids          # shared reference — grows as msgs are marked
        self.server_filter = server_filter.lower().strip()
        self._ws = None
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._stopped = False
        self._msg_id = 100               # CDP message ID counter (avoid clashing with _eval_js)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Connect, register the JS binding, inject observer, start recv thread."""
        ws_url = _get_discord_ws_url()
        if not ws_url:
            raise RuntimeError("NotificationListener: no Discord tab found")
        # Use a long recv timeout so the socket stays open during quiet periods.
        # Timeouts are caught in _recv_loop and treated as normal (not errors).
        self._ws = websocket.create_connection(ws_url, timeout=90)
        self._cdp("Runtime.enable")
        self._cdp("Runtime.addBinding", {"name": "__botNotify"})
        time.sleep(0.3)   # let Chrome register the binding before we inject JS
        self._do_inject()
        self._thread = threading.Thread(
            target=self._recv_loop, daemon=True, name="cdp-listener"
        )
        self._thread.start()
        log.info("Real-time notification listener active")

    def reinject(self):
        """Re-open inbox and re-inject observer — call every few minutes as a safety net."""
        try:
            open_inbox()
            time.sleep(1.0)
            self._do_inject()
            log.debug("Observer re-injected")
        except Exception as e:
            log.warning(f"Observer reinject failed: {e}")

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self):
        self._stopped = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cdp(self, method: str, params: dict | None = None) -> int:
        mid = self._msg_id
        self._msg_id += 1
        payload = json.dumps({"id": mid, "method": method, "params": params or {}})
        with self._send_lock:
            self._ws.send(payload)
        return mid

    def _do_inject(self):
        self._cdp("Runtime.evaluate", {
            "expression": _OBSERVER_JS,
            "returnByValue": True,
        })

    def _handle_card(self, card: dict):
        if not card.get("content") or not card.get("id"):
            return
        server = card.get("server", "")
        if self.server_filter:
            if self.server_filter not in server.lower():
                log.debug(f"Listener: dropped card (server={server!r} doesn't match filter)")
                return
        if card["id"] in self.seen_ids:
            return
        log.debug(f"Listener: queuing card from server={server!r} author={card.get('author','')!r}")
        self.queue.put(card)

    def _recv_loop(self):
        import socket as _socket
        _timeout_types = (
            websocket.WebSocketTimeoutException,
            _socket.timeout,
            TimeoutError,
        )
        # Consecutive non-fatal recv errors. A socket that keeps raising the same
        # error (WinError 10054 "forcibly closed", "Connection timed out") is
        # effectively dead — recv() would spin forever logging once per 2s. We
        # rate-limit the log and force a reconnect once it crosses the threshold,
        # instead of grinding on a corpse socket (the historical churn source).
        consec_errs = 0
        while not self._stopped:
            try:
                raw = self._ws.recv()
                consec_errs = 0
                data = json.loads(raw)
                if data.get("method") == "Runtime.bindingCalled":
                    p = data.get("params", {})
                    if p.get("name") == "__botNotify":
                        try:
                            card = json.loads(p.get("payload", "{}"))
                            self._handle_card(card)
                        except Exception as e:
                            log.debug(f"Listener card parse error: {e}")
            except _timeout_types:
                # No CDP event in the recv window — normal during quiet periods.
                # Send a lightweight keepalive so Chrome doesn't drop the connection.
                consec_errs = 0
                try:
                    self._cdp("Runtime.evaluate", {
                        "expression": "1",
                        "returnByValue": True,
                    })
                except Exception:
                    pass
                continue
            except (websocket.WebSocketConnectionClosedException,
                    ConnectionResetError, ConnectionAbortedError, OSError) as e:
                # The socket is gone (Chrome closed the tab, network reset, WinError
                # 10054, etc.). recv() would keep raising forever — so RECONNECT,
                # don't just sleep-and-retry on a dead socket.
                if self._stopped:
                    return
                log.warning(f"CDP listener connection lost ({e}) — reconnecting")
                if not self._reconnect():
                    return
                consec_errs = 0
                continue
            except Exception as e:
                if self._stopped:
                    return
                consec_errs += 1
                # Rate-limit: log the first, then every 50th, to avoid log floods.
                if consec_errs == 1 or consec_errs % 50 == 0:
                    log.warning(f"CDP listener recv error (x{consec_errs}): {e}")
                if consec_errs >= 5:
                    # Socket is wedged — stop spinning and rebuild it.
                    log.warning("CDP listener: repeated recv errors — forcing reconnect")
                    if not self._reconnect():
                        return
                    consec_errs = 0
                    continue
                time.sleep(2)

    def _reconnect(self) -> bool:
        """Rebuild the CDP socket and re-inject the observer.

        Returns True if a fresh connection was established (caller resumes its
        recv loop), False if all attempts were exhausted. Iterative by design —
        it never calls _recv_loop(), so reconnect cycles cannot grow the stack.
        """
        for attempt in range(1, 7):
            wait = 5 * attempt
            log.info(f"CDP reconnect attempt {attempt}/6 in {wait}s...")
            time.sleep(wait)
            try:
                ws_url = _get_discord_ws_url()
                if not ws_url:
                    continue
                self._ws = websocket.create_connection(ws_url, timeout=90)
                self._cdp("Runtime.enable")
                self._cdp("Runtime.addBinding", {"name": "__botNotify"})
                time.sleep(0.3)
                open_inbox()
                time.sleep(1.0)
                self._do_inject()
                log.info("CDP listener reconnected successfully")
                return True
            except Exception as e:
                log.warning(f"Reconnect attempt {attempt} failed: {e}")
        log.error("CDP listener could not reconnect after 6 attempts")
        return False
