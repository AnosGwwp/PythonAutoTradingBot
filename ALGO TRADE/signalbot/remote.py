"""Control the scanner from your phone through the same Telegram bot.

Security: only messages from YOUR chat (TELEGRAM_CHAT_ID) are obeyed; anything else is ignored.
Changes made from the phone are saved to logs/settings.json and override signals.toml, so
they survive restarts. Delete that file to go back to signals.toml.
"""
from __future__ import annotations

import copy
import html
import json
import logging
import re
import threading
import time
import urllib.parse
from pathlib import Path

from . import learn, net, tracker
from .data import TF_MS, DataError, price
from .notify import away, coin, money, px, r_usd, stop_label, trade_progress, usd

log = logging.getLogger("signals")

TREND_FOR = {"15m": "1h", "30m": "4h", "1h": "4h", "2h": "1d", "4h": "1d"}

# setting name -> (config section or None, field)
KEYS = {"pairs": (None, "pairs"), "watch_pairs": (None, "watch_pairs"), "extra_pairs": (None, "extra_pairs"),
        "entry_tf": (None, "entry_tf"),
        "trend_tf": (None, "trend_tf"), "paused": (None, "paused"), "min_score": ("rules", "min_score"),
        "account_size": ("risk", "account_size"), "risk_pct": ("risk", "risk_pct"),
        "leverage": ("risk", "leverage"), "autotrade": ("trading", "enabled"),
        "learn_rules": (None, "learn_rules"), "tp_r": ("rules", "tp_r"),
        "breakeven_at_r": ("rules", "breakeven_at_r"), "setups": ("rules", "setups")}
REVIEW_SETTINGS = {"min_score", "tp_r", "breakeven_at_r", "setups"}   # what /apply may change

KEYBOARD = {"keyboard": [[{"text": "📊 Status"}, {"text": "📈 Trades"}],
                         [{"text": "💰 Balance"}, {"text": "🔍 Scan now"}],
                         [{"text": "⏸ Pause"}, {"text": "▶️ Resume"}],
                         [{"text": "📰 News"}, {"text": "⚙️ Settings"}, {"text": "❓ Help"}]],
            "resize_keyboard": True, "is_persistent": True}
BUTTON_WORDS = {"status": "/status", "trades": "/trades", "scan now": "/scan", "settings": "/settings",
                "pause": "/pause", "resume": "/resume", "help": "/help", "balance": "/balance",
                "news": "/news"}

COMMAND_MENU = [
    ("status", "Is it running, open trades, results"),
    ("trades", "Open trades with live profit/loss"),
    ("balance", "Bybit balance and positions"),
    ("news", "Upcoming big events, price shocks, headlines"),
    ("summary", "Today's results"),
    ("review", "Learn from the trade history: what works, what loses"),
    ("history", "Last closed trades, explained, with Bybit's real P&L"),
    ("rules", "Your approved 'don't auto-trade' rules"),
    ("apply", "Approve a review suggestion, e.g. /apply 1"),
    ("drop", "Remove a rule, e.g. /drop 1"),
    ("autotrade", "Auto-trading on Bybit: /autotrade on | off"),
    ("closeall", "Close ALL bot trades on Bybit now"),
    ("scan", "Check all pairs right now"),
    ("pause", "Stop new signals (open trades still tracked)"),
    ("resume", "Send new signals again"),
    ("settings", "Show all settings"),
    ("pairs", "Show your pairs"),
    ("add", "Add a main pair, e.g. /add SUI"),
    ("extra", "Auto-trade a coin at half risk in its own slot, e.g. /extra ZEC"),
    ("watch", "Add a low-priority pair, e.g. /watch DOGE"),
    ("remove", "Remove a pair, e.g. /remove SUI"),
    ("risk", "Risk % per trade, e.g. /risk 1"),
    ("lev", "Leverage, e.g. /lev 10"),
    ("capital", "Your capital in $, e.g. /capital 3000"),
    ("minscore", "Minimum signal strength 40-95, e.g. /minscore 70"),
    ("tf", "Chart timeframe: 15m 30m 1h 2h 4h"),
    ("close", "You exited a trade, e.g. /close BTC"),
    ("help", "All commands"),
]

