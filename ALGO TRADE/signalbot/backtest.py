"""Walk-forward backtest of the exact live rules, with no look-ahead.

A signal on candle i is filled at candle i+1's OPEN (what you'd realistically get),
then managed by tracker.update() on each following candle. Fees are included.
Like the auto-trader, it skips a signal whose fill would chase price past the "don't chase" level.
Not modelled: news pauses, max_open_trades / max_same_direction across pairs, and funding fees.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from . import tracker
from .data import TF_MS, candles
from .strategy import CHASE_ATR, evaluate, prepare, prepare_trend, shift_entry, trend_index

log = logging.getLogger("signals")


def run_pair(symbol: str, cfg: dict, get=None) -> list:
    """`get` is how candles are fetched (default: download). The scanner passes its CandleCache,
    so a repeated review only downloads the candles that are new since the last one."""
    S, T = load_series(symbol, cfg, get)
    return simulate(symbol, S, T, cfg)


def load_series(symbol: str, cfg: dict, get=None):
    """History with indicators, downloaded once; simulate() can then replay it with any rules."""
    get = get or candles
    tf, htf = cfg["entry_tf"], cfg["trend_tf"]
    bars = cfg["backtest"]["bars"] + 260
    ratio = max(1, TF_MS[htf] // TF_MS[tf])
    return (prepare(get(symbol, tf, bars, cfg["data_source"])),
            prepare_trend(get(symbol, htf, bars // ratio + 260, cfg["data_source"])))


def simulate(symbol: str, S, T, cfg: dict) -> list:
    tf, r = cfg["entry_tf"], cfg["rules"]
    trades, open_sig, cool_until, j = [], None, -1, 0
    fee, bar_ms = cfg["risk"]["fee_pct"], TF_MS[tf]
    for i in range(len(S.candles)):
        k = S.candles[i]
        if open_sig is not None:
            tracker.update(open_sig, k, fee, r["max_bars_open"], bar_ms, r.get("breakeven_at_r"))
            if open_sig.status == "closed":
                if open_sig.traded:
                    trades.append(open_sig)
                open_sig, cool_until = None, i + r["cooldown_bars"]
            continue
        if i < cool_until or i + 1 >= len(S.candles):
            continue
        j = max(0, trend_index(T, k.tc, j))
        sig = evaluate(S, i, T, j, cfg, symbol, tf)
        if sig:
            fill = S.candles[i + 1].o                # realistic fill at next open
            chased = (sig.d * (fill - (sig.entry + sig.d * CHASE_ATR * sig.atr)) > 0
                      or sig.d * (fill - sig.sl) <= 0)
            if not chased:                           # the auto-trader skips chased ones; live they
                shift_entry(sig, fill)               # are still alerted and tracked, keeping the pair
                sig.traded = True                    # busy, so follow them but leave them out of stats
            open_sig = sig
    return trades


def stats(trades: list) -> dict:
    rs = [t.realized_r for t in trades]
    if not rs:
        return {"n": 0}
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    eq = peak = dd = 0.0
    for x in rs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(rs), "win": len(wins) / len(rs) * 100, "avg": sum(rs) / len(rs), "total": sum(rs),
            "pf": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf"),
            "dd": dd, "tp1": sum(t.tp_hit >= 1 for t in trades) / len(rs) * 100}


def report(cfg: dict) -> None:
    pairs = cfg["pairs"] + [p for p in cfg.get("watch_pairs", []) if p not in cfg["pairs"]]
    print(f"\nBacktest: {cfg['backtest']['bars']} x {cfg['entry_tf']} candles per pair, "
          f"trend filter {cfg['trend_tf']}, fees {cfg['risk']['fee_pct']}%/side, min score {cfg['rules']['min_score']}")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = dict(zip(pairs, pool.map(lambda p: _safe(p, cfg), pairs)))
    hdr = f"{'pair':<10}{'trades':>7}{'win%':>7}{'TP1%':>7}{'avg R':>8}{'total R':>9}{'PF':>6}{'maxDD R':>9}"
    print(hdr + "\n" + "-" * len(hdr))
    allt = []
    for p in pairs:
        ts = results[p]
        if ts is None:
            print(f"{p:<10}  data error")
            continue
        allt += ts
        _row(p, stats(ts))
    print("-" * len(hdr))
    allt.sort(key=lambda t: t.time)          # portfolio equity curve in time order
    _row("ALL", stats(allt))
    for setup in cfg["rules"]["setups"]:
        _row(f" {setup[:9]}", stats([t for t in allt if setup in t.setups]))
    for side in ("LONG", "SHORT"):
        _row(f" {side.lower()}", stats([t for t in allt if t.side == side]))
    s = stats(allt)
    if s["n"]:
        risk_amt = cfg["risk"]["account_size"] * cfg["risk"]["risk_pct"] / 100
        print(f"\nAt {cfg['risk']['risk_pct']}% risk ({risk_amt:g} USDT/trade) that is "
              f"{s['total'] * risk_amt:+,.0f} USDT over the period, worst drawdown {s['dd'] * risk_amt:,.0f} USDT.")
        print("Past results do not guarantee future results. Expectancy (avg R) > 0.1 with 50+ trades is a "
              "reasonable bar before trusting a configuration.")


def _safe(p, cfg):
    try:
        return run_pair(p, cfg)
    except Exception as e:
        log.warning("%s: %s", p, e)
        return None


def _row(name: str, s: dict) -> None:
    if not s.get("n"):
        print(f"{name:<10}{0:>7}")
        return
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    print(f"{name:<10}{s['n']:>7}{s['win']:>7.0f}{s['tp1']:>7.0f}{s['avg']:>+8.2f}{s['total']:>+9.1f}{pf:>6}{s['dd']:>9.1f}")
