"""Live scanner: evaluates each pair right after every candle close and tracks open signals.

Efficiency:
  * candles are cached in memory and only new ones are downloaded (CandleCache)
  * the loop wakes ~2 s after each candle close, so signals fire immediately, and otherwise
    every scan_every_sec to follow open trades on 1-minute candles
State survives restarts (logs/signals_state.json).
"""
from __future__ import annotations

import json
import logging
import os
import signal as os_signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from . import tracker
from .data import TF_MS, CandleCache, DataError, candles
from .news import News
from .notify import Notifier, coin, format_signal, px, r_usd, sizing, usd
from .remote import KEYBOARD, Remote
from .strategy import Signal, evaluate, prepare, prepare_trend, trend_index
from .trader import Trader

log = logging.getLogger("signals")

HISTORY = 999          # candles per timeframe: long enough for EMA200 to match the backtest
MAX_SIGNAL_AGE_MS = 10 * 60_000   # after downtime, don't act on a setup from a candle long gone


class State:
    def __init__(self, path: Path):
        self.path = path
        self.open: dict[str, Signal] = {}
        self.closed: list[Signal] = []
        self.last_eval: dict[str, int] = {}     # pair -> close time of last evaluated candle
        self.cool_until: dict[str, int] = {}    # pair -> ms
        self.meta: dict = {}                    # misc (daily summary date, ...)
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            self.open = {k: Signal(**v) for k, v in d.get("open", {}).items()}
            self.closed = [Signal(**v) for v in d.get("closed", [])]
            self.last_eval = d.get("last_eval", {})
            self.cool_until = d.get("cool_until", {})
            self.meta = d.get("meta", {})

    def save(self) -> None:
        """Atomic write. Never raises: a failed save must not stop trading or lose an alert."""
        data = json.dumps({
            "open": {k: asdict(v) for k, v in self.open.items()},
            "closed": [asdict(v) for v in self.closed[-500:]],
            "last_eval": self.last_eval, "cool_until": self.cool_until, "meta": self.meta}, indent=1)
        tmp = self.path.with_suffix(".tmp")
        for attempt in range(3):
            try:
                tmp.write_text(data, encoding="utf-8")
                tmp.replace(self.path)
                return
            except OSError as e:          # e.g. Windows: file briefly open in another program
                if attempt == 2:
                    log.error("could not save state: %s", e)
                time.sleep(0.2)


@dataclass
class ScanResult:
    new: int = 0
    data_ok: int = 0
    data_err: int = 0
    retry_soon: bool = False       # a candle closed but the exchange hadn't published it yet


def _evaluate_pair(pair: str, cfg: dict, cache: CandleCache | None) -> tuple[str, Signal | None, int]:
    tf, src = cfg["entry_tf"], cfg["data_source"]
    get = cache.get if cache else candles
    S = prepare(get(pair, tf, HISTORY, src))
    T = prepare_trend(get(pair, cfg["trend_tf"], HISTORY, src))
    i = len(S.candles) - 1
    j = trend_index(T, S.candles[i].tc)
    return pair, evaluate(S, i, T, j, cfg, pair, tf), S.candles[i].tc


def all_pairs(cfg: dict) -> list[str]:
    return list(dict.fromkeys(cfg["pairs"] + cfg.get("watch_pairs", [])))