HELP = """🤖 <b>Phone controls</b>

<b>Buttons</b> below do the common things. Commands:

📊 /status  — running? open trades, results
📈 /trades  — open trades with live P&amp;L
🔍 /scan  — check all pairs now
⏸ /pause  ·  ▶️ /resume  — stop / restart new signals

<b>Bybit auto-trading</b>
💰 /balance  — equity, positions, stops
📰 /news  — big economic events, shocks, headlines
🌙 /summary  — today's results
/autotrade off  — keep alerts, stop placing orders (/autotrade on)
/closeall  — close every bot trade at market NOW
/close BTC  — close one trade (on Bybit too)

<b>Learning</b>
🧠 /review  — your trades explained, history tests, suggestions (also weekly)
📒 /history  — last closed trades with Bybit's real P&amp;L
/apply 1  — approve suggestion 1
/rules  ·  /drop 1  — see / remove your rules

<b>Money</b>
/capital 3000  — your account size in $
/risk 1  — % of capital lost if the stop hits
/lev 10  — leverage you use on the exchange

<b>Pairs</b>
/pairs  — list them
/add SUI  — main pair (loud alert)
/extra ZEC  — extra pair: auto-traded at half risk, one at a time
/watch DOGE  — low-priority (silent, stronger setups only)
/remove SUI

<b>Other</b>
/minscore 70  — fewer, stronger signals
/tf 4h  — chart: 15m 30m 1h 2h 4h
/settings  — show everything"""


# ---------------------------------------------------------------- persisted settings
def settings_path(cfg: dict) -> Path:
    return Path(cfg["alerts"]["log_dir"]) / "settings.json"


def _set(cfg: dict, key: str, value) -> None:
    sec, f = KEYS[key]
    (cfg.setdefault(sec, {}) if sec else cfg)[f] = value


def apply_overrides(cfg: dict) -> dict:
    p = settings_path(cfg)
    if p.exists():
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("%s is damaged; ignoring phone settings", p)
            return cfg
        for k, v in saved.items():
            if k in KEYS:
                _set(cfg, k, v)
    return cfg


def save_setting(cfg: dict, key: str, value) -> None:
    _set(cfg, key, value)
    p = settings_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        saved = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        saved = {}
    saved[key] = value
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(saved, indent=1), encoding="utf-8")
    tmp.replace(p)


# coin names people type -> ticker
COIN_NAMES = {"BITCOIN": "BTC", "ETHEREUM": "ETH", "ETHER": "ETH", "SOLANA": "SOL", "ZCASH": "ZEC",
              "HYPERLIQUID": "HYPE", "BINANCE": "BNB", "BINANCECOIN": "BNB", "RIPPLE": "XRP", "DOGECOIN": "DOGE",
              "CARDANO": "ADA", "AVALANCHE": "AVAX", "CHAINLINK": "LINK", "POLKADOT": "DOT", "LITECOIN": "LTC",
              "TRON": "TRX", "TONCOIN": "TON", "SUI": "SUI", "APTOS": "APT", "ARBITRUM": "ARB", "OPTIMISM": "OP"}


def normalize_pair(text: str) -> str | None:
    s = re.sub(r"[^A-Z0-9]", "", text.upper())
    s = COIN_NAMES.get(s, s)
    if s.endswith("USDC"):
        s = s[:-4] + "USDT"          # signals use USDT-perp prices; USDC trades within cents
    elif not s.endswith("USDT"):
        s += "USDT"
    return s if len(s) > 4 else None


def _num(args: list[str], kind=float):
    if not args:
        return None
    try:
        return kind(args[0].replace("$", "").replace(",", "").replace("%", "").rstrip("xX"))
    except ValueError:
        return None


