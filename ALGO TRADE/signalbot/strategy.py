"""Signal rules.

Only trade WITH the higher-timeframe trend, and only on two classic setups:
  * pullback  — trend intact, price dips into the EMA20 zone and closes back in trend direction
  * breakout  — close beyond the N-bar range on expanding volume with a trending ADX
Stop goes beyond the recent swing (clamped to an ATR band); targets are R-multiples.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import indicators as ind
from .data import Candle


@dataclass
class Series:
    candles: list[Candle]
    o: list[float]
    h: list[float]
    l: list[float]
    c: list[float]
    v: list[float]
    e20: list[float]
    e50: list[float]
    e200: list[float]
    rsi: list[float]
    atr: list[float]
    adx: list[float]
    vsma: list[float]


@dataclass
class Trend:
    candles: list[Candle]
    c: list[float]
    e50: list[float]
    e200: list[float]


@dataclass
class Signal:
    symbol: str
    side: str                 # "LONG" / "SHORT"
    tf: str
    time: int                 # close time of the signal candle, ms
    entry: float
    sl: float
    tps: list[float]
    score: int
    setups: list[str]
    reasons: list[str]
    atr: float
    # --- tracking state ---
    status: str = "open"      # open / closed
    tp_hit: int = 0
    sl_now: float = 0.0
    realized_r: float = 0.0
    remaining: float = 1.0
    bars: int = 0
    last_t: int = 0           # last candle open-time applied
    exit_reason: str = ""
    closed_at: int = 0
    events: list[str] = field(default_factory=list)
    risk_usd: float = 0.0     # $ at risk when the signal was sent (so later setting changes don't rewrite history)
    traded: bool = False      # auto-traded on the exchange
    qty: float = 0.0          # filled size on the exchange
    traded_at: int = 0        # ms

    @property
    def d(self) -> int:
        return 1 if self.side == "LONG" else -1

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)


def prepare(cs: list[Candle]) -> Series:
    o, h, l, c, v = ([x.o for x in cs], [x.h for x in cs], [x.l for x in cs],
                     [x.c for x in cs], [x.v for x in cs])
    return Series(cs, o, h, l, c, v, ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 200),
                  ind.rsi(c), ind.atr(h, l, c), ind.adx(h, l, c), ind.sma(v, 20))


def prepare_trend(cs: list[Candle]) -> Trend:
    c = [x.c for x in cs]
    return Trend(cs, c, ind.ema(c, 50), ind.ema(c, 200))


def trend_index(T: Trend, close_ms: int, start: int = 0) -> int:
    """Index of the last higher-TF candle fully closed by close_ms (no look-ahead)."""
    j = start
    while j + 1 < len(T.candles) and T.candles[j + 1].tc <= close_ms:
        j += 1
    return j if T.candles[j].tc <= close_ms else -1


def htf_bias(T: Trend, j: int) -> int:
    if j < 203 or any(math.isnan(x) for x in (T.e50[j], T.e200[j], T.e50[j - 3])):
        return 0
    if T.c[j] > T.e200[j] and T.e50[j] > T.e200[j] and T.e50[j] > T.e50[j - 3]:
        return 1
    if T.c[j] < T.e200[j] and T.e50[j] < T.e200[j] and T.e50[j] < T.e50[j - 3]:
        return -1
    return 0


def evaluate(S: Series, i: int, T: Trend, j: int, cfg: dict, symbol: str, tf: str) -> Signal | None:
    r = cfg["rules"]
    lb = r["breakout_lookback"]
    if i < max(210, lb + 2) or j < 0:
        return None
    vals = (S.e20[i], S.e50[i], S.e200[i], S.rsi[i], S.rsi[i - 1], S.atr[i], S.adx[i], S.vsma[i - 1])
    if any(math.isnan(x) for x in vals):
        return None
    d = htf_bias(T, j)
    if d == 0:
        return None

    c, o, h, l = S.c, S.o, S.h, S.l
    a, rs, ax = S.atr[i], S.rsi[i], S.adx[i]
    vol_ratio = S.v[i] / S.vsma[i - 1] if S.vsma[i - 1] > 0 else 0.0
    setups, reasons = [], []

    # ---------- pullback ----------
    if "pullback" in r["setups"]:
        if d == 1:
            dip = min(l[i - 2:i + 1])
            ok = (S.e20[i] > S.e50[i] and c[i] > S.e50[i]
                  and dip <= S.e20[i] + 0.25 * a and dip >= S.e50[i] - 0.5 * a
                  and c[i] > S.e20[i] and c[i] > o[i] and c[i] > c[i - 1]
                  and 40 <= rs <= 70 and rs > S.rsi[i - 1] and ax >= 18)
        else:
            pop = max(h[i - 2:i + 1])
            ok = (S.e20[i] < S.e50[i] and c[i] < S.e50[i]
                  and pop >= S.e20[i] - 0.25 * a and pop <= S.e50[i] + 0.5 * a
                  and c[i] < S.e20[i] and c[i] < o[i] and c[i] < c[i - 1]
                  and 30 <= rs <= 60 and rs < S.rsi[i - 1] and ax >= 18)
        if ok:
            setups.append("pullback")
            reasons.append("Price dipped to the 20-EMA and bounced" if d == 1
                           else "Price rallied to the 20-EMA and got rejected")

    # ---------- breakout ----------
    if "breakout" in r["setups"]:
        hi, lo = max(h[i - lb:i]), min(l[i - lb:i])
        stretched = abs(c[i] - S.e20[i]) / a > 2.5
        vol_ok = vol_ratio >= r["breakout_volume_mult"]
        if d == 1:
            ok = c[i] > hi and vol_ok and ax >= 20 and rs < 78 and c[i] > S.e50[i] and not stretched
        else:
            ok = c[i] < lo and vol_ok and ax >= 20 and rs > 22 and c[i] < S.e50[i] and not stretched
        if ok:
            setups.append("breakout")
            reasons.append(f"Broke the {lb}-candle {'high' if d == 1 else 'low'} on {vol_ratio:.1f}× normal volume")

    if not setups:
        return None

    # ---------- score ----------
    score = 40
    score += 10 if ax >= 25 else 5 if ax >= 20 else 0
    score += 15 if vol_ratio >= 2 else 10 if vol_ratio >= 1.2 else 0
    htf_spread = abs(T.e50[j] / T.e200[j] - 1)
    score += 10 if htf_spread > 0.01 else 5 if htf_spread > 0.003 else 0
    sweet = (45 <= rs <= 62) if d == 1 else (38 <= rs <= 55)
    score += 10 if sweet else 0
    score += 10 if len(setups) == 2 else 0
    rng = h[i] - l[i]
    if rng > 0:
        pos = (c[i] - l[i]) / rng
        score += 5 if (pos >= 0.75 if d == 1 else pos <= 0.25) else 0
    score = min(score, 100)
    if score < r["min_score"]:
        return None

    # ---------- stop + targets ----------
    swing = (min(l[i - r["swing_lookback"] + 1:i + 1]) - 0.1 * a if d == 1
             else max(h[i - r["swing_lookback"] + 1:i + 1]) + 0.1 * a)
    dist = abs(c[i] - swing)
    dist = min(max(dist, r["sl_atr_min"] * a), r["sl_atr_max"] * a)
    entry = c[i]
    sl = entry - d * dist
    tps = [entry + d * dist * m for m in r["tp_r"]]

    reasons.insert(0, f"Bigger trend ({cfg['trend_tf']}) is {'UP' if d == 1 else 'DOWN'}")
    reasons.append(f"Momentum turning {'up' if rs > S.rsi[i - 1] else 'down'} (RSI {rs:.0f})")
    reasons.append(f"{'Strong' if ax >= 25 else 'Moderate'} trend strength (ADX {ax:.0f})")
    return Signal(symbol, "LONG" if d == 1 else "SHORT", tf, S.candles[i].tc, entry, sl, tps,
                  score, setups, reasons, a, sl_now=sl, last_t=S.candles[i].t)


def shift_entry(sig: Signal, new_entry: float) -> None:
    """Re-anchor levels to an actual fill price, keeping the same risk distance."""
    dist, d = sig.risk, sig.d
    k = [abs(tp - sig.entry) / dist for tp in sig.tps]
    sig.entry = new_entry
    sig.sl = sig.sl_now = new_entry - d * dist
    sig.tps = [new_entry + d * dist * m for m in k]
