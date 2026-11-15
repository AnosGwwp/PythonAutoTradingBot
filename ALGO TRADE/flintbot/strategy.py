"""Market-making strategy: microprice fair, volatility-scaled spread, inventory skew.

Built around Flint's OracleOffset model:
  * a LADDER of cumulative offsets + sizes is installed on-chain once (expensive), and
  * each tick only a (buy_fair, sell_fair) pair is re-sent (cheap).
Volatility widening and inventory skew are therefore expressed purely through the two
fairs, so the bot almost never has to re-install the ladder.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from .config import Config
from .feed import Book, Level

PULL_BPS = 2000.0  # pushing one side's fair 20% away effectively removes it from the book


def bps(x: float) -> float:
    return x / 1e4


@dataclass(frozen=True)
class Ladder:
    """Installed OracleOffset ladder. offsets are CUMULATIVE, in quote-price units."""
    install_price: float
    offsets: tuple[float, ...]
    sizes: tuple[Decimal, ...]

    def prices(self, fair: float, side: str) -> list[float]:
        out, acc = [], 0.0
        for off in self.offsets:
            acc += off
            out.append(fair - acc if side == "bid" else fair + acc)
        return out


@dataclass(frozen=True)
class Quote:
    fair: float
    reservation: float
    half_spread_bps: float
    sigma_bps: float          # volatility over the horizon, bps
    inv_ratio: float          # inventory / max_inventory, clamped to [-1, 1]
    bid_fair: float
    ask_fair: float
    pull_bid: bool
    pull_ask: bool


@dataclass
class VolEstimator:
    """EWMA of per-second log-return variance, robust to irregular tick spacing.

    Samples at most every `sample_sec`: tick-to-tick microprice changes are mostly
    book-size noise, which would otherwise inflate volatility and over-widen quotes."""
    halflife_sec: float
    sample_sec: float = 5.0
    var_rate: float | None = None   # variance per second
    _last_px: float | None = None
    _last_ts: float | None = None

    def seed(self, closes: list[float], bar_sec: float) -> int:
        """Warm start from historical candle closes so the first minutes aren't blind."""
        rs = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rs) >= 10:
            self.var_rate = sum(r * r for r in rs) / len(rs) / bar_sec
        return len(rs)

    def update(self, px: float, ts: float) -> None:
        if self._last_px is not None and ts - self._last_ts < self.sample_sec:
            return
        if self._last_px is not None and ts > self._last_ts:
            dt = ts - self._last_ts
            r = math.log(px / self._last_px)
            inst = r * r / dt
            a = 1 - 0.5 ** (dt / self.halflife_sec)
            self.var_rate = inst if self.var_rate is None else (1 - a) * self.var_rate + a * inst
        self._last_px, self._last_ts = px, ts

    def sigma(self, horizon_sec: float) -> float:
        return math.sqrt((self.var_rate or 0.0) * horizon_sec)


OWN_MATCH_BPS = 0.5   # on-chain prices are tick-rounded, so our float prices never match exactly


def _strip_own(levels: tuple[Level, ...], own: dict[float, float],
               tol_bps: float = OWN_MATCH_BPS) -> list[Level]:
    """Remove our own resting size from the public book so we don't chase ourselves.

    Each of our levels is matched to the NEAREST book level within tol_bps (our levels sit at
    least step_bps apart, far more than the tolerance) and only our size is subtracted there, so
    other makers' size at that price is kept."""
    sizes = [lv.size for lv in levels]
    for px, sz in own.items():
        best, best_d = None, bps(tol_bps) * px
        for i, lv in enumerate(levels):
            d = abs(lv.price - px)
            if d <= best_d:
                best, best_d = i, d
        if best is not None:
            sizes[best] -= sz
    return [Level(lv.price, s) for lv, s in zip(levels, sizes) if s > 1e-12]


def microprice(book: Book, own_bids: dict[float, float] | None = None,
               own_asks: dict[float, float] | None = None) -> float | None:
    bids = _strip_own(book.bids, own_bids or {})
    asks = _strip_own(book.asks, own_asks or {})
    if bids and asks:
        b, a = bids[0], asks[0]
        if a.price <= b.price:          # crossed/locked: plain mid is safer
            return (a.price + b.price) / 2
        return (b.price * a.size + a.price * b.size) / (a.size + b.size)
    return None


@dataclass
class Strategy:
    cfg: Config
    lot_size: Decimal = Decimal("0")         # base-unit lot; 0 = no rounding
    vol: VolEstimator = field(init=False)
    ladder: Ladder | None = None

    def __post_init__(self):
        self.vol = VolEstimator(self.cfg.vol_halflife_sec, self.cfg.vol_sample_sec)

    # ----- ladder -----
    def build_ladder(self, price: float) -> Ladder:
        c = self.cfg
        offsets = [price * bps(c.min_half_spread_bps)] + [price * bps(c.step_bps)] * (c.levels - 1)
        sizes = []
        for i in range(c.levels):
            s = c.level_size * Decimal(str(c.size_growth ** i))
            if self.lot_size > 0:
                s = (s / self.lot_size).to_integral_value(ROUND_DOWN) * self.lot_size
            sizes.append(max(s, self.lot_size or s))
        self.ladder = Ladder(price, tuple(offsets), tuple(sizes))
        return self.ladder

    def ladder_stale(self, price: float) -> bool:
        if self.ladder is None:
            return True
        drift = abs(price / self.ladder.install_price - 1) * 100
        return drift > self.cfg.reinstall_drift_pct

    # ----- per tick -----
    def compute(self, fair: float, ts: float, inventory_base: float,
                spread_mult: float = 1.0) -> Quote:
        c = self.cfg
        self.vol.update(fair, ts)
        sigma = self.vol.sigma(c.vol_horizon_sec)
        half = max(c.min_half_spread_bps, c.vol_mult * sigma * 1e4)
        half = min(half * spread_mult, c.max_half_spread_bps)

        q = max(-1.0, min(1.0, inventory_base / c.max_inventory_base))
        reservation = fair * (1 - bps(c.skew_bps) * q)    # long -> quote lower to shed inventory

        # Ladder level0 already sits min_half away; widen the fairs by the remainder.
        assert self.ladder is not None, "build_ladder() first"
        base_half_bps = self.ladder.offsets[0] / fair * 1e4
        extra = max(0.0, half - base_half_bps)
        pull_bid, pull_ask = q >= 1.0, q <= -1.0
        bid_fair = reservation * (1 - bps(PULL_BPS if pull_bid else extra))
        ask_fair = reservation * (1 + bps(PULL_BPS if pull_ask else extra))
        return Quote(fair, reservation, half, sigma * 1e4, q, bid_fair, ask_fair, pull_bid, pull_ask)


def needs_requote(new: Quote, sent: Quote | None, age_sec: float, cfg: Config) -> bool:
    """Send a fair update only when it matters: moved > requote_bps, side pulled/restored,
    or heartbeat due (keeps the on-chain fair inside its staleness window)."""
    if sent is None or age_sec >= cfg.heartbeat_sec:
        return True
    if (new.pull_bid, new.pull_ask) != (sent.pull_bid, sent.pull_ask):
        return True
    th = bps(cfg.requote_bps)
    return (abs(new.bid_fair / sent.bid_fair - 1) > th or
            abs(new.ask_fair / sent.ask_fair - 1) > th)


def dec(x: float, sig: int = 10) -> str:
    """Float -> decimal string for the SDK (which rejects floats)."""
    return format(Decimal(f"{x:.{sig}g}").normalize(), "f")
