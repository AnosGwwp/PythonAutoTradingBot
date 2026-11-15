"""Public candle data (no API key): Binance USDT-M futures, falling back to Bybit linear."""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
         "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}
BYBIT_TF = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120",
            "4h": "240", "1d": "D"}


@dataclass(frozen=True)
class Candle:
    t: int        # open time, ms
    tc: int       # close time, ms (exclusive)
    o: float
    h: float
    l: float
    c: float
    v: float


class DataError(RuntimeError):
    pass


def _get(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "signalbot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError,
            json.JSONDecodeError) as e:
        raise DataError(f"{url.split('?')[0]}: {e}") from e


def _binance(symbol: str, tf: str, limit: int, end_ms: int | None) -> list[Candle]:
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={tf}&limit={min(limit, 1500)}"
    if end_ms:
        url += f"&endTime={end_ms}"
    rows = _get(url)
    if isinstance(rows, dict):
        raise DataError(f"binance {symbol}: {rows.get('msg', rows)}")
    return [Candle(int(r[0]), int(r[6]) + 1, float(r[1]), float(r[2]), float(r[3]),
                   float(r[4]), float(r[5])) for r in rows]


def _bybit(symbol: str, tf: str, limit: int, end_ms: int | None) -> list[Candle]:
    url = (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}"
           f"&interval={BYBIT_TF[tf]}&limit={min(limit, 1000)}")
    if end_ms:
        url += f"&end={end_ms}"
    d = _get(url)
    if d.get("retCode") != 0:
        raise DataError(f"bybit {symbol}: {d.get('retMsg')}")
    step = TF_MS[tf]
    rows = sorted(d["result"]["list"], key=lambda r: int(r[0]))
    return [Candle(int(r[0]), int(r[0]) + step, float(r[1]), float(r[2]), float(r[3]),
                   float(r[4]), float(r[5])) for r in rows]


SOURCES = {"binance": _binance, "bybit": _bybit}


def candles(symbol: str, tf: str, n: int, source: str = "binance") -> list[Candle]:
    """Last n CLOSED candles (the still-forming candle is dropped). Paginates for large n."""
    if tf not in TF_MS:
        raise DataError(f"unsupported timeframe {tf}")
    order = [source] + [s for s in SOURCES if s != source]
    last_err = None
    for src in order:
        try:
            return _fetch(SOURCES[src], symbol, tf, n)
        except DataError as e:
            last_err = e
    raise DataError(f"all sources failed for {symbol} {tf}: {last_err}")


class CandleCache:
    """Recent candles kept in memory; only new candles are downloaded.

    Live indicators need long history (EMA200 keeps ~37% of its seed after 100 bars), so the
    scanner asks for 1000 candles. With this cache that is one download at start-up and then
    1-3 candles per update, instead of 2 x 300 candles per pair every hour.
    """

    def __init__(self):
        self._d: dict[tuple, list[Candle]] = {}
        self._lock = threading.Lock()

    def get(self, symbol: str, tf: str, n: int, source: str = "binance") -> list[Candle]:
        key, step = (symbol, tf, source), TF_MS[tf]
        with self._lock:
            have = self._d.get(key)
        now = int(time.time() * 1000)
        if have and len(have) >= n:
            if now < have[-1].tc + step:                 # next candle hasn't closed yet
                return have[-n:]
            missing = (now - have[-1].tc) // step + 1
            if missing <= 500:
                try:
                    fresh = candles(symbol, tf, int(missing) + 2, source)
                except DataError:
                    fresh = []
                if fresh and fresh[0].t <= have[-1].t + step:     # contiguous with what we have
                    merged = ([c for c in have if c.t < fresh[0].t] + fresh)[-max(n, len(have)):]
                    with self._lock:
                        self._d[key] = merged
                    return merged[-n:]
        full = candles(symbol, tf, n, source)
        with self._lock:
            self._d[key] = full
        return full


def price(symbol: str, source: str = "binance") -> float:
    """Latest traded price of a perpetual (raises DataError if the symbol doesn't exist)."""
    def binance():
        d = _get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}", 10)
        if "price" not in d:
            raise DataError(f"binance {symbol}: {d.get('msg', d)}")
        return float(d["price"])

    def bybit():
        d = _get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}", 10)
        if d.get("retCode") != 0 or not d["result"]["list"]:
            raise DataError(f"bybit {symbol}: {d.get('retMsg')}")
        return float(d["result"]["list"][0]["lastPrice"])

    fns = {"binance": binance, "bybit": bybit}
    last_err = None
    for src in [source] + [s for s in fns if s != source]:
        try:
            return fns[src]()
        except (DataError, KeyError, ValueError) as e:
            last_err = e
    raise DataError(f"no price for {symbol}: {last_err}")


def _fetch(fn, symbol: str, tf: str, n: int) -> list[Candle]:
    now = int(time.time() * 1000)
    out: list[Candle] = []
    end = None
    while len(out) < n + 1:
        batch = fn(symbol, tf, n + 1 - len(out), end)
        if not batch:
            break
        out = batch + [c for c in out if c.t > batch[-1].t]
        end = batch[0].t - 1
        if len(batch) < 2:
            break
    closed = [c for c in out if c.tc <= now]
    if len(closed) < min(n, 50):
        raise DataError(f"{symbol} {tf}: only {len(closed)} candles")
    return closed[-n:]
