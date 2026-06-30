"""
Live price stream from BloFin's public WebSocket (wss://<host>/ws/public).

Maintains an in-memory cache of the latest ticker price per instrument so the
resolver and dashboard can peek at near-real-time prices instead of polling REST.

Design notes:
  • The 'tickers' channel only pushes on a price CHANGE. On the LIVE endpoint that
    is continuous; on DEMO it is sparse (prices barely move), so callers should
    treat a stale/absent cache entry as "fall back to REST".
  • get(symbol, max_age) returns a price only if it's fresh enough — otherwise None,
    and the caller (blofin_client.get_market_price) does a REST lookup instead.
  • Runs in a daemon thread with ping/pong keepalive and auto-reconnect, mirroring
    the CDP NotificationListener pattern.
"""

import json
import threading
import time

import websocket

from logger import log


class PriceStream:
    """
    A live last-price cache fed by BloFin's public ticker WebSocket.

    Usage:
        ps = PriceStream("https://openapi.blofin.com")   # or the demo base URL
        ps.start()                                        # connects + starts recv thread
        ps.ensure(["BTC-USDT", "ETH-USDT"])               # subscribe to symbols
        price = ps.get("BTC-USDT", max_age=15)            # cached price, or None if stale
        ps.stop()

    Reusable standalone: yes — only needs `websocket-client` and a logger. The
    `get()`-returns-None-when-stale contract lets callers fall back to REST.
    """

    def __init__(self, base_url: str):
        # Derive the public WS URL from the REST base (demo vs live).
        host = base_url.replace("https://", "").replace("http://", "").rstrip("/")
        self._ws_url = f"wss://{host}/ws/public"
        self._prices: dict[str, tuple[float, float]] = {}   # instId -> (price, epoch)
        self._lock = threading.Lock()
        self._subscribed: set[str] = set()
        self._ws = None
        self._send_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # recv timeout = idle keepalive interval. BloFin drops idle connections after
    # ~30s, so we must ping WELL inside that window — pinging at 30s flaps the
    # connection every ~35s. 15s gives a safe 2x margin.
    _RECV_TIMEOUT = 15

    def start(self):
        """Open the WebSocket and start the background receive thread."""
        self._ws = websocket.create_connection(self._ws_url, timeout=self._RECV_TIMEOUT)
        self._thread = threading.Thread(target=self._recv_loop, daemon=True, name="price-stream")
        self._thread.start()
        log.info(f"Price stream connected: {self._ws_url}")

    @property
    def is_alive(self) -> bool:
        """True while the receive thread is running."""
        return self._thread is not None and self._thread.is_alive()

    def ensure(self, symbols: list[str]):
        """Subscribe to any symbols not already subscribed (instId form, e.g. BTC-USDT)."""
        new = [s for s in symbols if s and s not in self._subscribed]
        if not new or not self._ws:
            return
        args = [{"channel": "tickers", "instId": s} for s in new]
        try:
            self._send(json.dumps({"op": "subscribe", "args": args}))
            self._subscribed.update(new)
            log.debug(f"Price stream subscribed: {new}")
        except Exception as e:
            log.debug(f"Price stream subscribe failed: {e}")

    def get(self, symbol: str, max_age: float = 15.0):
        """Return the cached price if it's at most `max_age` seconds old, else None."""
        with self._lock:
            entry = self._prices.get(symbol)
        if not entry:
            return None
        price, ts = entry
        return price if (time.time() - ts) <= max_age else None

    def stop(self):
        """Signal the receive thread to exit and close the WebSocket."""
        self._stopped = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _send(self, payload: str):
        """Thread-safe send on the shared WebSocket."""
        with self._send_lock:
            self._ws.send(payload)

    def _recv_loop(self):
        """Receive ticker frames into the cache; keep-alive on idle; reconnect on drop."""
        import socket as _socket
        timeouts = (websocket.WebSocketTimeoutException, _socket.timeout, TimeoutError)
        while not self._stopped:
            try:
                raw = self._ws.recv()
                if raw == "pong":
                    continue
                msg = json.loads(raw)
                arg = msg.get("arg", {})
                if arg.get("channel") == "tickers" and "data" in msg:
                    data = msg["data"]
                    d = data[0] if isinstance(data, list) and data else data
                    inst = arg.get("instId")
                    last = d.get("last")
                    if inst and last is not None:
                        with self._lock:
                            self._prices[inst] = (float(last), time.time())
            except timeouts:
                # Idle window — keep the socket alive (BloFin drops idle conns ~30s).
                try:
                    self._send("ping")
                except Exception:
                    pass
                continue
            except (websocket.WebSocketConnectionClosedException,
                    ConnectionResetError, ConnectionAbortedError, OSError) as e:
                if self._stopped:
                    return
                # Routine on the demo endpoint (idle drops) — DEBUG, not WARNING,
                # so it doesn't flood the log. REST fallback covers the gap.
                log.debug(f"Price stream closed ({e}) — reconnecting")
                if not self._reconnect():
                    return
                continue
            except Exception as e:
                if not self._stopped:
                    log.debug(f"Price stream recv error: {e}")
                    time.sleep(2)

    def _reconnect(self) -> bool:
        """Re-open the socket and re-subscribe after a drop (exponential backoff).

        Returns True once reconnected (caller resumes its recv loop), False if
        exhausted. Iterative — never calls _recv_loop(), so repeated drops over a
        long run cannot grow the stack.
        """
        for attempt in range(1, 7):
            wait = 5 * attempt
            time.sleep(wait)
            try:
                self._ws = websocket.create_connection(self._ws_url, timeout=self._RECV_TIMEOUT)
                resubscribe = list(self._subscribed)
                self._subscribed.clear()
                self.ensure(resubscribe)
                log.debug("Price stream reconnected")
                return True
            except Exception as e:
                log.debug(f"Price stream reconnect {attempt} failed: {e}")
        log.warning("Price stream could not reconnect — REST fallback remains active")
        return False
