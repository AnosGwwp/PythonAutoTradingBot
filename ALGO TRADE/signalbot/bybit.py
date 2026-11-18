"""Minimal Bybit V5 REST client (stdlib only) for USDT perpetuals in one-way mode.

Keys come from the BYBIT_API_KEY / BYBIT_API_SECRET environment variables, never from files.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import time
import urllib.parse
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from . import net

log = logging.getLogger("signals")

HOSTS = {"demo": "https://api-demo.bybit.com",          # demo trading: real prices, fake money
         "testnet": "https://api-testnet.bybit.com",
         "mainnet": "https://api.bybit.com"}

# 0 ok; "leverage / position mode / SL not modified" are fine; 110072 "orderLinkId is duplicate"
# means the order was already placed by an earlier attempt of the same request.
OK_CODES = {0, 110043, 110025, 34040, 110072}
UNCERTAIN = -1   # network failure, timeout or HTTP 5xx: Bybit may or may not have executed the request


class BybitError(RuntimeError):
    def __init__(self, code: int, msg: str):
        super().__init__(f"Bybit {code}: {msg}")
        self.code = code


def floor_step(x: float, step: str) -> Decimal:
    s = Decimal(step)
    return (Decimal(str(x)) / s).to_integral_value(ROUND_DOWN) * s


def round_tick(p: float, tick: str) -> Decimal:
    t = Decimal(tick)
    return (Decimal(str(p)) / t).to_integral_value(ROUND_HALF_UP) * t


class Bybit:
    def __init__(self, key: str, secret: str, env: str = "demo", recv_window: int = 10000):
        if env not in HOSTS:
            raise ValueError(f"env must be one of {list(HOSTS)}")
        self.key, self.secret, self.env = key, secret.encode(), env
        self.host, self.recv = HOSTS[env], str(recv_window)
        self.offset_ms = 0
        self._instr: dict[str, dict] = {}

    # ---------------------------------------------------------------- transport
    def _req(self, method: str, path: str, params: dict | None = None, auth: bool = True,
             full: bool = False) -> dict:
        try:
            return self._req1(method, path, params, auth, full)
        except BybitError as e:
            if e.code != 10002 or not auth:          # 10002 = our clock is off: resync once, retry
                raise
            self.sync_time()
            return self._req1(method, path, params, auth, full)

    def _req1(self, method: str, path: str, params: dict | None, auth: bool, full: bool = False) -> dict:
        """`full` returns the whole reply (with retExtInfo) instead of just `result`."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"Content-Type": "application/json"}
        if method == "GET":
            payload = urllib.parse.urlencode(params)
            url, data = f"{self.host}{path}" + (f"?{payload}" if payload else ""), None
        else:
            payload = json.dumps(params, separators=(",", ":"))
            url, data = f"{self.host}{path}", payload.encode()
        if auth:
            ts = str(int(time.time() * 1000) + self.offset_ms)
            sign = hmac.new(self.secret, (ts + self.key + self.recv + payload).encode(), hashlib.sha256).hexdigest()
            headers.update({"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts,
                            "X-BAPI-RECV-WINDOW": self.recv, "X-BAPI-SIGN": sign})
        try:
            body = json.loads(net.request(method, url, body=data, headers=headers, timeout=15))
        except net.HTTPStatusError as e:
            text = e.body[:200].decode(errors="replace")
            if e.code >= 500:   # gateway/server error: the request may still have been executed
                raise BybitError(UNCERTAIN, f"HTTP {e.code} (outcome unknown): {text}") from e
            raise BybitError(e.code, f"HTTP {e.code}: {text}") from e
        except (http.client.HTTPException, OSError) as e:
            raise BybitError(UNCERTAIN, f"network: {e}") from e
        except ValueError as e:
            raise BybitError(UNCERTAIN, f"bad response: {e}") from e
        code = body.get("retCode", -1)
        if code not in OK_CODES:
            raise BybitError(code, body.get("retMsg", ""))
        return body if full else (body.get("result") or {})

    def get(self, path: str, auth: bool = True, **params) -> dict:
        return self._req("GET", path, params, auth)

    def post(self, path: str, body: dict) -> dict:
        return self._req("POST", path, body)

    def sync_time(self) -> None:
        r = self.get("/v5/market/time", auth=False)
        server_ms = int(r.get("timeNano", 0)) // 1_000_000 or int(r["timeSecond"]) * 1000
        self.offset_ms = server_ms - int(time.time() * 1000)

    # ---------------------------------------------------------------- account
    def wallet(self) -> dict:
        r = self.get("/v5/account/wallet-balance", accountType="UNIFIED", coin="USDT")
        a = r["list"][0]
        f = lambda d, k: float(d.get(k) or 0)
        usdt = next((c for c in a.get("coin", []) if c.get("coin") == "USDT"), {})
        if a.get("totalAvailableBalance"):
            avail = f(a, "totalAvailableBalance")
        else:   # isolated margin: Bybit leaves the account totals blank, so compute from USDT
            avail = (f(usdt, "walletBalance") - f(usdt, "totalPositionIM") - f(usdt, "totalOrderIM")
                     - f(usdt, "locked"))
        return {"equity": f(usdt, "equity") or f(a, "totalEquity"), "available": max(0.0, avail),
                "upnl": f(usdt, "unrealisedPnl") or f(a, "totalPerpUPL")}

    def positions(self) -> dict[str, dict]:
        r = self.get("/v5/position/list", category="linear", settleCoin="USDT", limit=200)
        out = {}
        for p in r.get("list", []):
            if float(p.get("size") or 0) > 0:
                out[p["symbol"]] = {"side": p["side"], "size": float(p["size"]),
                                    "avg": float(p.get("avgPrice") or 0),
                                    "upnl": float(p.get("unrealisedPnl") or 0),
                                    "sl": p.get("stopLoss"), "lev": p.get("leverage")}
        return out

    def position(self, symbol: str) -> dict | None:
        r = self.get("/v5/position/list", category="linear", symbol=symbol)
        for p in r.get("list", []):
            if float(p.get("size") or 0) > 0:
                return {"side": p["side"], "size": float(p["size"]), "avg": float(p.get("avgPrice") or 0)}
        return None

    def closed_pnl(self, symbol: str | None = None, start_ms: int | None = None, limit: int = 100) -> list[dict]:
        """Realized P&L records (newest first), one per closing fill, fees included."""
        r = self.get("/v5/position/closed-pnl", category="linear", symbol=symbol, startTime=start_ms,
                     limit=min(limit, 100))
        return r.get("list", [])

    # ---------------------------------------------------------------- market info
    def instrument(self, symbol: str) -> dict:
        if symbol not in self._instr:
            r = self.get("/v5/market/instruments-info", auth=False, category="linear", symbol=symbol)
            if not r.get("list"):
                raise BybitError(-2, f"{symbol} is not a Bybit USDT perpetual")
            i = r["list"][0]
            lot, pf = i["lotSizeFilter"], i["priceFilter"]
            self._instr[symbol] = {"qty_step": lot["qtyStep"], "min_qty": float(lot["minOrderQty"]),
                                   "max_mkt_qty": float(lot.get("maxMktOrderQty") or lot["maxOrderQty"]),
                                   "min_notional": float(lot.get("minNotionalValue") or 0),
                                   "tick": pf["tickSize"], "max_lev": float(i["leverageFilter"]["maxLeverage"])}
        return self._instr[symbol]

    def last_price(self, symbol: str) -> float:
        r = self.get("/v5/market/tickers", auth=False, category="linear", symbol=symbol)
        return float(r["list"][0]["lastPrice"])

    # ---------------------------------------------------------------- trading
    def one_way_mode(self) -> None:
        self.post("/v5/position/switch-mode", {"category": "linear", "coin": "USDT", "mode": 0})

    def set_leverage(self, symbol: str, lev: int) -> None:
        self.post("/v5/position/set-leverage", {"category": "linear", "symbol": symbol,
                                                "buyLeverage": str(lev), "sellLeverage": str(lev)})

    def market(self, symbol: str, side: str, qty: Decimal, reduce: bool = False,
               sl: Decimal | None = None) -> str:
        body = {"category": "linear", "symbol": symbol, "side": side, "orderType": "Market",
                "qty": str(qty), "positionIdx": 0, "reduceOnly": reduce,
                "orderLinkId": self._link()}      # tags orders placed by this bot
        if sl is not None:
            body.update({"stopLoss": str(sl), "slTriggerBy": "LastPrice", "tpslMode": "Full"})
        return self.post("/v5/order/create", body).get("orderId", "")

    def limit(self, symbol: str, side: str, qty: Decimal, price: Decimal, reduce: bool = True) -> str:
        return self.post("/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": side, "orderType": "Limit", "qty": str(qty),
            "price": str(price), "timeInForce": "GTC", "positionIdx": 0, "reduceOnly": reduce,
            "orderLinkId": self._link()}).get("orderId", "")

    def limit_batch(self, symbol: str, side: str, orders: list[tuple[Decimal, Decimal]]) -> list[str | None]:
        """Reduce-only GTC limit orders [(qty, price), ...] in ONE request. Returns an error message
        (or None) per order. Falls back to one request per order if the batch call is refused; the
        same orderLinkIds are reused, so an order that did go through can't be placed twice."""
        reqs = [{"symbol": symbol, "side": side, "orderType": "Limit", "qty": str(q), "price": str(p),
                 "timeInForce": "GTC", "positionIdx": 0, "reduceOnly": True, "orderLinkId": self._link()}
                for q, p in orders]
        try:
            body = self._req("POST", "/v5/order/create-batch", {"category": "linear", "request": reqs}, full=True)
            ext = (body.get("retExtInfo") or {}).get("list") or []
            return [None if i >= len(ext) or ext[i].get("code") in OK_CODES
                    else f"Bybit {ext[i].get('code')}: {ext[i].get('msg')}" for i in range(len(reqs))]
        except BybitError as e:
            log.warning("batch order failed (%s); placing the orders one by one", e)
        out: list[str | None] = []
        for r in reqs:
            try:
                self.post("/v5/order/create", {"category": "linear", **r})
                out.append(None)
            except BybitError as e:
                out.append(str(e))
        return out

    def _link(self) -> str:
        self._n = getattr(self, "_n", 0) + 1
        return f"sb-{int(time.time() * 1000)}-{self._n}"     # letters, digits and '-' only

    def set_sl(self, symbol: str, sl: Decimal) -> None:
        self.post("/v5/position/trading-stop", {"category": "linear", "symbol": symbol, "positionIdx": 0,
                                                "tpslMode": "Full", "stopLoss": str(sl),
                                                "slTriggerBy": "LastPrice"})

    def cancel_all(self, symbol: str) -> None:
        self.post("/v5/order/cancel-all", {"category": "linear", "symbol": symbol})


def detect_env(key: str, secret: str) -> tuple[str | None, dict]:
    """Which Bybit environment accepts this key? Returns (env, wallet) or (None, {})."""
    for env in ("demo", "testnet", "mainnet"):
        try:
            ex = Bybit(key, secret, env)
            ex.sync_time()
            return env, ex.wallet()
        except BybitError:
            continue
    return None, {}
