"""Follow a signal candle by candle until it hits SL, all TPs, or expires.

Management (identical in live tracking and backtests):
  * close an equal slice at each TP (3 TPs -> 1/3 each)
  * after TP1 the stop moves to breakeven; after TP2 it moves to TP1
  * if a candle touches both stop and target, assume the STOP hit first (conservative)
  * a TP counts only when price trades THROUGH it (a touch may not fill a resting limit order;
    a trade beyond it means every order at the TP price was filled)
  * if a candle hits a TP and then CLOSES back beyond the stop it just moved, that stop was
    certainly hit after the TP (the close is the candle's last price)

  * optional (rules.breakeven_at_r): stop to entry once price is that many R in profit, before TP1

update() returns a list of event dicts:
  {"kind": "tp", "n": 1, "price": .., "new_sl": ..}
  {"kind": "breakeven", "price": entry, "new_sl": entry, "at_r": ..}     (optional rule above)
  {"kind": "stop", "after_tp": 0|1|2.., "price": .., "breakeven": bool}
  {"kind": "done"}                     all targets hit
  {"kind": "expired", "price": .., "bars": ..}
"""
from __future__ import annotations

from .data import Candle
from .strategy import Signal


def update(sig: Signal, k: Candle, fee_pct: float, max_bars: int, bar_ms: int,
           breakeven_at_r: float | None = None) -> list[dict]:
    """breakeven_at_r: also move the stop to entry once price has gone this many R in our favour
    before TP1 (None = only at TP1, the default rule)."""
    if sig.status != "open" or k.t <= sig.last_t:
        return []
    sig.last_t = k.t
    d, risk, n = sig.d, sig.risk, len(sig.tps)
    ev: list[dict] = []
    # bars counted in signal-timeframe units even when tracking on finer candles
    sig.bars = max(sig.bars, int((k.tc - sig.time) // bar_ms))
    # how far price went for / against us (measured from the entry, in R) - used by learn.py
    sig.mfe_r = max(sig.mfe_r, d * ((k.h if d == 1 else k.l) - sig.entry) / risk)
    sig.mae_r = max(sig.mae_r, d * (sig.entry - (k.l if d == 1 else k.h)) / risk)

    hit_sl = (k.l <= sig.sl_now) if d == 1 else (k.h >= sig.sl_now)
    if hit_sl:
        sig.realized_r += sig.remaining * d * (sig.sl_now - sig.entry) / risk
        sig.remaining = 0.0
        at_entry = sig.tp_hit == 0 and sig.sl_now == sig.entry
        ev.append({"kind": "stop", "after_tp": sig.tp_hit, "price": sig.sl_now, "breakeven": at_entry})
        _close(sig, "stop at breakeven" if at_entry else "stop loss" if sig.tp_hit == 0
               else f"stop after TP{sig.tp_hit}", k.tc, fee_pct)
        return ev

    tp_before, sl_before = sig.tp_hit, sig.sl_now
    while sig.tp_hit < n:
        tp = sig.tps[sig.tp_hit]
        if (k.h > tp) if d == 1 else (k.l < tp):
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

    if (breakeven_at_r and sig.tp_hit == 0 and d * (sig.sl_now - sig.entry) < 0
            and d * ((k.h if d == 1 else k.l) - sig.entry) >= breakeven_at_r * risk):
        sig.sl_now = sig.entry                                       # early breakeven
        ev.append({"kind": "breakeven", "price": sig.entry, "new_sl": sig.entry, "at_r": breakeven_at_r})

    if sig.sl_now != sl_before and d * (k.c - sig.sl_now) <= 0:      # came back through the moved stop
        sig.realized_r += sig.remaining * d * (sig.sl_now - sig.entry) / risk
        sig.remaining = 0.0
        at_entry = sig.tp_hit == 0
        ev.append({"kind": "stop", "after_tp": sig.tp_hit, "price": sig.sl_now, "breakeven": at_entry})
        _close(sig, "stop at breakeven" if at_entry else f"stop after TP{sig.tp_hit}", k.tc, fee_pct)
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
