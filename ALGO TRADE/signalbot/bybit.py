"""Minimal Bybit V5 REST client (stdlib only) for USDT perpetuals in one-way mode.

Keys come from the BYBIT_API_KEY / BYBIT_API_SECRET environment variables, never from files.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

HOSTS = {"demo": "https://api-demo.bybit.com",          # demo trading: real prices, fake money
         "testnet": "https://api-testnet.bybit.com",
         "mainnet": "https://api.bybit.com"}

OK_CODES = {0, 110043, 110025, 34040}   # 0 ok; "leverage/position mode/SL not modified" are fine


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
    def _req(self, method: str, path: str, params: dict | None = None, auth: bool = True) -> dict:
        try:
            return self._req1(method, path, params, auth)
        except BybitError as e:
            if e.code != 10002 or not auth:          # 10002 = our clock is off: resync once, retry
                raise
            self.sync_time()
            return self._req1(method, path, params, auth)

    def _req1(self, method: str, path: str, params: dict | None, auth: bool) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"User-Agent": "signalbot/1.0", "Content-Type": "application/json"}
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
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise BybitError(e.code, e.read()[:200].decode(errors="replace")) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise BybitError(-1, f"network: {e}") from e
        except json.JSONDecodeError as e:
            raise BybitError(-1, f"bad response: {e}") from e
        code = body.get("retCode", -1)
        if code not in OK_CODES:
            raise BybitError(code, body.get("retMsg", ""))
        return body.get("result") or {}

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
