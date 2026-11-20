"""Learn from the bot's own trade history, and SUGGEST changes (you approve each one).

1. JOURNAL  Every closed trade (auto-traded or alert-only) is appended to logs/journal.jsonl with
            the indicator values on the signal candle and how far price moved for / against it.
            The file is never trimmed.
2. REVIEW   Groups trades by condition (setup, side, coin, ADX, volume, trend strength, session...)
            and looks for conditions that keep losing. Live trades alone are far too few to judge
            (about one a day), so the evidence comes from a rolling backtest of the SAME rules over
            the last `backtest.bars` candles. Your live trades are shown alongside and can veto.
3. RULES    A suggestion becomes a rule only when you send /apply N. A rule stops AUTO-TRADES that
            match it. The alert is still sent, tracked and journaled, so the bot keeps checking
            whether the condition has recovered, and suggests dropping the rule when it has.

A suggestion needs ALL of: at least `min_trades` matching trades, an average result clearly below
zero (95% upper bound < 0), losing in BOTH the older and the newer half of the history, not
contradicted by your live trades, and not more than 40% of all trades. Anything weaker is
mostly noise: acting on it would make the bot worse, not better.
"""
from __future__ import annotations

import html
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .notify import coin, r_usd
from .strategy import Signal

log = logging.getLogger("signals")

JOURNAL = "journal.jsonl"
SUGGESTIONS = "learn.json"
Z95 = 1.645
MAX_SHARE = 0.4          # never suggest skipping more than this share of all trades
MAX_SUGGESTIONS = 3


# ---------------------------------------------------------------- conditions
def _band(x, cuts: list[float], labels: list[str]) -> str | None:
    if x is None:
        return None
    for cut, lab in zip(cuts, labels):
        if x < cut:
            return lab
    return labels[-1]


def _f(t: dict, key: str):
    return (t.get("features") or {}).get(key)


DIMS = {   # dimension -> value of a trade record (None = unknown, e.g. trades recorded before features)
    "setup": lambda t: "+".join(sorted(t.get("setups") or [])) or None,
    "side": lambda t: t.get("side"),
    "coin": lambda t: coin(t["symbol"]) if t.get("symbol") else None,
    "score": lambda t: _band(t.get("score"), [65, 75], ["<65", "65-74", "75+"]),
    "adx": lambda t: _band(_f(t, "adx"), [20, 25, 35], ["<20", "20-25", "25-35", "35+"]),
    "volume": lambda t: _band(_f(t, "vol_ratio"), [1.0, 1.5, 2.0], ["<1x", "1-1.5x", "1.5-2x", "2x+"]),
    "trend": lambda t: _band(_f(t, "htf_spread_pct"), [0.3, 1.0], ["weak", "medium", "strong"]),
    "stretch": lambda t: _band(_f(t, "stretch_atr"), [1.0, 2.0], ["<1 ATR", "1-2 ATR", "2+ ATR"]),
    "stop": lambda t: _band(_f(t, "stop_atr"), [1.05, 2.45], ["tight", "normal", "wide"]),
    "session": lambda t: _band(_f(t, "hour_utc"), [8, 16], ["Asia", "Europe", "US"]),
    "news": lambda t: _f(t, "news") if _f(t, "news") in ("with", "against") else None,   # live trades only
}
COMBOS = [("setup", "coin"), ("side", "coin"), ("setup", "side")]
NAMES = {"setup": "{} setups", "side": "{} trades", "coin": "{}", "score": "strength {}",
         "adx": "ADX {}", "volume": "volume {}", "trend": "{} big trend", "stretch": "price {} from EMA20",
         "stop": "{} stops", "session": "{} session", "news": "trades {} the news"}


def cond_id(cond: dict) -> str:
    return "|".join(f"{d}={v}" for d, v in sorted(cond.items()))


def label(cond: dict) -> str:
    return " · ".join(NAMES[d].format(v) for d, v in cond.items())


def matches(t: dict, cond: dict) -> bool:
    return all(DIMS[d](t) == v for d, v in cond.items())


def _blocked(t: dict, rules: list[dict]) -> bool:
    return any(matches(t, r["cond"]) for r in rules)


def matching_rule(sig: Signal, cfg: dict) -> dict | None:
    """The first approved rule that says 'don't auto-trade this', or None."""
    rec = to_record(sig, "live")
    return next((r for r in cfg.get("learn_rules", []) if matches(rec, r["cond"])), None)