# ---------------------------------------------------------------- command handler
class Remote:
    def __init__(self, cfg: dict, st, notifier, lock: threading.RLock, wake: threading.Event, trader=None,
                 news=None, cache=None):
        self.cfg, self.st, self.n, self.lock, self.wake = cfg, st, notifier, lock, wake
        self.trader, self.news, self.cache = trader, news, cache
        self._reviewing = threading.Lock()
        self.conflict_warned = 0.0
        self.chat = str(notifier.tg_chat)
        self.started = time.time()
        self.last_scan = 0.0
        self.scan_requested = False
        self.offset = 0

    # ---- network
    def start(self) -> bool:
        if not self.n.tg:
            return False
        threading.Thread(target=self._loop, name="telegram-remote", daemon=True).start()
        return True

    def _api(self, method: str, params: dict, timeout: float = 15):
        url = f"https://api.telegram.org/bot{self.n.tg_token}/{method}"
        return json.loads(net.request("POST", url, body=urllib.parse.urlencode(params).encode(),
                                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                                      timeout=timeout))

    def _loop(self) -> None:
        try:
            self._api("setMyCommands", {"commands": json.dumps(
                [{"command": c, "description": d} for c, d in COMMAND_MENU])})
            # skip commands sent while the scanner was off (they may be stale)
            last = self._api("getUpdates", {"offset": -1, "timeout": 0}).get("result", [])
            if last:
                self.offset = last[-1]["update_id"] + 1
        except Exception as e:
            log.warning("telegram remote setup: %s", e)
        while True:
            try:
                res = self._api("getUpdates", {"offset": self.offset, "timeout": 25,
                                               "allowed_updates": '["message"]'}, timeout=40)
            except net.HTTPStatusError as e:
                if e.code == 409 and time.time() - self.conflict_warned > 3600:
                    # Telegram allows one reader per bot: another copy of this bot is running
                    self.conflict_warned = time.time()
                    self.n.alert("⚠️ <b>Two copies of this bot are running</b> (PC and VPS?).\n"
                                 "That can double-trade. Stop one of them now.")
                log.warning("telegram remote: HTTP %s", e.code)
                time.sleep(10)
                continue
            except Exception as e:
                log.warning("telegram remote: %s", e)
                time.sleep(10)
                continue
            for u in res.get("result", []):
                self.offset = u["update_id"] + 1
                m = u.get("message") or {}
                if str(m.get("chat", {}).get("id")) != self.chat:
                    log.warning("ignored a Telegram message from an unknown chat")
                    continue
                text = (m.get("text") or "").strip()
                if not text:
                    continue
                log.info("phone command: %s", text)
                try:
                    reply = self.handle(text)
                except Exception as e:
                    log.exception("command failed")
                    reply = f"⚠️ That failed: {html.escape(str(e))}"
                if reply:
                    self.n.reply(reply, keyboard=KEYBOARD)

    # ---- dispatch
    def handle(self, text: str) -> str | None:
        words = re.sub(r"[^a-z ]", "", text.lower()).strip()
        if words in BUTTON_WORDS:
            cmd, args = BUTTON_WORDS[words], []
        else:
            cmd, *args = text.split()
            cmd = cmd.lower().split("@")[0]
            if not cmd.startswith("/"):
                cmd = "/" + cmd
        fn = getattr(self, "cmd_" + cmd[1:], None)
        if fn is None:
            return f"I don't know {html.escape(cmd)}. Tap ❓ Help."
        return fn(args)

    # ---- info
    def cmd_start(self, args):
        return HELP

    cmd_help = cmd_start

    def _now_prices(self, sigs) -> dict[str, float | None]:
        out = {}
        for s in sigs:
            try:
                out[s.symbol] = price(s.symbol, self.cfg["data_source"])
            except DataError:
                out[s.symbol] = None
        return out

    def cmd_status(self, args):
        with self.lock:
            cfg, st = self.cfg, self.st
            rk = cfg["risk"]
            paused = cfg.get("paused", False)
            sigs = list(st.open.values())
            closed = list(st.closed)
        now = self._now_prices(sigs)
        risk_amt = rk["account_size"] * rk["risk_pct"] / 100
        lines = ["📊 <b>Status</b>  ·  " + ("⏸ <b>PAUSED</b> (open trades still managed)" if paused else "✅ scanning"),
                 "",
                 f"👀 {self._names(cfg['pairs'])}"
                 + (f"  ·  extra {self._names(cfg.get('extra_pairs', []))}" if cfg.get("extra_pairs") else "")
                 + (f"  ·  watchlist {self._names(cfg.get('watch_pairs', []))}" if cfg.get("watch_pairs") else "")
                 + f"  ·  {cfg['entry_tf']} chart",
                 f"💰 ${rk['account_size']:,.0f}  ·  risk <b>${risk_amt:,.0f}</b> per trade ({rk['risk_pct']:g}%)  ·  "
                 f"{rk.get('leverage', 1)}x  ·  min strength {cfg['rules']['min_score']}",
                 "", f"<b>Open trades ({len(sigs)})</b>"]
        for s in sigs:
            p = now.get(s.symbol)
            pnl = (f"<b>{money(tracker.live_r(s, p) * r_usd(s, cfg))}</b> ({tracker.live_r(s, p):+.2f}R)"
                   if p else "price unavailable")
            if s.pending:
                pnl = f"⏳ limit entry at {px(s.pending['price'])} waiting to fill"
            lines.append(f"{'🟢' if s.d == 1 else '🔴'} {coin(s.symbol)} {s.side.lower()}  {pnl}"
                         + ("" if s.pending else f"  ·  stop {stop_label(s).split(' · ')[0]}")
                         + ("" if s.traded or s.pending else "  ·  alert only"))
        if not sigs:
            lines.append("none")
        lines += ["", "<b>Closed trades</b>"]
        if closed:
            rs = [s.realized_r for s in closed]
            won = sum(r > 0.05 for r in rs)
            dollars = sum(s.realized_r * r_usd(s, cfg) for s in closed)
            lines.append(f"{len(rs)} closed  ·  {won} won  ·  {len(rs) - won} lost or flat")
            lines.append(f"Total <b>{money(dollars)}</b> ({sum(rs):+.2f}R)  ·  /history")
        else:
            lines.append("none yet")
        if self.trader:
            lines += ["", self.trader.status_line()]
        last = time.strftime("%H:%M", time.localtime(self.last_scan)) if self.last_scan else "-"
        lines.append(f"<i>Last check {last}  ·  running since "
                     f"{time.strftime('%d %b %H:%M', time.localtime(self.started))}</i>")
        return "\n".join(lines)

    def cmd_trades(self, args):
        with self.lock:
            sigs = list(self.st.open.values())
            cfg = self.cfg
        if not sigs:
            return "📈 No open trades right now."
        now = self._now_prices(sigs)
        positions = {}
        if self.trader and self.trader.ex and any(s.traded for s in sigs):
            try:
                positions = self.trader.ex.positions()
            except Exception:
                positions = {}
        out = ["📈 <b>Open trades</b>"]
        for s in sigs:
            p = now.get(s.symbol)
            opened = time.strftime("%d %b %H:%M", time.localtime(s.time / 1000))
            block = [f"\n{'🟢' if s.d == 1 else '🔴'} <b>{coin(s.symbol)} {s.side}</b>  ·  {s.tf}  ·  since {opened}"
                     + ("  ·  🤖 on Bybit" if s.traded else "  ·  ⏳ entry order on Bybit" if s.pending
                        else "  ·  alert only")]
            if s.pending:
                until = time.strftime("%H:%M", time.localtime(s.pending["until"] / 1000))
                block.append(f"⏳ Limit {'buy' if s.d == 1 else 'sell'} at {px(s.pending['price'])} waiting to fill "
                             f"(until {until})" + (f"  ·  now {px(p)}, {away(p, s.pending['price'])}" if p else ""))
            elif p:
                lr = tracker.live_r(s, p)
                block.append(f"P&amp;L <b>{money(lr * r_usd(s, cfg))}</b> ({lr:+.2f}R)  ·  now {px(p)}, entry {px(s.entry)}")
            else:
                block.append(f"Entry {px(s.entry)}  (price unavailable)")
            stop_txt = f"🛑 Stop {px(s.sl_now, s.entry)} ({stop_label(s)})" + (f"  <i>{away(p, s.sl_now)}</i>" if p else "")
            bybit_sl = (positions.get(s.symbol) or {}).get("sl")
            if s.traded and positions:
                ok = bybit_sl not in (None, "", "0") and abs(float(bybit_sl) - s.sl_now) <= abs(s.sl_now) * 1e-4
                stop_txt += "  ✅ on Bybit" if ok else f"  ⚠️ Bybit shows {bybit_sl or 'NONE'}"
            block.append(stop_txt)
            if s.tp_hit < len(s.tps):
                block.append(f"🎯 Next TP{s.tp_hit + 1} {px(s.tps[s.tp_hit], s.entry)}"
                             + (f"  <i>{away(p, s.tps[s.tp_hit])}</i>" if p else "")
                             + (f"  ·  {s.tp_hit} TP hit, {s.realized_r:+.2f}R banked" if s.tp_hit else ""))
            if p:
                block.append(trade_progress(s, p))
            out += [b for b in block if b]
        out.append("\n<i>Want out early? /close COIN</i>")
        return "\n".join(out)

    def cmd_settings(self, args):
        with self.lock:
            cfg = self.cfg
            rk = cfg["risk"]
        return "\n".join([
            "⚙️ <b>Settings</b>",
            f"Capital: ${rk['account_size']:,.0f}   /capital",
            f"Risk per trade: {rk['risk_pct']:g}% = ${rk['account_size'] * rk['risk_pct'] / 100:,.0f}   /risk",
            f"Leverage: {rk.get('leverage', 1)}x isolated   /lev",
            f"Max margin per trade: {rk.get('max_margin_pct', 30):g}% of capital",
            f"Chart: {cfg['entry_tf']} (trend {cfg['trend_tf']})   /tf",
            f"Min strength: {cfg['rules']['min_score']} (watchlist {cfg.get('watch_min_score', 75)})   /minscore",
            f"Main pairs: {self._names(cfg['pairs'])}",
            f"Extra pairs (half risk): {self._names(cfg.get('extra_pairs', [])) or '-'}   /extra",
            f"Watchlist: {self._names(cfg.get('watch_pairs', [])) or '-'}",
            f"New signals: {'⏸ paused' if cfg.get('paused') else 'on'}",
            "",
            "<i>Changes from the phone are saved and survive restarts.</i>",
        ])

    def cmd_pairs(self, args):
        with self.lock:
            main, watch = self.cfg["pairs"], self.cfg.get("watch_pairs", [])
            extra = self.cfg.get("extra_pairs", [])
            t = self.cfg.get("trading", {})
        return (f"🔔 <b>Main</b> (auto-traded): {self._names(main) or '-'}\n"
                f"➕ <b>Extra</b> (auto-traded at {t.get('extra_risk_mult', 0.5):g}x risk, "
                f"{t.get('extra_max_open', 1)} at a time): {self._names(extra) or '-'}\n"
                f"👀 <b>Watchlist</b> (silent alerts only, strength ≥ {self.cfg.get('watch_min_score', 75)}): "
                f"{self._names(watch) or '-'}\n\n/add COIN  ·  /extra COIN  ·  /watch COIN  ·  /remove COIN")

    # ---- control
    def cmd_pause(self, args):
        with self.lock:
            save_setting(self.cfg, "paused", True)
        return "⏸ <b>Paused.</b> No new signals. Open trades are still tracked.\nTap ▶️ Resume to restart."

    def cmd_resume(self, args):
        with self.lock:
            save_setting(self.cfg, "paused", False)
        return "▶️ <b>Resumed.</b> New signals are on."

    def cmd_scan(self, args):
        with self.lock:
            self.scan_requested = True
        self.wake.set()
        return "🔍 Checking all pairs now…"

    def scan_done(self, new: int) -> None:
        self.n.reply("🔍 Check done: " + (f"{new} new signal(s) above." if new else
                     "no setup right now. That's normal; I'll alert you when one appears."))

    # ---- money
    def cmd_risk(self, args):
        v = _num(args)
        if v is None or not 0.1 <= v <= 5:
            return "Send a % between 0.1 and 5, e.g. <code>/risk 1</code>"
        with self.lock:
            save_setting(self.cfg, "risk_pct", v)
            amt = self.cfg["risk"]["account_size"] * v / 100
        warn = "\n⚠️ Above 2% a losing streak hurts fast. 5 losses in a row is common." if v > 2 else ""
        return f"✅ Risk per trade: <b>{v:g}%</b> = <b>${amt:,.0f}</b> lost if the stop is hit.{warn}"

    def cmd_lev(self, args):
        v = _num(args, int)
        if v is None or not 1 <= v <= 50:
            return "Send a leverage between 1 and 50, e.g. <code>/lev 10</code>"
        with self.lock:
            save_setting(self.cfg, "leverage", v)
        warn = ("\n⚠️ Above 20x the liquidation price gets close; I'll lower it per trade when the stop "
                "would be too near liquidation.") if v > 20 else ""
        return (f"✅ Leverage: <b>{v}x</b> (use <b>isolated</b> margin on the exchange).\n"
                "Leverage only changes the margin you lock up. The $ you lose at the stop is set by /risk."
                + warn)

    def cmd_capital(self, args):
        v = _num(args)
        if v is None or not 50 <= v <= 10_000_000:
            return "Send your capital in $, e.g. <code>/capital 3000</code>"
        with self.lock:
            save_setting(self.cfg, "account_size", v)
            amt = v * self.cfg["risk"]["risk_pct"] / 100
        return f"✅ Capital: <b>${v:,.0f}</b>. At {self.cfg['risk']['risk_pct']:g}% that is ${amt:,.0f} per trade."

    def cmd_minscore(self, args):
        v = _num(args, int)
        if v is None or not 40 <= v <= 95:
            return "Send a number 40-95, e.g. <code>/minscore 70</code> (higher = fewer, stronger signals)"
        with self.lock:
            save_setting(self.cfg, "min_score", v)
        return f"✅ Minimum signal strength: <b>{v}</b>/100."

    def cmd_tf(self, args):
        tf = args[0].lower() if args else ""
        if tf not in TREND_FOR:
            return "Send one of: " + ", ".join(f"<code>/tf {t}</code>" for t in TREND_FOR)
        with self.lock:
            save_setting(self.cfg, "entry_tf", tf)
            save_setting(self.cfg, "trend_tf", TREND_FOR[tf])
            # wait for the next fresh candle close instead of firing on an old one
            boundary = int(time.time() * 1000) // TF_MS[tf] * TF_MS[tf]
            for p in self.cfg["pairs"] + self.cfg.get("watch_pairs", []) + self.cfg.get("extra_pairs", []):
                self.st.last_eval[p] = boundary
        warn = "\n⚠️ 15m lost money in the backtest; 1h or 4h is better." if tf == "15m" else ""
        return f"✅ Chart: <b>{tf}</b> (trend filter {TREND_FOR[tf]}). Takes effect at the next {tf} candle.{warn}"

    # ---- pairs
    def _add(self, args, key: str, label: str):
        if not args:
            cmd = {"pairs": "add", "watch_pairs": "watch", "extra_pairs": "extra"}[key]
            return f"Which coin? e.g. <code>/{cmd} SUI</code>"
        sym = normalize_pair(args[0])
        if not sym:
            return "That doesn't look like a coin name."
        try:
            price(sym, self.cfg["data_source"])
        except DataError:
            return f"❌ Couldn't find <b>{html.escape(sym)}</b> on Binance/Bybit futures."
        with self.lock:
            for other in ("pairs", "watch_pairs", "extra_pairs"):    # a coin lives in one list only
                if other != key and sym in self.cfg.get(other, []):
                    save_setting(self.cfg, other, [p for p in self.cfg[other] if p != sym])
            cur = list(self.cfg.get(key, []))
            if sym not in cur:
                cur.append(sym)
                save_setting(self.cfg, key, cur)
                self.st.last_eval[sym] = int(time.time() * 1000) // TF_MS[self.cfg["entry_tf"]] * \
                    TF_MS[self.cfg["entry_tf"]]
        return f"✅ {coin(sym)} added to {label}."

    def cmd_add(self, args):
        return self._add(args, "pairs", "main pairs (loud alerts)")

    def cmd_watch(self, args):
        return self._add(args, "watch_pairs", "watchlist (silent alerts)")

    def cmd_extra(self, args):
        return self._add(args, "extra_pairs", "extra pairs (auto-traded at half risk, one at a time, "
                                              "never taking room from your main pairs)")

    def cmd_remove(self, args):
        sym = normalize_pair(args[0]) if args else None
        if not sym:
            return "Which coin? e.g. <code>/remove SUI</code>"
        with self.lock:
            if self.cfg["pairs"] == [sym]:
                return "You need at least one main pair. /add another coin first."
            found = False
            for key in ("pairs", "watch_pairs", "extra_pairs"):
                if sym in self.cfg.get(key, []):
                    save_setting(self.cfg, key, [p for p in self.cfg[key] if p != sym])
                    found = True
            still_open = sym in self.st.open
        if not found:
            return f"{coin(sym)} isn't in your pairs."
        return f"✅ {coin(sym)} removed." + ("\nIts open trade is still tracked (/close to stop)." if still_open else "")

    def cmd_close(self, args):
        sym = normalize_pair(args[0]) if args else None
        with self.lock:
            open_now = list(self.st.open)
        if not sym or sym not in open_now:
            return ("Which trade? Open: " + (", ".join(f"<code>/close {coin(p)}</code>" for p in open_now)
                                            if open_now else "none"))
        return self._close_one(sym)

    def _close_one(self, sym: str) -> str:
        try:
            now = price(sym, self.cfg["data_source"])
        except DataError as e:
            return f"❌ No price for {coin(sym)} right now: {html.escape(str(e))}"
        with self.lock:
            sig = self.st.open.get(sym)
            if sig is None:
                return f"{coin(sym)} already closed."
            extra = ""
            if sig.pending and self.trader:
                extra = "\n" + self.trader.cancel_pending(sig, "you closed the signal")
            if sig.traded and self.trader:
                try:
                    size = self.trader.flatten(sig)
                    extra = f"\n🤖 Closed {size:g} {coin(sym)} on Bybit at market." if size else \
                        "\n🤖 Nothing left open on Bybit; orders cancelled."
                except Exception as e:
                    return (f"❌ Bybit refused to close {coin(sym)}: {html.escape(str(e))}\n"
                            "Close it in the Bybit app. The trade is still tracked here.")
            t = int(time.time() * 1000)
            tracker.close_now(sig, now, t, tracker.fees_of(self.cfg))
            sig.events.append(f"{time.strftime('%m-%d %H:%M')} closed by you (/close) @ {now:.8g}")
            self.st.closed.append(self.st.open.pop(sym))
            learn.on_closed(self.cfg, self.st, sig)
            self.st.cool_until[sym] = t + self.cfg["rules"]["cooldown_bars"] * TF_MS[self.cfg["entry_tf"]]
            self.st.save()
            self.n._csv("CLOSED", sig, f"closed by you @ {now:.8g}")
            one_r = r_usd(sig, self.cfg)
        return (f"✅ <b>{coin(sym)} {sig.side} closed</b> at ≈ {px(now)}.{extra}\n"
                f"💵 Result: <b>{sig.realized_r:+.2f}R ≈ {usd(sig.realized_r * one_r)}</b>")

    # ---- Bybit
    def cmd_balance(self, args):
        if not self.trader:
            return "Auto-trading isn't set up."
        with self.lock:
            open_sigs = list(self.st.open.values())
        try:
            return self.trader.balance_text(open_sigs)
        except Exception as e:
            return f"❌ Couldn't reach Bybit: {html.escape(str(e))}"

    def cmd_autotrade(self, args):
        if not self.trader:
            return "Auto-trading isn't set up."
        arg = args[0].lower() if args else ""
        if arg not in ("on", "off"):
            return self.trader.status_line() + "\nSend <code>/autotrade on</code> or <code>/autotrade off</code>"
        with self.lock:
            save_setting(self.cfg, "autotrade", arg == "on")
            if arg == "on" and self.trader.ex is None and not self.trader.connect():
                return f"⚠️ Couldn't connect to Bybit: {html.escape(self.trader.error)}"
        return (f"✅ Auto-trading <b>ON</b> ({self.trader.label}). New signals will be placed as orders."
                if arg == "on" else
                "⏹ Auto-trading <b>OFF</b>. You still get alerts. Trades already open stay managed "
                "(stop moves, closing). /closeall closes them now.")

    def cmd_news(self, args):
        if not self.news:
            return "News guard is off ([news] enabled = false)."
        return self.news.summary(time.time() * 1000)

    def cmd_summary(self, args):
        from .scanner import daily_summary
        with self.lock:
            return daily_summary(self.cfg, self.st, self.trader)

    # ---- learning
    def cmd_review(self, args):
        """Runs in the background so other commands keep working; the report arrives when done."""
        if not self._reviewing.acquire(blocking=False):
            return "🧠 A review is already running. The report will arrive shortly."
        with self.lock:
            snapshot = copy.deepcopy(self.cfg)

        def work():
            try:
                self.n.reply(learn.review(snapshot, self.cache.get if self.cache else None), keyboard=KEYBOARD)
            except Exception as e:
                log.exception("review failed")
                self.n.reply(f"⚠️ Review failed: {html.escape(str(e))}")
            finally:
                self._reviewing.release()
        threading.Thread(target=work, name="review", daemon=True).start()
        return "🧠 Reviewing the trade history… the report follows in a few seconds."

    def cmd_apply(self, args):
        n = _num(args, int)
        sugg = learn.load_suggestions(settings_path(self.cfg).parent)
        if not sugg:
            return "No suggestions to apply. Send /review first."
        if n is None or not 1 <= n <= len(sugg):
            return f"Send /apply 1 to {len(sugg)} (the numbers from the last /review)."
        g = sugg[n - 1]
        if g.get("type") == "setting":
            if g["param"] not in REVIEW_SETTINGS:
                return "That suggestion can't be applied from the phone."
            with self.lock:
                old = (self.cfg["rules"] if g["param"] in KEYS and KEYS[g["param"]][0] == "rules"
                       else self.cfg).get(g["param"])
                save_setting(self.cfg, g["param"], g["value"])
            return (f"✅ Changed: <b>{html.escape(g['label'])}</b> (was {html.escape(json.dumps(old))}).\n"
                    f"It applies to new signals. Saved, so it survives restarts. To undo, send /review again "
                    f"after a while, or delete logs/settings.json to go back to signals.toml.")
        with self.lock:
            rules = list(self.cfg.get("learn_rules", []))
            if any(r["id"] == g["id"] for r in rules):
                return f"That rule is already active: {html.escape(g['label'])}."
            rules.append({"id": g["id"], "cond": g["cond"], "label": g["label"],
                          "added": time.strftime("%Y-%m-%d"),
                          "evidence": f"{g['n']} trades, avg {g['avg']:+.2f}R"})
            save_setting(self.cfg, "learn_rules", rules)
        return (f"✅ Rule {len(rules)} added: no more auto-trades for <b>{html.escape(g['label'])}</b>.\n"
                "You still get those alerts, and they are still tracked, so the review will tell you "
                "if this condition recovers. /rules to see all, /drop to remove.")

    def cmd_history(self, args):
        n = _num(args, int) or 10
        return learn.history_text(self.cfg, max(1, min(n, 20)))

    def cmd_rules(self, args):
        with self.lock:
            rules = list(self.cfg.get("learn_rules", []))
        if not rules:
            return "🧠 No rules yet. Every signal on a main pair is auto-traded. /review looks for losing patterns."
        out = ["🧠 <b>Your rules</b> (no auto-trades for these)"]
        out += [f"{i}. {html.escape(r['label'])}  <i>(added {r.get('added', '?')}: {r.get('evidence', '')})</i>"
                for i, r in enumerate(rules, 1)]
        out.append("\n/drop N removes one.")
        return "\n".join(out)

    def cmd_drop(self, args):
        n = _num(args, int)
        with self.lock:
            rules = list(self.cfg.get("learn_rules", []))
            if n is None or not 1 <= n <= len(rules):
                return "Which rule? See /rules, then e.g. <code>/drop 1</code>." if rules else "No rules to drop."
            gone = rules.pop(n - 1)
            save_setting(self.cfg, "learn_rules", rules)
        return f"✅ Rule removed: <b>{html.escape(gone['label'])}</b> is auto-traded again."

    def cmd_closeall(self, args):
        with self.lock:
            syms = [p for p, s in self.st.open.items() if s.traded]
        if not syms:
            return "No bot trades open on Bybit."
        return "\n\n".join(self._close_one(s) for s in syms)

    @staticmethod
    def _names(pairs: list[str]) -> str:
        return ", ".join(coin(p) for p in pairs)
