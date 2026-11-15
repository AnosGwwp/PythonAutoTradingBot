"""Follow a signal candle by candle until it hits SL, all TPs, or expires.

Management (identical in live tracking and backtests):
  * close an equal slice at each TP (3 TPs -> 1/3 each)
  * after TP1 the stop moves to breakeven; after TP2 it moves to TP1
  * if a candle touches both stop and target, assume the STOP hit first (conservative)

update() returns a list of event dicts:
  {"kind": "tp", "n": 1, "price": .., "new_sl": ..}
  {"kind": "stop", "after_tp": 0|1|2.., "price": ..}
  {"kind": "done"}                     all targets hit
  {"kind": "expired", "price": .., "bars": ..}
"""
from __future__ import annotations

from .data import Candle
from .strategy import Signal


def update(sig: Signal, k: Candle, fee_pct: float, max_bars: int, bar_ms: int) -> list[dict]:
    if sig.status != "open" or k.t <= sig.last_t:
        return []
    sig.last_t = k.t
    d, risk, n = sig.d, sig.risk, len(sig.tps)
    ev: list[dict] = []
    # bars counted in signal-timeframe units even when tracking on finer candles
    sig.bars = max(sig.bars, int((k.tc - sig.time) // bar_ms))

    hit_sl = (k.l <= sig.sl_now) if d == 1 else (k.h >= sig.sl_now)
    if hit_sl:
        sig.realized_r += sig.remaining * d * (sig.sl_now - sig.entry) / risk
        sig.remaining = 0.0
        ev.append({"kind": "stop", "after_tp": sig.tp_hit, "price": sig.sl_now})
        _close(sig, "stop loss" if sig.tp_hit == 0 else f"stop after TP{sig.tp_hit}", k.tc, fee_pct)
        return ev

    while sig.tp_hit < n:
        tp = sig.tps[sig.tp_hit]
        if (k.h >= tp) if d == 1 else (k.l <= tp):
            part = 1.0 / n
            sig.realized_r += part * d * (tp - sig.entry) / risk
            sig.remaining -= part
            sig.tp_hit += 1
            sig.sl_now = sig.entry if sig.tp_hit == 1 else sig.tps[sig.tp_hit - 2]
            ev.append({"kind": "tp", "n": sig.tp_hit, "price": tp, "new_sl": sig.sl_now})
        else:
            break
    if sig.tp_hit == n:
        sig.remaining = 0.0
        _close(sig, "all targets", k.tc, fee_pct)
        ev.append({"kind": "done"})
        return ev

    if sig.bars >= max_bars:
        sig.realized_r += sig.remaining * d * (k.c - sig.entry) / risk
        sig.remaining = 0.0
        _close(sig, "expired", k.tc, fee_pct)
        ev.append({"kind": "expired", "price": k.c, "bars": sig.bars})
    return ev


def close_now(sig: Signal, price: float, t: int, fee_pct: float, reason: str = "closed by you") -> None:
    """Close whatever is left of the position at `price` (manual exit from the phone)."""
    if sig.status != "open":
        return
    sig.realized_r += sig.remaining * sig.d * (price - sig.entry) / sig.risk
    sig.remaining = 0.0
    _close(sig, reason, t, fee_pct)


def live_r(sig: Signal, price: float) -> float:
    """Banked R plus the open remainder marked at `price` (before fees)."""
    return sig.realized_r + sig.remaining * sig.d * (price - sig.entry) / sig.risk


def _close(sig: Signal, reason: str, t: int, fee_pct: float) -> None:
    # round-trip taker fees expressed in R
    sig.realized_r -= 2 * fee_pct / 100 * sig.entry / sig.risk
    sig.status, sig.exit_reason, sig.closed_at = "closed", reason, t