# ---------------------------------------------------------------- journal
def trade_id(sig: Signal) -> str:
    return f"{sig.symbol}|{sig.time}"


def to_record(sig: Signal, source: str) -> dict:
    signal_px = sig.features.get("signal_price")
    slip = (sig.d * (sig.entry - signal_px) / sig.risk) if signal_px and sig.risk else None   # + = worse fill
    return {"id": trade_id(sig), "source": source, "symbol": sig.symbol, "side": sig.side, "tf": sig.tf,
            "time": sig.time, "closed_at": sig.closed_at, "setups": list(sig.setups), "score": sig.score,
            "features": dict(sig.features), "entry": sig.entry, "sl": sig.sl, "tps": list(sig.tps),
            "tp_hit": sig.tp_hit, "realized_r": round(sig.realized_r, 4), "exit_reason": sig.exit_reason,
            "bars": sig.bars, "mfe_r": round(sig.mfe_r, 3), "mae_r": round(sig.mae_r, 3),
            "traded": sig.traded, "qty": sig.qty, "risk_usd": round(sig.risk_usd, 2),
            "signal_price": signal_px, "slippage_r": round(slip, 3) if slip is not None else None,
            "events": list(sig.events)[-30:]}


def _append(cfg: dict, row: dict) -> None:
    path = Path(cfg["alerts"]["log_dir"]) / JOURNAL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        log.warning("could not write the trade journal: %s", e)


def record(cfg: dict, sig: Signal) -> None:
    """Append a closed trade to the journal. Never raises: a journal line must not stop trading."""
    if not sig.risk_usd:                     # trades from before $-risk was stored: use the settings
        try:
            sig.risk_usd = r_usd(sig, cfg)
        except (KeyError, ZeroDivisionError, TypeError):
            pass
    _append(cfg, to_record(sig, "live"))


def on_closed(cfg: dict, st, sig: Signal) -> None:
    """Every place that closes a trade calls this: journal it, and for an exchange trade, queue a
    look-up of what Bybit really booked (its records appear a little after the close)."""
    record(cfg, sig)
    if sig.traded:
        st.meta.setdefault("pnl_pending", []).append(
            {"id": trade_id(sig), "symbol": sig.symbol, "from": sig.ordered_at or sig.traded_at, "qty": sig.qty,
             "to": sig.closed_at or int(time.time() * 1000), "since": int(time.time() * 1000)})


def collect_exchange_pnl(cfg: dict, st, trader, now_ms: int) -> None:
    """Attach Bybit's realized P&L (real fills, fees included) to journaled trades."""
    keep = []
    for p in st.meta.get("pnl_pending", []):
        if now_ms - p["since"] < 120_000:            # give Bybit two minutes to book it
            keep.append(p)
            continue
        res = trader.realized_pnl(p["symbol"], p["from"], p["to"], p.get("qty", 0.0)) if trader else None
        if res is not None:
            _append(cfg, {"type": "exchange_pnl", "id": p["id"], "pnl": round(res[0], 4), "fills": res[1],
                          "at": now_ms})
        elif now_ms - p["since"] < 2 * 3_600_000:
            keep.append(p)                           # try again later
        else:
            log.warning("no Bybit P&L found for %s", p["id"])
    st.meta["pnl_pending"] = keep


def backfill(cfg: dict, closed: list[Signal]) -> int:
    """Journal closed trades that ended before the journal existed (they live in the state file)."""
    have = {t.get("id") or f"{t.get('symbol')}|{t.get('time')}" for t in load_journal(Path(cfg["alerts"]["log_dir"]))}
    added = 0
    for sig in closed:
        if sig.status == "closed" and trade_id(sig) not in have:
            if not sig.risk_usd:
                try:
                    sig.risk_usd = r_usd(sig, cfg)
                except (KeyError, ZeroDivisionError, TypeError):
                    pass
            _append(cfg, dict(to_record(sig, "live"), backfilled=True))
            added += 1
    return added


def load_journal(log_dir: Path) -> list[dict]:
    """Closed trades from the journal, with Bybit's realized P&L attached where it is known."""
    trades, pnl = [], {}
    try:
        lines = (log_dir / JOURNAL).read_text(encoding="utf-8").splitlines()
    except OSError:
        return trades
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if row.get("type") == "exchange_pnl":
            pnl[row["id"]] = row["pnl"]
        else:
            trades.append(row)
    for t in trades:
        if t.get("id") in pnl:
            t["exchange_pnl"] = pnl[t["id"]]
    return trades


