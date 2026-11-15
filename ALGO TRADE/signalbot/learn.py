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
from pathlib import Path

from .notify import coin, usd
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
}
COMBOS = [("setup", "coin"), ("side", "coin"), ("setup", "side")]
NAMES = {"setup": "{} setups", "side": "{} trades", "coin": "{}", "score": "strength {}",
         "adx": "ADX {}", "volume": "volume {}", "trend": "{} big trend", "stretch": "price {} from EMA20",
         "stop": "{} stops", "session": "{} session"}


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
def to_record(sig: Signal, source: str) -> dict:
    return {"source": source, "symbol": sig.symbol, "side": sig.side, "tf": sig.tf, "time": sig.time,
            "closed_at": sig.closed_at, "setups": list(sig.setups), "score": sig.score,
            "features": dict(sig.features), "entry": sig.entry, "sl": sig.sl, "tps": list(sig.tps),
            "tp_hit": sig.tp_hit, "realized_r": round(sig.realized_r, 4), "exit_reason": sig.exit_reason,
            "bars": sig.bars, "mfe_r": round(sig.mfe_r, 3), "mae_r": round(sig.mae_r, 3),
            "traded": sig.traded, "qty": sig.qty, "risk_usd": round(sig.risk_usd, 2)}


def record(cfg: dict, sig: Signal) -> None:
    """Append a closed trade to the journal. Never raises: a journal line must not stop trading."""
    path = Path(cfg["alerts"]["log_dir"]) / JOURNAL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(to_record(sig, "live")) + "\n")
    except OSError as e:
        log.warning("could not write the trade journal: %s", e)


def load_journal(log_dir: Path) -> list[dict]:
    out = []
    try:
        lines = (log_dir / JOURNAL).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


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


def build_report(history: list[dict], live: list[dict], rules: list[dict], cfg: dict,
                 failed: list[str] | None = None) -> tuple[str, list[dict]]:
    lc = cfg.get("learn", {})
    min_trades = int(lc.get("min_trades", 30))
    lines = [f"🧠 <b>Review</b>  ·  {time.strftime('%d %b')}", ""]

    # 1. the bot's own live record
    if live:
        s = _stats([t["realized_r"] for t in live])
        lines.append(f"<b>Your live record</b> ({s['n']} closed): {s['win']:.0f}% won · {s['total']:+.2f}R")
        traded = [t for t in live if t.get("traded")]
        if traded:
            dollars = sum(t["realized_r"] * (t.get("risk_usd") or 0) for t in traded)
            lines.append(f"Auto-traded: {len(traded)} · {sum(t['realized_r'] for t in traded):+.2f}R ≈ {usd(dollars)}")
    else:
        lines.append("<b>Your live record</b>: nothing closed yet. Every trade from now on is recorded.")

    # 2. the same rules over a long history
    pairs = ", ".join(coin(p) for p in dict.fromkeys(cfg["pairs"] + cfg.get("watch_pairs", [])))
    pool = [t for t in history if not _blocked(t, rules)]
    lines += ["", f"<b>History check</b> (same rules, last {cfg['backtest']['bars']} × {cfg['entry_tf']}, {pairs})"]
    if failed:
        lines.append(f"<i>no data for {', '.join(failed)}</i>")
    s = _stats([t["realized_r"] for t in history])
    if not s:
        lines.append("No trades in the history.")
        return "\n".join(lines), []
    lines.append(f"{s['n']} trades · {s['win']:.0f}% won · avg {s['avg']:+.2f}R · total {s['total']:+.1f}R")
    if rules:
        p = _stats([t["realized_r"] for t in pool])
        if p:
            lines.append(f"With your {len(rules)} rule(s): {p['n']} trades · avg {p['avg']:+.2f}R · total {p['total']:+.1f}R")

    singles = []
    for cond in _conditions(pool):
        if len(cond) == 1:
            st = _stats([t["realized_r"] for t in pool if matches(t, cond)])
            if st and st["n"] >= 20:
                singles.append((label(cond), st))
    singles.sort(key=lambda x: x[1]["avg"])
    if len(singles) >= 2:
        lines += ["", "✅ <b>Best</b>"] + [_line(n, st) for n, st in reversed(singles[-3:])]
        lines += ["❌ <b>Worst</b>"] + [_line(n, st) for n, st in singles[:3]]

    # 3. suggestions
    sugg = find_suggestions(history, live, rules, min_trades)
    lines += ["", "💡 <b>Suggestions</b>"]
    if not sugg:
        lines.append("None. No condition is losing clearly and consistently enough to act on. That is the "
                     "usual result: acting on weaker evidence would mostly chase bad luck.")
    for k, g in enumerate(sugg, 1):
        live_txt = (f" Your live trades: {g['live_n']}, avg {g['live_avg']:+.2f}R." if g["live_n"] else "")
        lines.append(f"{k}. Stop auto-trading <b>{html.escape(g['label'])}</b>\n"
                     f"   {g['n']} trades, {g['win']:.0f}% won, avg {g['avg']:+.2f}R; losing in the older "
                     f"({g['old_avg']:+.2f}R) and newer ({g['new_avg']:+.2f}R) half.{live_txt}\n"
                     f"   Would have saved ≈ {g['saved']:.1f}R.  → /apply {k}")
    for d in find_drops(history, rules, min_trades):
        lines.append(f"♻️ Rule {d['index']} (<b>{html.escape(d['label'])}</b>) is no longer losing: "
                     f"{d['n']} trades, avg {d['avg']:+.2f}R.  → /drop {d['index']}")

    # 4. trade management observations
    notes = _management(pool + [t for t in live if not _blocked(t, rules)])
    if notes:
        lines += ["", "🔍 <b>Trade management</b> <i>(for your judgement, never applied automatically)</i>"]
        lines += [f"• {n}" for n in notes]
    lines += ["", f"Active rules: {len(rules)}  ·  /rules"]
    return "\n".join(lines), sugg


def review(cfg: dict) -> str:
    """Run the backtest, analyse, save the numbered suggestions for /apply, return the report."""
    from .backtest import run_pair       # imported here: backtest is only needed for a review
    log_dir = Path(cfg["alerts"]["log_dir"])
    history, failed = [], []
    for p in dict.fromkeys(cfg["pairs"] + cfg.get("watch_pairs", [])):
        try:
            history += [to_record(s, "backtest") for s in run_pair(p, cfg)]
        except Exception as e:
            log.warning("review: no history for %s: %s", p, e)
            failed.append(coin(p))
    text, sugg = build_report(history, load_journal(log_dir), cfg.get("learn_rules", []), cfg, failed)
    save_suggestions(log_dir, sugg)
    return text


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
