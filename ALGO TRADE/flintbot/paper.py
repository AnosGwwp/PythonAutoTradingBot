"""Paper executor: simulates our ladder against REAL public taker fills on Flint.

Fill model (trade-through, conservative):
  a taker SELL at price p fills our bid levels priced >= p; a taker BUY at p fills our
  ask levels priced <= p. We capture `queue_share` of that taker's size (Flint fills makers
  pro-rata at each price), limited by level size, our balances and the inventory cap.
Only fills stamped after our quote was placed count, so we never trade on hindsight.
"""
from __future__ import annotations

from collections import deque

from .config import Config
from .feed import Fill
from .strategy import Ladder, Quote


class PaperExecutor:
    mode = "paper"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = float(cfg.start_base)
        self.quote = float(cfg.start_quote)
        self.start_base = self.base
        self.start_quote = self.quote
        self.ladder: Ladder | None = None
        self.bids: list[list[float]] = []   # [price, remaining]
        self.asks: list[list[float]] = []
        self.placed_ts = float("inf")
        self.active = False
        self._seen: deque[str] = deque(maxlen=5000)
        self._seen_set: set[str] = set()
        self.n_fills = 0
        self.volume_quote = 0.0
        self.fees = 0.0
        self.errors = 0

    # ----- executor interface -----
    async def install(self, ladder: Ladder, q: Quote, ts: float, with_risk: bool) -> None:
        self.ladder = ladder
        await self.update(q, ts)

    async def update(self, q: Quote, ts: float) -> None:
        assert self.ladder is not None
        sizes = [float(s) for s in self.ladder.sizes]
        self.bids = [] if q.pull_bid else [[p, s] for p, s in zip(self.ladder.prices(q.bid_fair, "bid"), sizes)]
        self.asks = [] if q.pull_ask else [[p, s] for p, s in zip(self.ladder.prices(q.ask_fair, "ask"), sizes)]
        self.placed_ts = ts
        self.active = True

    async def pull(self) -> None:
        self.bids, self.asks, self.active = [], [], False

    async def balances(self) -> tuple[float, float]:
        return self.base, self.quote

    def inventory(self) -> float:
        return self.base - self.start_base

    def spread_mult(self) -> float:
        return 1.0

    def own_levels(self) -> tuple[dict[float, float], dict[float, float]]:
        return {}, {}   # paper quotes never appear in the real book

    async def shutdown(self) -> None:
        await self.pull()

    # ----- simulation -----
    def on_fills(self, fills: list[Fill]) -> list[dict]:
        events = []
        if not self.active:
            for f in fills:
                self._mark(f.key)
            return events
        fee_rate = self.cfg.maker_fee_bps / 1e4
        cap = self.cfg.max_inventory_base
        for f in fills:
            if f.key in self._seen_set:
                continue
            self._mark(f.key)
            if f.ts <= self.placed_ts:
                continue
            avail = f.size * self.cfg.queue_share
            if f.side == "sell":   # taker sold into bids
                for lv in self.bids:
                    if avail <= 0 or lv[0] < f.price:
                        break
                    room_cap = cap - self.inventory()
                    room_cash = self.quote / (lv[0] * (1 + fee_rate))
                    qty = min(avail, lv[1], room_cap, room_cash)
                    if qty <= 1e-12:
                        continue
                    self._book_fill("buy", lv[0], qty, fee_rate, f.ts, events)
                    lv[1] -= qty
                    avail -= qty
            else:                  # taker bought from asks
                for lv in self.asks:
                    if avail <= 0 or lv[0] > f.price:
                        break
                    room_cap = cap + self.inventory()
                    qty = min(avail, lv[1], room_cap, self.base)
                    if qty <= 1e-12:
                        continue
                    self._book_fill("sell", lv[0], qty, fee_rate, f.ts, events)
                    lv[1] -= qty
                    avail -= qty
        return events

    def _book_fill(self, side, px, qty, fee_rate, ts, events):
        notional = px * qty
        fee = notional * fee_rate
        if side == "buy":
            self.base += qty
            self.quote -= notional + fee
        else:
            self.base -= qty
            self.quote += notional - fee
        self.n_fills += 1
        self.volume_quote += notional
        self.fees += fee
        events.append({"ev": "fill", "side": side, "price": px, "size": qty, "ts": ts})

    def _mark(self, key: str) -> None:
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)

    def pnl(self, mid: float) -> float:
        """Mark-to-market PnL vs simply holding the starting portfolio (= pure trading edge)."""
        return (self.base - self.start_base) * mid + (self.quote - self.start_quote)