# ---------------------------------------------------------------- analysis
def _stats(rs: list[float]) -> dict | None:
    n = len(rs)
    if not n:
        return None
    m = sum(rs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "avg": m, "total": sum(rs), "win": sum(x > 0 for x in rs) / n * 100,
            "upper": m + Z95 * sd / math.sqrt(n)}


def _conditions(trades: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for t in trades:
        vals = {d: f(t) for d, f in DIMS.items()}
        for d, v in vals.items():
            if v is not None:
                seen.setdefault(cond_id({d: v}), {d: v})
        for a, b in COMBOS:
            if vals[a] is not None and vals[b] is not None:
                c = {a: vals[a], b: vals[b]}
                seen.setdefault(cond_id(c), c)
    return list(seen.values())


def find_suggestions(history: list[dict], live: list[dict], rules: list[dict], min_trades: int) -> list[dict]:
    """Conditions that lose clearly AND consistently (see module docstring)."""
    pool = [t for t in history if not _blocked(t, rules)]
    live_pool = [t for t in live if not _blocked(t, rules)]
    if len(pool) < min_trades:
        return []
    times = sorted(t["time"] for t in pool)
    mid = times[len(times) // 2]
    found = []
    for cond in _conditions(pool):
        sel = [t for t in pool if matches(t, cond)]
        if len(sel) < min_trades or len(sel) > MAX_SHARE * len(pool):
            continue
        s = _stats([t["realized_r"] for t in sel])
        if s["upper"] >= 0:
            continue
        old = _stats([t["realized_r"] for t in sel if t["time"] < mid])
        new = _stats([t["realized_r"] for t in sel if t["time"] >= mid])
        if not old or not new or old["n"] < 5 or new["n"] < 5 or old["avg"] >= 0 or new["avg"] >= 0:
            continue                                   # not consistent over time: likely noise
        lv = _stats([t["realized_r"] for t in live_pool if matches(t, cond)])
        if lv and lv["n"] >= 5 and lv["avg"] > 0.2:
            continue                                   # your live trades disagree
        found.append({"id": cond_id(cond), "cond": cond, "label": label(cond), "n": s["n"],
                      "avg": s["avg"], "win": s["win"], "saved": -s["total"], "old_avg": old["avg"],
                      "new_avg": new["avg"], "live_n": lv["n"] if lv else 0, "live_avg": lv["avg"] if lv else 0.0})
    found.sort(key=lambda x: -x["saved"])
    chosen: list[dict] = []
    for s in found:      # skip a narrower condition already covered by a chosen broader one
        if any(all(s["cond"].get(d) == v for d, v in c["cond"].items()) for c in chosen):
            continue
        chosen.append(s)
        if len(chosen) == MAX_SUGGESTIONS:
            break
    return chosen


def find_drops(history: list[dict], rules: list[dict], min_trades: int) -> list[dict]:
    """Approved rules whose condition is no longer losing (so the rule now costs trades)."""
    out = []
    for i, r in enumerate(rules):
        others = rules[:i] + rules[i + 1:]
        s = _stats([t["realized_r"] for t in history if matches(t, r["cond"]) and not _blocked(t, others)])
        if s and s["n"] >= min_trades and s["avg"] >= 0:
            out.append({"index": i + 1, "label": r["label"], "n": s["n"], "avg": s["avg"]})
    return out


def _management(trades: list[dict]) -> list[str]:
    """How trades behaved between entry and exit (observations, never applied automatically)."""
    out = []
    stopped = [t for t in trades if t.get("exit_reason") == "stop loss"]
    if len(stopped) >= 10:
        p = sum(t.get("mfe_r", 0) >= 0.5 for t in stopped) / len(stopped) * 100
        out.append(f"{p:.0f}% of trades that hit the full stop were +0.5R in profit first.")
    winners = [t for t in trades if t["realized_r"] > 0]
    if len(winners) >= 10:
        p = sum(t.get("mae_r", 0) >= 0.8 for t in winners) / len(winners) * 100
        out.append(f"{p:.0f}% of winners came within 0.2R of their stop first.")
    if len(trades) >= 10:
        p = sum(t.get("exit_reason") == "expired" for t in trades) / len(trades) * 100
        out.append(f"{p:.0f}% ran out of time (no stop or target within the time limit).")
    return out


def _line(name: str, s: dict) -> str:
    return f"• {html.escape(name)}: {s['n']} trades · {s['win']:.0f}% won · avg {s['avg']:+.2f}R"


# ---------------------------------------------------------------- live trades, explained
def diagnose(t: dict) -> tuple[str, list[str]]:
    """Plain-words verdict for one closed trade, plus mistake tags used for the takeaways."""
    r, why, mfe = t["realized_r"], t.get("exit_reason", ""), t.get("mfe_r", 0) or 0
    tags: list[str] = []
    if r > 0.05:
        text = "✅ hit every target" if why == "all targets" else f"✅ won ({why})"
    elif why == "stop loss":
        if mfe >= 0.5:
            text, tags = f"❌ gave back a profit (was +{mfe:.1f}R first)", ["gave_back"]
        elif (t.get("bars") or 0) <= 2 and mfe < 0.25:
            text, tags = "❌ failed straight away", ["fast_fail"]
        else:
            text = "❌ stopped out"
    elif why in ("stop at breakeven",) or why.startswith("stop after TP"):
        text = f"⚪ {why}"
    elif why == "expired":
        text = "⌛ ran out of time"
    else:
        text = f"✋ {why}"
    if _f(t, "news") == "against":
        text += " · traded against the news"
        tags.append("against_news")
    slip = t.get("slippage_r")
    if slip is not None and slip > 0.15:
        text += f" · filled {slip:.2f}R worse than the signal"
        tags.append("slippage")
    return text, tags


def _takeaways(live: list[dict], gap: float | None, whatifs: list[dict]) -> list[str]:
    losers = [t for t in live if t["realized_r"] <= 0.05]
    tags = [tag for t in losers for tag in diagnose(t)[1]]
    out = []
    if not live:
        out.append("No live trades closed yet. Until there are some, the history test below is the evidence.")
    elif len(live) < 5:
        out.append(f"Only {len(live)} live trade(s) so far: too few to judge the bot on its own trades. "
                   "The history test below carries the evidence for now.")
    best = {w["param"]: w for w in whatifs if w.get("robust")}
    if losers and tags.count("gave_back") >= max(2, len(losers) * 0.4):
        tested = [w for w in whatifs if w["param"] == "breakeven_at_r"]
        if "breakeven_at_r" in best:
            hint = " An earlier breakeven also passed the history test below."
        elif tested:
            hint = (" But on the history test an earlier breakeven did worse overall: it would also have "
                    "stopped out trades that went on to win. So this is normal for the rules, not a mistake to fix.")
        else:
            hint = ""
        out.append(f"{tags.count('gave_back')} of {len(losers)} losses were in profit first.{hint}")
    if losers and tags.count("fast_fail") >= max(2, len(losers) * 0.4):
        hint = " A stricter strength filter passed the history test below." if "min_score" in best else ""
        out.append(f"{tags.count('fast_fail')} of {len(losers)} losses failed straight away, which usually "
                   f"means late entries.{hint}")
    if tags.count("against_news") >= 2:
        out.append(f"{tags.count('against_news')} losses were trades against the news mood.")
    slips = [t["slippage_r"] for t in live if t.get("slippage_r") is not None]
    if len(slips) >= 3 and sum(slips) / len(slips) > 0.1:
        out.append(f"Fills are on average {sum(slips) / len(slips):.2f}R worse than the signal price.")
    if gap is not None and gap <= -0.3:
        out.append(f"Live trades did {abs(gap):.2f}R per trade worse than the history for the same weeks: "
                   "look at fills, news and trades closed by hand.")
    if not out:
        out.append("Nothing unusual: live trades behave like the history says they should.")
    return out


# ---------------------------------------------------------------- what-if tests on history
def _variants(cfg: dict) -> list[tuple[str, str, object]]:
    """(label, settings key, value): alternatives to test against the current rules."""
    r = cfg["rules"]
    out = []
    for v in (60, 65, 70, 75, 80):
        if v != r.get("min_score"):
            out.append((f"min strength {v}", "min_score", v))
    if r.get("breakeven_at_r"):
        out.append(("breakeven only at TP1", "breakeven_at_r", None))
    for v in (0.5, 0.75):
        if v != r.get("breakeven_at_r"):
            out.append((f"stop to entry at +{v}R", "breakeven_at_r", v))
    for v in ([0.8, 1.6, 2.4], [1.0, 1.5, 2.0], [1.5, 3.0]):
        if v != list(r.get("tp_r", [])):
            out.append(("targets " + " / ".join(f"{x:g}R" for x in v), "tp_r", v))
    if len(r.get("setups", [])) > 1:
        for v in r["setups"]:
            out.append((f"{v} setups only", "setups", [v]))
    return out


def what_if(series: dict, cfg: dict, baseline: list[dict], min_trades: int, btc=(None, None)) -> list[dict]:
    """Replay the same history with each variant. 'robust' = more total R than now in BOTH the older
    and the newer half, by a real margin: a change that only helps in one half is luck."""
    from .backtest import simulate
    if len(baseline) < min_trades:
        return []
    mid = sorted(t["time"] for t in baseline)[len(baseline) // 2]
    half = lambda ts, newer: sum(t["realized_r"] for t in ts if (t["time"] >= mid) == newer)
    base_total = sum(t["realized_r"] for t in baseline)
    out = []
    for name, key, value in _variants(cfg):
        c = {**cfg, "rules": {**cfg["rules"], key: value}}
        trades = [to_record(s, "backtest") for sym, (S, T) in series.items() for s in simulate(sym, S, T, c, btc[1], btc[0])]
        if not trades:
            continue
        total = sum(t["realized_r"] for t in trades)
        gain = total - base_total
        robust = (len(trades) >= min_trades and gain >= max(3.0, 0.05 * len(baseline))
                  and half(trades, False) > half(baseline, False) and half(trades, True) > half(baseline, True))
        out.append({"type": "setting", "label": name, "param": key, "value": value, "n": len(trades),
                    "avg": total / len(trades), "gain": gain, "robust": robust,
                    "id": f"set:{key}={json.dumps(value)}"})
    out.sort(key=lambda w: -w["gain"])
    return out


# ---------------------------------------------------------------- the report
def _money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def build_report(history: list[dict], live: list[dict], rules: list[dict], cfg: dict,
                 failed: list[str] | None = None, whatifs: list[dict] | None = None) -> tuple[str, list[dict]]:
    lc = cfg.get("learn", {})
    min_trades = int(lc.get("min_trades", 30))
    whatifs = whatifs or []
    live = sorted(live, key=lambda t: t.get("closed_at") or t.get("time") or 0)
    lines = [f"🧠 <b>Review</b>  ·  {time.strftime('%d %b')}"]

    # 1. the bot's own trades
    lines += ["", "<b>📒 Your trades</b>"]
    gap = None
    if live:
        s = _stats([t["realized_r"] for t in live])
        won = sum(t["realized_r"] > 0.05 for t in live)
        dollars = sum(t["realized_r"] * (t.get("risk_usd") or 0) for t in live)
        lines.append(f"{s['n']} closed  ·  {won} won  ·  {s['n'] - won} lost or flat")
        lines.append(f"Result <b>{_money(dollars)}</b> ({s['total']:+.2f}R, avg {s['avg']:+.2f}R per trade)")
        booked = [t for t in live if t.get("exchange_pnl") is not None]
        if booked:
            est = sum(t["realized_r"] * (t.get("risk_usd") or 0) for t in booked)
            lines.append(f"Bybit booked {_money(sum(t['exchange_pnl'] for t in booked))} on {len(booked)} "
                         f"auto-trade(s); the bot's own estimate for them: {_money(est)}")
        lines.append("")
        for t in live[-8:]:
            when = time.strftime("%d %b", time.localtime((t.get("closed_at") or t["time"]) / 1000))
            verdict, _ = diagnose(t)
            lines.append(f"{'🟢' if t['side'] == 'LONG' else '🔴'} {coin(t['symbol'])} {t['side'].lower()} · {when} · "
                         f"<b>{t['realized_r']:+.2f}R</b>\n      {verdict}")
        recent_bt = [t for t in history if t["time"] >= live[0]["time"]]
        if len(live) >= 5 and len(recent_bt) >= 3:
            gap = s["avg"] - sum(t["realized_r"] for t in recent_bt) / len(recent_bt)
            lines.append(f"\nSame weeks in the history: {len(recent_bt)} trades, avg "
                         f"{sum(t['realized_r'] for t in recent_bt) / len(recent_bt):+.2f}R (live: {s['avg']:+.2f}R)")
        by_news = {v: _stats([t["realized_r"] for t in live if _f(t, "news") == v]) for v in ("with", "against")}
        if sum(x["n"] for x in by_news.values() if x) >= 6:
            lines.append("With the news: " + "  ·  ".join(
                f"{lab} {x['n']} trades, avg {x['avg']:+.2f}R"
                for v, lab in (("with", "with"), ("against", "against")) if (x := by_news[v])))
    else:
        lines.append("Nothing closed yet. Every trade from now on is recorded.")

    # 2. what stands out
    lines += ["", "<b>💡 What stands out</b>"] + [f"• {x}" for x in _takeaways(live, gap, whatifs)]

    # 3. the same rules over a long history, and what-if tests
    pairs = ", ".join(coin(p) for p in dict.fromkeys(cfg["pairs"] + cfg.get("extra_pairs", []) + cfg.get("watch_pairs", [])))
    pool = [t for t in history if not _blocked(t, rules)]
    lines += ["", f"<b>🧪 History test</b>  <i>(same rules, last {cfg['backtest']['bars']} × {cfg['entry_tf']}, {pairs})</i>"]
    if failed:
        lines.append(f"<i>no data for {', '.join(failed)}</i>")
    s = _stats([t["realized_r"] for t in pool])
    if not s:
        lines.append("No trades in the history.")
        return "\n".join(lines), []
    lines.append(f"Now: {s['n']} trades · {s['win']:.0f}% won · avg {s['avg']:+.2f}R · total {s['total']:+.1f}R")
    lines += [f"<i>{x}</i>" for x in _management(pool)[:1]]
    settings = [w for w in whatifs if w["robust"]][:2]
    for w in whatifs[:4]:
        mark = "✅" if w in settings else "·"
        note = "better in both halves" if w["robust"] else ("better, but not in both halves" if w["gain"] > 0
                                                             else "worse")
        lines.append(f"{mark} {html.escape(w['label'])}: avg {w['avg']:+.2f}R, {w['gain']:+.1f}R vs now ({note})")
    singles = []
    for cond in _conditions(pool):
        if len(cond) == 1:
            st = _stats([t["realized_r"] for t in pool if matches(t, cond)])
            if st and st["n"] >= 20:
                singles.append((label(cond), st))
    singles.sort(key=lambda x: x[1]["avg"])
    if len(singles) >= 2:
        lines.append("Best: " + "; ".join(f"{html.escape(n)} {st['avg']:+.2f}R" for n, st in reversed(singles[-2:])))
        lines.append("Worst: " + "; ".join(f"{html.escape(n)} {st['avg']:+.2f}R" for n, st in singles[:2]))
        lines.append("<i>For information only: past best and worst groups (coins especially) often swap "
                     "later, so don't add or drop coins on this. Only ✅ suggestions held up in both halves.</i>")

    # 4. suggestions: skip rules and setting changes, each only with strong evidence
    sugg = [dict(g, type="skip") for g in find_suggestions(history, live, rules, min_trades)] + settings
    lines += ["", "<b>✅ Suggestions</b>"]
    if not sugg:
        lines.append("None this time. Nothing beats the current rules clearly in both halves of the history; "
                     "changing settings on weaker evidence mostly chases luck.")
    for k, g in enumerate(sugg, 1):
        if g["type"] == "skip":
            lines.append(f"{k}. Stop auto-trading <b>{html.escape(g['label'])}</b>: {g['n']} trades, avg "
                         f"{g['avg']:+.2f}R, losing in both halves. Saves ≈ {g['saved']:.1f}R.  /apply {k}")
        else:
            lines.append(f"{k}. Change to <b>{html.escape(g['label'])}</b>: {g['gain']:+.1f}R on the history, "
                         f"better in both halves.  /apply {k}")
    for d in find_drops(history, rules, min_trades):
        lines.append(f"♻️ Rule {d['index']} (<b>{html.escape(d['label'])}</b>) is no longer losing: "
                     f"{d['n']} trades, avg {d['avg']:+.2f}R.  /drop {d['index']}")

    # 5. how complete the data is
    if live:
        booked = sum(t.get("exchange_pnl") is not None for t in live if t.get("traded"))
        lines += ["", f"<i>Data: {len(live)} trades in the journal ({sum(bool(t.get('traded')) for t in live)} "
                      f"on Bybit, {booked} with Bybit's P&L, {sum(bool(t.get('backfilled')) for t in live)} "
                      f"added from older records, {sum(not t.get('features') for t in live)} without indicator data)."
                      f"  /history</i>"]
    lines.append(f"<i>Active rules: {len(rules)}  ·  /rules</i>")
    return "\n".join(lines), sugg


def review(cfg: dict, get=None) -> str:
    """Load history once, replay the current rules and each what-if variant, analyse the journal,
    save the numbered suggestions for /apply, return the report.
    `get` fetches candles (the scanner passes its cache, so only new candles are downloaded)."""
    from .backtest import btc_series, load_series, simulate     # imported here: only needed for a review
    # A long history: suggestions judged on a few months often fail over a year.
    bars = int(cfg.get("learn", {}).get("history_bars", 8760))
    cfg = {**cfg, "backtest": {**cfg.get("backtest", {}), "bars": bars}}
    log_dir = Path(cfg["alerts"]["log_dir"])
    pairs = list(dict.fromkeys(cfg["pairs"] + cfg.get("extra_pairs", []) + cfg.get("watch_pairs", [])))

    def one(p):
        try:
            return load_series(p, cfg, get)
        except Exception as e:
            log.warning("review: no history for %s: %s", p, e)
            return None
    with ThreadPoolExecutor(max_workers=min(6, len(pairs))) as pool:      # pairs in parallel
        loaded = dict(zip(pairs, pool.map(one, pairs)))
    series = {p: v for p, v in loaded.items() if v is not None}
    failed = [coin(p) for p, v in loaded.items() if v is None]
    btc = (None, None)                     # Bitcoin's series, for the Bitcoin filters
    try:
        btc = btc_series(cfg, get, series.get("BTCUSDT"))
    except Exception as e:
        log.warning("review: no Bitcoin data for the BTC filters: %s", e)
    history = [to_record(s, "backtest") for p, (S, T) in series.items()
               for s in simulate(p, S, T, cfg, btc[1], btc[0])]
    rules = cfg.get("learn_rules", [])
    pool_now = [t for t in history if not _blocked(t, rules)]
    whatifs = what_if(series, cfg, pool_now, int(cfg.get("learn", {}).get("min_trades", 30)), btc)
    text, sugg = build_report(history, load_journal(log_dir), rules, cfg, failed, whatifs)
    save_suggestions(log_dir, sugg)
    return text


def history_text(cfg: dict, n: int = 10) -> str:
    """/history: the last closed trades, in plain words, with what Bybit booked."""
    live = sorted(load_journal(Path(cfg["alerts"]["log_dir"])), key=lambda t: t.get("closed_at") or t["time"])
    if not live:
        return "📒 No closed trades in the journal yet."
    lines = [f"📒 <b>Last {min(n, len(live))} trades</b>  <i>(of {len(live)} recorded)</i>"]
    for t in live[-n:][::-1]:
        when = time.strftime("%d %b %H:%M", time.localtime((t.get("closed_at") or t["time"]) / 1000))
        money = t["realized_r"] * (t.get("risk_usd") or 0)
        bybit = f" · Bybit {_money(t['exchange_pnl'])}" if t.get("exchange_pnl") is not None else ""
        lines.append(f"\n{'🟢' if t['side'] == 'LONG' else '🔴'} <b>{coin(t['symbol'])} {t['side'].lower()}</b> · {when}"
                     f"{' · 🤖 Bybit' if t.get('traded') else ' · alert only'}\n"
                     f"<b>{t['realized_r']:+.2f}R" + (f" ≈ {_money(money)}" if money else "") + f"</b>{bybit}\n"
                     f"{diagnose(t)[0]}")
        steps = [e for e in t.get("events", []) if "signal" not in e][-3:]
        if steps:
            lines.append("<i>" + html.escape("  →  ".join(e[6:] for e in steps)) + "</i>")
    return "\n".join(lines)


def save_suggestions(log_dir: Path, sugg: list[dict]) -> None:
    try:
        (log_dir / SUGGESTIONS).write_text(json.dumps({"time": int(time.time()), "suggestions": sugg}),
                                           encoding="utf-8")
    except OSError as e:
        log.warning("could not save suggestions: %s", e)


def load_suggestions(log_dir: Path) -> list[dict]:
    try:
        return json.loads((log_dir / SUGGESTIONS).read_text(encoding="utf-8")).get("suggestions", [])
    except (OSError, ValueError):
        return []
