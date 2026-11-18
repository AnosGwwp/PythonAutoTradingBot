"""Shared HTTPS client (stdlib only) that keeps connections open between requests.

Opening a new connection costs a TCP + TLS handshake, typically 100-300 ms before the request even
starts. Opening a trade takes about ten Bybit calls in a row, so reusing a warm connection per
host makes orders noticeably faster. Responses are gzip-compressed when the server supports it
(candle downloads shrink several times).

Safety: a request is retried ONCE, only when it failed on a REUSED connection (the server may
have silently closed it after its keep-alive timeout), and never after a timeout. Bybit orders
carry a unique orderLinkId, so even in that case a duplicate order is rejected by Bybit rather
than executed twice.
"""
from __future__ import annotations

import gzip
import http.client
import threading
import time
from urllib.parse import urlsplit

USER_AGENT = "signalbot/1.0"
MAX_IDLE_SEC = 45          # don't reuse a connection idle for longer than this
MAX_IDLE_PER_HOST = 4

_lock = threading.Lock()
_idle: dict[str, list[tuple[http.client.HTTPSConnection, float]]] = {}


class HTTPStatusError(Exception):
    """The server answered, but not with 2xx."""

    def __init__(self, code: int, body: bytes):
        super().__init__(f"HTTP {code}: {body[:200].decode(errors='replace')}")
        self.code, self.body = code, body


def _take(host: str, timeout: float) -> tuple[http.client.HTTPSConnection, bool]:
    now = time.monotonic()
    with _lock:
        lst = _idle.get(host) or []
        while lst:
            conn, t = lst.pop()
            if now - t < MAX_IDLE_SEC:
                return conn, True
            conn.close()
    return http.client.HTTPSConnection(host, timeout=timeout), False


def _give(host: str, conn: http.client.HTTPSConnection) -> None:
    with _lock:
        lst = _idle.setdefault(host, [])
        if len(lst) < MAX_IDLE_PER_HOST:
            lst.append((conn, time.monotonic()))
            return
    conn.close()


def request(method: str, url: str, body: bytes | None = None, headers: dict | None = None,
            timeout: float = 15.0) -> bytes:
    """Return the body of a 2xx response. Raises HTTPStatusError for other statuses, and
    OSError / http.client.HTTPException when the network fails."""
    u = urlsplit(url)
    if u.scheme != "https":
        raise ValueError("https only")
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", **(headers or {})}
    for attempt in (1, 2):
        conn, reused = _take(u.netloc, timeout)
        conn.timeout = timeout
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            r = conn.getresponse()
            data = r.read()
        except TimeoutError:
            conn.close()
            raise                                  # the server may still be working on it
        except (http.client.HTTPException, OSError):
            conn.close()
            if reused and attempt == 1:
                continue                           # stale keep-alive connection: one fresh try
            raise
        if r.will_close:
            conn.close()
        else:
            _give(u.netloc, conn)
        if (r.getheader("Content-Encoding") or "").lower() == "gzip":
            data = gzip.decompress(data)
        if not 200 <= r.status < 300:
            raise HTTPStatusError(r.status, data)
        return data
    raise AssertionError("unreachable")
