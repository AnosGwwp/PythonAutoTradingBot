"""Market data from Flint's public MCP endpoint (JSON-RPC over HTTPS, no sign-in).

Standard library only. Each worker thread keeps its own persistent TLS connection,
so after the first request every call skips the handshake.
"""
from __future__ import annotations

import asyncio
import http.client
import itertools
import json
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    bids: tuple[Level, ...]   # best first
    asks: tuple[Level, ...]   # best first
    ts: float                 # exchange timestamp, unix seconds
    slot: int

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None


@dataclass(frozen=True)
class Fill:
    """A public taker fill. side is the TAKER side: 'buy' lifts asks, 'sell' hits bids."""
    key: str
    ts: float
    price: float
    size: float
    side: str


@dataclass(frozen=True)
class PairInfo:
    label: str
    base: str
    quote: str
    base_id: int
    quote_id: int
    program_id: str
    fee_hbps: int
    last_price: float | None
    lot_size: Decimal          # smallest tradable base size


class FeedError(RuntimeError):
    pass


def _decode(raw: bytes) -> dict:
    """JSON-RPC reply as plain JSON, or as a Server-Sent Events stream (we accept both)."""
    text = raw.decode("utf-8").strip()
    if text.startswith(("event:", "data:", "id:", ":")):
        data = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        if not data:
            raise ValueError("event stream without data")
        return json.loads(data[-1])
    return json.loads(text)


def _parsed(fn):
    """Turn a missing field / bad number in a reply into FeedError, which the engine counts as a
    data error, instead of an exception that ends the run."""
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise FeedError(f"{fn.__name__}: unexpected data ({type(e).__name__}: {e})") from e
    wrapper.__name__ = fn.__name__
    return wrapper


class McpFeed:
    def __init__(self, url: str, timeout: float = 10.0):
        u = urlparse(url)
        if u.scheme != "https":
            raise ValueError("MCP url must be https")
        self._host, self._path, self._timeout = u.netloc, u.path or "/", timeout
        self._local = threading.local()
        self._ids = itertools.count(1)

    # ---------- transport ----------
    def _conn(self) -> http.client.HTTPSConnection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = http.client.HTTPSConnection(self._host, timeout=self._timeout)
            self._local.conn = c
        return c

    def _call_sync(self, tool: str, args: dict) -> dict:
        body = json.dumps({
            "jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        })
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Connection": "keep-alive"}
        for attempt in (1, 2):  # one silent retry on a dropped keep-alive socket
            try:
                c = self._conn()
                c.request("POST", self._path, body, headers)
                r = c.getresponse()
                raw = r.read()
                if r.status != 200:
                    raise FeedError(f"{tool}: HTTP {r.status}")
                break
            except (http.client.HTTPException, OSError) as e:
                self._local.conn = None
                if attempt == 2:
                    raise FeedError(f"{tool}: {e}") from e
        try:
            msg = _decode(raw)
            if "error" in msg:
                raise FeedError(f"{tool}: {msg['error']}")
            res = msg["result"]
            text = res["content"][0]["text"]
            if res.get("isError"):
                raise FeedError(f"{tool}: {text}")
            return json.loads(text)
        except (ValueError, KeyError, IndexError, TypeError) as e:   # malformed reply = data error
            raise FeedError(f"{tool}: unexpected response ({type(e).__name__}: {e}): {raw[:120]!r}") from e

    async def call(self, tool: str, args: dict) -> dict:
        return await asyncio.to_thread(self._call_sync, tool, args)

    # ---------- typed helpers ----------
    @_parsed
    async def pair_info(self, label: str) -> PairInfo:
        data = await self.call("spot_market_data_list_pairs", {})
        for p in data.get("pairs", []):
            if p["pair"]["label"] == label:
                lp = p.get("last_price", {}).get("value")
                b = p["base"]
                lot = Decimal(int(b.get("atoms_per_lot", 0))) / (Decimal(10) ** int(b.get("decimals", 0)))
                return PairInfo(
                    label=label, base=p["pair"]["base"], quote=p["pair"]["quote"],
                    base_id=int(p["pair"]["base_id"]), quote_id=int(p["pair"]["quote_id"]),
                    program_id=p["base"].get("program_id", ""),
                    fee_hbps=int(p.get("fee_hbps", 0)),
                    last_price=float(lp) if lp else None,
                    lot_size=lot,
                )
        names = sorted(p["pair"]["label"] for p in data.get("pairs", []))
        raise FeedError(f"pair {label!r} not listed. Some available: {names[:15]}")

    @_parsed
    async def book(self, label: str) -> Book:
        d = (await self.call("spot_market_data_get_book",
                             {"pair": {"label": label}, "level": "FEED_LEVEL_L2"}))["l2"]
        lv = lambda xs: tuple(Level(float(x["price"]["value"]), float(x["size"]["value"])) for x in xs)
        md = d.get("metadata", {})
        ts = int(md.get("ts", {}).get("micros", 0)) / 1e6 or time.time()
        return Book(bids=lv(d.get("bids", [])), asks=lv(d.get("asks", [])),
                    ts=ts, slot=int(md.get("slot", 0)))

    @_parsed
    async def candle_closes(self, label: str, minutes: int = 120) -> list[float]:
        start = int((time.time() - minutes * 60) * 1e6)
        d = await self.call("spot_historical_get_candles", {
            "pair": {"label": label}, "interval": "CANDLE_INTERVAL_1M",
            "start": {"micros": str(start)}})
        cs = sorted(d.get("candles", []), key=lambda c: int(c["ts"]["micros"]))
        return [float(c["close"]["value"]) for c in cs if "close" in c]

    @_parsed
    async def fills_since(self, label: str, since_ts: float, limit: int = 200) -> list[Fill]:
        args = {"pair": {"label": label}, "limit": limit,
                "start": {"micros": str(int(since_ts * 1e6))}}
        d = await self.call("spot_historical_get_fills", args)
        out = []
        for f in d.get("fills", []):
            out.append(Fill(
                key=f"{f.get('signature', '')}:{f['price']['value']}:{f['size']['value']}",
                ts=int(f["ts"]["micros"]) / 1e6,
                price=float(f["price"]["value"]),
                size=float(f["size"]["value"]),
                side="buy" if f.get("side") == "SIDE_BUY" else "sell",
            ))
        out.sort(key=lambda x: x.ts)
        return out