def scan_once(cfg: dict, st: State, notifier: Notifier, force: bool = False, trader: Trader | None = None,
              news: News | None = None, cache: CandleCache | None = None) -> ScanResult:
    res = ScanResult()
    now = int(time.time() * 1000)
    bar_ms = TF_MS[cfg["entry_tf"]]
    r = cfg["rules"]
    src = cfg["data_source"]

    # 1. follow open signals on 1m candles (and mirror each event on the exchange)
    for pair, sig in list(st.open.items()):
        try:
            mins = max(5, min(3000, (now - sig.last_t) // 60_000 + 2))
            ks = candles(pair, "1m", int(mins), src)
            res.data_ok += 1
        except DataError as e:
            res.data_err += 1
            log.warning("tracking %s: %s", pair, e)
            continue
        for k in ks:
            for ev in tracker.update(sig, k, cfg["risk"]["fee_pct"], r["max_bars_open"], TF_MS[sig.tf]):
                note = trader.on_event(sig, ev) if trader else ""
                notifier.event(sig, ev, note)
            if sig.status == "closed":
                st.closed.append(st.open.pop(pair))
                st.cool_until[pair] = now + r["cooldown_bars"] * bar_ms
                break

    # 1b. trades you closed yourself in the Bybit app
    if trader:
        for sig, price in trader.sync(list(st.open.values())):
            tracker.close_now(sig, price, now, cfg["risk"]["fee_pct"], "closed on Bybit")
            st.closed.append(st.open.pop(sig.symbol))
            st.cool_until[sig.symbol] = now + r["cooldown_bars"] * bar_ms
            notifier.event(sig, {"kind": "closed", "price": price, "why": "closed on Bybit"})

    # 2. evaluate pairs whose entry candle has closed since we last looked
    if cfg.get("paused") and not force:
        st.save()
        return res
    watch = [p for p in cfg.get("watch_pairs", []) if p not in cfg["pairs"]]
    due = [p for p in all_pairs(cfg) if p not in st.open and st.cool_until.get(p, 0) <= now
           and (force or now >= st.last_eval.get(p, 0) + bar_ms)]
    if due:
        with ThreadPoolExecutor(max_workers=min(8, len(due))) as pool:
            futs = [pool.submit(_evaluate_pair, p, cfg, cache) for p in due]
            for f in futs:
                try:
                    pair, sig, last_close = f.result()
                except DataError as e:
                    res.data_err += 1
                    log.warning("%s", e)
                    continue
                except Exception:
                    log.exception("evaluating a pair failed")
                    continue
                res.data_ok += 1
                if last_close <= st.last_eval.get(pair, 0) and not force:
                    res.retry_soon = True
                    continue
                st.last_eval[pair] = last_close
                if sig and not force and now - last_close > MAX_SIGNAL_AGE_MS:
                    log.info("%s: setup on a candle that closed %d min ago (bot was off?), skipped",
                             pair, (now - last_close) // 60_000)
                    continue
                if sig and pair in watch and sig.score < cfg.get("watch_min_score", 75):
                    sig = None      # low-priority pairs need a stronger setup
                if not sig:
                    continue
                sig.last_t = last_close - 1   # track from the next minute on
                sig.risk_usd = sizing(sig, cfg)["risk_amt"]
                block = news.block_reason(pair, now) if news else ""
                if block:
                    note = (f"🤖 Not auto-traded: {block}." if trader and trader.active
                            else f"⚠️ Careful: {block}. Consider skipping this one.")
                else:
                    note = trader.open(sig, list(st.open.values()), st.closed) if trader else ""
                st.open[pair] = sig
                st.save()                       # persist the exchange link immediately
                notifier.signal(sig, note)
                res.new += 1
    st.save()
    return res


def preview(cfg: dict) -> int:
    """One-off check of all pairs on the last closed candle. Prints only: no state, no Telegram,
    so it is safe to run while the scanner is running."""
    found = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for fut in [pool.submit(_evaluate_pair, p, cfg, None) for p in all_pairs(cfg)]:
            try:
                pair, sig, _ = fut.result()
            except DataError as e:
                print(f"data error: {e}")
                continue
            if sig:
                found += 1
                print("\n" + format_signal(sig, cfg) + "\n")
    return found


def daily_summary(cfg: dict, st: State, trader: Trader | None) -> str:
    today = time.strftime("%Y-%m-%d")
    day = lambda ms: time.strftime("%Y-%m-%d", time.localtime(ms / 1000))
    new_today = sum(1 for s in list(st.open.values()) + st.closed if day(s.time) == today)
    closed = [s for s in st.closed if s.closed_at and day(s.closed_at) == today]
    lines = [f"🌙 <b>Daily summary</b>  ·  {time.strftime('%d %b')}", "",
             f"New signals today: {new_today}"]
    if closed:
        rs = sum(s.realized_r for s in closed)
        dollars = sum(s.realized_r * r_usd(s, cfg) for s in closed)
        lines.append(f"Closed today: {len(closed)}  ·  {sum(s.realized_r > 0 for s in closed)} won  ·  "
                     f"<b>{rs:+.2f}R ≈ {usd(dollars)}</b>")
        lines += [f"   {'✅' if s.realized_r > 0 else '❌'} {coin(s.symbol)} {s.side}  {s.realized_r:+.2f}R  "
                  f"({s.exit_reason})" for s in closed]
    else:
        lines.append("Closed today: none")
    lines.append(f"Open now: {', '.join(f'{coin(p)} {s.side}' for p, s in st.open.items()) or 'none'}")
    if st.closed:
        total = sum(s.realized_r * r_usd(s, cfg) for s in st.closed)
        wins = sum(s.realized_r > 0 for s in st.closed)
        lines.append(f"\nAll time: {len(st.closed)} trades  ·  {wins / len(st.closed) * 100:.0f}% won  ·  "
                     f"{sum(s.realized_r for s in st.closed):+.2f}R ≈ {usd(total)}")
    if trader and trader.ex:
        try:
            w = trader.ex.wallet()
            lines.append(f"💰 {trader.label} equity: ${w['equity']:,.2f}")
        except Exception:
            pass
    return "\n".join(lines)


ALREADY_RUNNING = 2


def _single_instance(path: Path):
    """Hold an OS lock for the whole run so a second launcher can't double-send alerts.
    The lock disappears automatically if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt          # systemd stop / docker stop -> same clean exit as Ctrl+C


def run(cfg: dict) -> int:
    log_dir = Path(cfg["alerts"]["log_dir"])
    instance_lock = _single_instance(log_dir / "scanner.lock")
    if instance_lock is None:
        print("The scanner is ALREADY RUNNING. Not starting a second copy.")
        return ALREADY_RUNNING
    try:
        os_signal.signal(os_signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass
    notifier = Notifier(cfg)
    st = State(log_dir / "signals_state.json")
    cache = CandleCache()
    lock, wake = threading.RLock(), threading.Event()
    trader = Trader(cfg)
    if cfg["trading"].get("env"):
        if not trader.connect():
            log.warning("auto-trading unavailable: %s", trader.error)
    news = News(cfg, log_dir) if cfg.get("news", {}).get("enabled", True) else None
    remote = Remote(cfg, st, notifier, lock, wake, trader, news)
    phone = remote.start()

    rk = cfg["risk"]
    names = lambda ps: ", ".join(coin(p) for p in ps)
    orphans = trader.orphans(list(st.open.values()))
    notifier.info(f"🟢 <b>Scanner started</b>\nWatching {names(cfg['pairs'])} on the {cfg['entry_tf']} chart"
                  + (f"\nWatchlist (silent): {names(cfg['watch_pairs'])}" if cfg.get("watch_pairs") else "")
                  + f"\nCapital ${rk['account_size']:,.0f}  ·  risk {rk['risk_pct']:g}%  ·  {rk.get('leverage', 1)}x"
                  + f"\nOpen trades being tracked: {len(st.open)}"
                  + f"\n{trader.status_line()}"
                  + (f"\n{news.status_line()}" if news else "")
                  + (f"\n⚠️ Bybit positions NOT managed by the bot: {names(orphans)}" if orphans else "")
                  + ("\n⏸ New signals are PAUSED (tap ▶️ Resume)" if cfg.get("paused") else "")
                  + ("\n\n📱 Use the buttons below or send /help to control me." if phone else
                     "\n(Telegram is OFF: console alerts only)"), keyboard=KEYBOARD if phone else None)
    log.info("scanning %s + watchlist %s on %s (trend %s); %d open signals restored. Ctrl+C to stop.",
             ",".join(cfg["pairs"]), ",".join(cfg.get("watch_pairs", [])) or "-",
             cfg["entry_tf"], cfg["trend_tf"], len(st.open))
    for s in st.open.values():
        print(format_signal(s, cfg) + f"\n  [open, {s.tp_hit} TP hit, stop now {px(s.sl_now)}]\n")

    last_beat, down_since, down_alerted = 0.0, 0.0, False
    summary_hour = cfg.get("alerts", {}).get("summary_hour", 22)
    try:
        while True:
            t0 = time.time()
            with lock:
                forced, remote.scan_requested = remote.scan_requested, False
                wake.clear()
                try:
                    res = scan_once(cfg, st, notifier, force=forced, trader=trader, news=news, cache=cache)
                except Exception as e:   # keep the scanner alive on unexpected errors
                    log.exception("scan failed: %s", e)
                    res = ScanResult()
                remote.last_scan = time.time()
            if forced:
                remote.scan_done(res.new)

            # market data health
            if res.data_err and not res.data_ok:
                down_since = down_since or t0
                if not down_alerted and t0 - down_since > 300:
                    down_alerted = True
                    notifier.alert("⚠️ <b>Can't reach market data</b> (Binance and Bybit) for 5+ minutes. "
                                   "No new signals until it's back. Stop-losses on Bybit still protect open trades.")
            elif res.data_ok:
                if down_alerted:
                    notifier.info("✅ Market data is back.")
                down_since, down_alerted = 0.0, False

            # news guard (outside the lock so the phone stays responsive)
            if news:
                with lock:
                    pairs = list(dict.fromkeys(all_pairs(cfg) + list(st.open)))
                try:
                    news.tick(pairs, lambda p: cache.get(p, "1m", 70, cfg["data_source"]), notifier)
                except Exception:
                    log.exception("news check failed")

            # daily summary
            today = time.strftime("%Y-%m-%d")
            if 0 <= summary_hour <= 23 and time.localtime().tm_hour == summary_hour \
                    and st.meta.get("summary_day") != today:
                with lock:
                    st.meta["summary_day"] = today
                    st.save()
                    text = daily_summary(cfg, st, trader)
                notifier.info(text)

            if t0 - last_beat > 900:
                last_beat = t0
                log.info("running%s | open trades: %s", " (PAUSED)" if cfg.get("paused") else "",
                         ", ".join(f"{p} {s.side}" for p, s in st.open.items()) or "none")

            # wake at the next candle close (+2 s), or after scan_every_sec, whichever is first
            bar_s = TF_MS[cfg["entry_tf"]] / 1000
            next_close = (time.time() // bar_s + 1) * bar_s + 2
            deadline = min(t0 + cfg["scan_every_sec"], next_close)
            if res.retry_soon:
                deadline = min(deadline, time.time() + 5)
            while time.time() < deadline and not wake.wait(1.0):
                pass
    except KeyboardInterrupt:
        notifier.info("⏹ <b>Scanner stopped.</b> No new signals until it starts again. "
                      "Stop-losses already on Bybit stay active.")
        return 0
    finally:
        instance_lock.close()


def status(cfg: dict) -> None:
    st = State(Path(cfg["alerts"]["log_dir"]) / "signals_state.json")
    print(f"\nOPEN SIGNALS ({len(st.open)})")
    for s in st.open.values():
        print(format_signal(s, cfg) + f"\n  [open: {s.tp_hit} TP hit, stop now {px(s.sl_now)}, "
                                      f"locked {s.realized_r:+.2f}R]\n")
    if st.closed:
        rs = [s.realized_r for s in st.closed]
        wins = sum(r > 0 for r in rs)
        print(f"TRACK RECORD: {len(rs)} closed, {wins / len(rs) * 100:.0f}% winners, "
              f"total {sum(rs):+.2f}R, avg {sum(rs) / len(rs):+.2f}R")
        for s in st.closed[-10:]:
            print(f"  {time.strftime('%m-%d %H:%M', time.localtime(s.time / 1000))} {s.side:<5} {s.symbol:<9} "
                  f"{s.exit_reason:<22} {s.realized_r:+.2f}R")
