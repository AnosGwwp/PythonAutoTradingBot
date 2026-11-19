"""Formatting + delivery: console, CSV journal, Telegram (HTML, mobile-friendly)."""
from __future__ import annotations

import csv
import html
import json
import logging
import math
import os
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import net
from .strategy import CHASE_ATR, Signal

log = logging.getLogger("signals")


# ---------------------------------------------------------------- helpers
def px(p: float, ref: float | None = None) -> str:
    """Price with sensible decimals. `ref` (e.g. the entry) makes every level of one trade use the
    same number of decimals, so they line up."""
    base = ref if ref and ref > 0 else p
    if base <= 0:
        return f"{p}"
    dec = min(8, max(2, 4 - int(math.floor(math.log10(base)))))
    return f"{p:,.{dec}f}"


def usd(x: float) -> str:
    return f"{'-' if x < 0 else '+' if x > 0 else ''}${abs(x):,.2f}"


def coin(symbol: str) -> str:
    for q in ("USDT", "USDC"):
        if symbol.endswith(q):
            return symbol[: -len(q)]
    return symbol


def sizing(sig: Signal, cfg: dict) -> dict:
    """Position size from a fixed $ risk, then margin from the chosen leverage.

    Leverage does NOT change how many dollars you lose at the stop (risk_pct does that); it only
    changes how much margin is locked up. The leverage is lowered automatically when the stop
    would sit too close to the liquidation price (stop may use at most 80% of that distance).
    """
    rk = cfg["risk"]
    acct = rk["account_size"]
    risk_amt = acct * rk["risk_pct"] / 100
    want = max(1, int(rk.get("leverage", rk.get("max_leverage", 1))))
    mmr = rk.get("maint_margin_pct", 0.5) / 100
    stop = sig.risk / sig.entry
    safe = max(1, int(1 / (stop / 0.8 + mmr)))
    lev = min(want, safe)
    qty = risk_amt / sig.risk
    notional = qty * sig.entry
    max_notional = acct * rk.get("max_margin_pct", 30) / 100 * lev
    capped = notional > max_notional
    if capped:
        notional = max_notional
        qty = notional / sig.entry
        risk_amt = qty * sig.risk
    liq = sig.entry * (1 - sig.d * (1 / lev - mmr)) if lev > 1 else None
    return {"qty": qty, "notional": notional, "lev": lev, "want": want, "margin": notional / lev,
            "risk_amt": risk_amt, "risk_pct": risk_amt / acct * 100, "acct": acct,
            "capped": capped, "liq": liq}


def is_extra(symbol: str, cfg: dict) -> bool:
    return symbol in cfg.get("extra_pairs", []) and symbol not in cfg.get("pairs", [])


def pair_cfg(cfg: dict, symbol: str) -> dict:
    """Settings for one pair: extra pairs trade at a fraction of the normal risk (default half)."""
    if is_extra(symbol, cfg):
        m = cfg.get("trading", {}).get("extra_risk_mult", 0.5)
        return {**cfg, "risk": {**cfg["risk"], "risk_pct": cfg["risk"]["risk_pct"] * m}}
    return cfg


def r_usd(sig: Signal, cfg: dict) -> float:
    """Dollars per 1R for this signal (fixed when it was sent)."""
    return sig.risk_usd or sizing(sig, pair_cfg(cfg, sig.symbol))["risk_amt"]


def strength(score: int) -> str:
    n = max(1, min(5, round(score / 20)))
    word = "Very strong" if score >= 85 else "Strong" if score >= 75 else "Good" if score >= 65 else "Fair"
    return f"{'★' * n}{'☆' * (5 - n)}  {word} ({score}/100)"


def is_watch(sig: Signal, cfg: dict) -> bool:
    return sig.symbol in cfg.get("watch_pairs", [])


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def dotenv() -> dict:
    """KEY=value lines from the project's .env file (used on the VPS)."""
    out = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def env(name: str) -> str:
    """Secret lookup: process environment -> .env file -> saved Windows user variable (setx)."""
    v = os.environ.get(name, "") or dotenv().get(name, "")
    if v:
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except Exception:
        return ""


def console(text: str) -> None:
    """Print without ever raising: an emoji on a non-UTF-8 console (or a missing console) must not
    abort a scan and lose the Telegram alert that follows."""
    try:
        print(text, flush=True)
    except (UnicodeEncodeError, OSError, ValueError, AttributeError):
        try:
            print(text.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# ---------------------------------------------------------------- messages (HTML)
def money(x: float) -> str:
    """-$7.01 / +$12.50: always signed, so a loss can't be misread as a gain."""
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def away(now: float, level: float) -> str:
    """How far a level is from the current price, e.g. '2.1% away'."""
    return f"{abs(level / now - 1) * 100:.1f}% away" if now else ""


def bar(frac: float, width: int = 10) -> str:
    """Progress bar from stop (0) to the next target (1): where price is between the two."""
    n = max(0, min(width, round(frac * width)))
    return "▓" * n + "░" * (width - n)


def trade_progress(sig: Signal, now: float) -> str:
    """'🛑 1,796.11 ▓▓▓░░░░░░░ 1,698.53 🎯' with the current stop and the next target."""
    if sig.tp_hit >= len(sig.tps) or not now:
        return ""
    tgt, stop = sig.tps[sig.tp_hit], sig.sl_now
    span = (tgt - stop) * sig.d
    frac = ((now - stop) * sig.d / span) if span else 0.0
    return f"<code>🛑 {px(stop, sig.entry)} {bar(frac)} {px(tgt, sig.entry)} 🎯</code>"


def stop_label(sig: Signal) -> str:
    """Where the stop is, in words."""
    if sig.tp_hit >= 2:
        return f"at TP{sig.tp_hit - 1} · profit locked"
    if sig.d * (sig.sl_now - sig.entry) >= 0:
        return "at entry · can't lose"
    return "not moved yet"


def signal_message(sig: Signal, cfg: dict, note: str = "") -> str:
    s = sizing(sig, pair_cfg(cfg, sig.symbol))
    if sig.traded:      # show what was really done on the exchange
        s["qty"], s["notional"] = sig.qty, sig.qty * sig.entry
        s["margin"], s["risk_amt"] = s["notional"] / s["lev"], sig.risk_usd
        s["risk_pct"] = sig.risk_usd / s["acct"] * 100
    long = sig.side == "LONG"
    pct = lambda p: f"{(p / sig.entry - 1) * 100:+.1f}%"
    chase = sig.entry + sig.d * CHASE_ATR * sig.atr
    when = time.strftime("%H:%M", time.localtime(sig.time / 1000))
    stars = max(1, min(5, round(sig.score / 20)))
    head = (f"{'🟢' if long else '🔴'} <b>{'LONG' if long else 'SHORT'} {coin(sig.symbol)}</b>  ·  {sig.tf}"
            f"  ·  {'★' * stars}{'☆' * (5 - stars)} {sig.score}")
    if is_watch(sig, cfg):
        head += "  ·  👀 watchlist"
    elif is_extra(sig.symbol, cfg):
        head += "  ·  ➕ extra pair (half risk)"
    lines = [head]
    if note:
        lines += ["", note]
    rows = [("Entry", px(sig.entry), "filled" if sig.traded else ""), ("Stop", px(sig.sl, sig.entry), pct(sig.sl))]
    rows += [(f"TP{k + 1}", px(tp, sig.entry), pct(tp)) for k, tp in enumerate(sig.tps)]
    w = max(len(r[1]) for r in rows)
    lines += ["", "<pre>" + "\n".join(f"{a:<6}{b:>{w}}  {c}".rstrip() for a, b, c in rows) + "</pre>"]
    lines.append(f"💰 Risk <b>${s['risk_amt']:,.2f}</b> ({s['risk_pct']:.1f}% of ${s['acct']:,.0f})"
                 + ("  <i>reduced: margin limit</i>" if s["capped"] else ""))
    lines.append(f"📦 {s['qty']:.4g} {coin(sig.symbol)} ≈ ${s['notional']:,.0f}  ·  {s['lev']}x isolated  ·  "
                 f"margin ${s['margin']:,.0f}")
    if s["liq"]:
        lines.append(f"💀 Liquidation ≈ {px(s['liq'])}"
                     + (f"  <i>(leverage lowered from {s['want']}x to keep it past the stop)</i>"
                        if s["lev"] < s["want"] else ""))
    n = len(sig.tps)
    manage = (f"Close 1/{n} at each TP; after TP1 move the stop to entry." if n > 1 else "Close all at TP1.")
    if sig.traded:
        lines.append("🤖 <i>The bot manages it: stop to entry after TP1, to TP1 after TP2.</i>")
    elif sig.pending:
        lines.append("⏳ <i>Waiting for the limit order to fill. Once it does, the bot manages the trade.</i>")
    elif cfg.get("trading", {}).get("limit_entry"):
        lines.append(f"👉 <i>Place a limit {'buy' if long else 'sell'} at {px(sig.entry)} (cheaper fee, better "
                     f"price) and cancel it if it hasn't filled within one {sig.tf} candle. {manage} "
                     f"Signal at {when}.</i>")
    else:
        lines.append(f"👉 <i>{'Buy' if long else 'Sell'} now, not {'above' if long else 'below'} {px(chase)}. "
                     + manage + f" Signal at {when}.</i>")
    lines.append("<blockquote expandable>💡 Why\n" + "\n".join(f"• {html.escape(r)}" for r in sig.reasons)
                 + "</blockquote>")
    return "\n".join(lines)


def event_message(sig: Signal, ev: dict, cfg: dict, note: str = "") -> str:
    msg = _event_body(sig, ev, cfg)
    return msg + (f"\n{note}" if note else "")


def _event_body(sig: Signal, ev: dict, cfg: dict) -> str:
    one_r = r_usd(sig, cfg)
    name = f"{coin(sig.symbol)} {sig.side.lower()}"
    result = lambda label: f"💵 {label} <b>{money(sig.realized_r * one_r)}</b>  ({sig.realized_r:+.2f}R)"
    k = ev["kind"]
    if k == "tp":
        where = "entry" if ev["n"] == 1 else f"TP{ev['n'] - 1}"
        if sig.traded:
            todo = f"✂️ 1/{len(sig.tps)} closed by the take-profit order\n🔒 Stop moved to {where} ({px(ev['new_sl'])})"
        else:
            todo = (f"👉 Close 1/{len(sig.tps)} now and move your stop to {where} ({px(ev['new_sl'])})")
        safe = "  ·  this trade can't lose now" if ev["n"] == 1 else "  ·  profit locked"
        return "\n".join([f"🎯 <b>{name}: TP{ev['n']} hit</b> at {px(ev['price'])}{safe}", todo,
                          result("Banked so far")])
    if k == "breakeven":
        todo = "" if sig.traded else f"\n👉 Move your stop to {px(ev['new_sl'])}."
        return (f"🛡 <b>{name}: stop moved to entry</b> ({px(ev['new_sl'])}): price reached "
                f"+{ev.get('at_r', 0):g}R, so the trade can't lose now.{todo}")
    if k == "closed":
        return "\n".join([f"✋ <b>{name} closed</b>: {html.escape(ev.get('why', 'closed'))}, at ≈ {px(ev['price'])}",
                          result("Result")])
    if k == "done":
        return "\n".join([f"🏆 <b>{name}: all targets hit!</b>", result("Result")])
    if k == "stop":
        if ev["after_tp"] == 0 and not ev.get("breakeven"):
            title = f"🛑 <b>{name}: stopped out</b> at {px(ev['price'])}"
        elif ev["after_tp"] <= 1:
            title = f"⚪ <b>{name}: closed at entry</b>" + (" after TP1" if ev["after_tp"] == 1 else " (no loss)")
        else:
            title = f"🔒 <b>{name}: closed at the locked-in stop</b> ({px(ev['price'])}) after TP{ev['after_tp']}"
        return "\n".join([title, result("Result")])
    if k == "expired":
        return "\n".join([f"⌛ <b>{name}: closed after {ev['bars']} candles</b> at {px(ev['price'])}",
                          "No target or stop was hit in time.", result("Result")])
    return f"{name}: {k}"


def event_text(ev: dict) -> str:
    """Short text for the CSV log and the trade's timeline."""
    k = ev["kind"]
    if k == "tp":
        return f"TP{ev['n']} @ {ev['price']:.8g}, stop -> {ev['new_sl']:.8g}"
    if k == "breakeven":
        return f"stop -> entry {ev['new_sl']:.8g} (+{ev.get('at_r', 0):g}R reached)"
    if k == "stop":
        return f"stop @ {ev['price']:.8g} (after {ev['after_tp']} TP)"
    if k == "expired":
        return f"expired @ {ev['price']:.8g}"
    if k == "closed":
        return f"closed @ {ev['price']:.8g} ({ev.get('why', '')})"
    return k


# kept for console / status output
def format_signal(sig: Signal, cfg: dict) -> str:
    return plain(signal_message(sig, cfg))


# ---------------------------------------------------------------- delivery
def telegram_setup(token: str) -> None:
    """Find the chat id of whoever messaged the bot, then send them a test alert."""
    base = f"https://api.telegram.org/bot{token}"
    try:
        me = json.loads(urllib.request.urlopen(f"{base}/getMe", timeout=10).read())
        upd = json.loads(urllib.request.urlopen(f"{base}/getUpdates", timeout=10).read())
    except Exception as e:
        print(f"Telegram rejected the token or is unreachable: {e}")
        return
    print(f"Bot OK: @{me['result']['username']}")
    chats = {}
    for u in upd.get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        ch = m.get("chat")
        if ch:
            chats[ch["id"]] = ch.get("username") or ch.get("title") or ch.get("first_name", "")
    if not chats:
        print(f"No messages yet. Open Telegram, send any message to @{me['result']['username']}, "
              "then run this again.")
        return
    for cid, name in chats.items():
        data = urllib.parse.urlencode({"chat_id": cid, "text": "✅ Signal scanner connected. Alerts will arrive here."}).encode()
        urllib.request.urlopen(f"{base}/sendMessage", data, timeout=10).read()
        print(f"chat id {cid} ({name}) -> test message sent")
    cid = next(iter(chats))
    print("\nNow run these in PowerShell (once), then open a NEW terminal:\n"
          f'  setx TELEGRAM_BOT_TOKEN "<your token>"\n  setx TELEGRAM_CHAT_ID "{cid}"\n'
          "and set  telegram = true  in signals.toml.")


class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dir = Path(cfg["alerts"]["log_dir"])
        self.dir.mkdir(parents=True, exist_ok=True)
        self.csv = self.dir / "signals.csv"
        self.tg_token = env("TELEGRAM_BOT_TOKEN")
        self.tg_chat = env("TELEGRAM_CHAT_ID")
        self.tg = cfg["alerts"]["telegram"]
        if self.tg and not (self.tg_token and self.tg_chat):
            log.warning("telegram enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; console only")
            self.tg = False
        self._q: queue.Queue | None = None
        self._qlock = threading.Lock()

    def signal(self, sig: Signal, note: str = "") -> None:
        msg = signal_message(sig, self.cfg, note)
        console("\n" + "=" * 60 + "\n" + plain(msg) + "\n" + "=" * 60)
        self._telegram(msg, silent=is_watch(sig, self.cfg) and not sig.traded)
        self._csv("NEW", sig, "auto-traded" if sig.traded else "")

    def event(self, sig: Signal, ev: dict, note: str = "") -> None:
        msg = event_message(sig, ev, self.cfg, note)
        console(">> " + plain(msg).replace("\n", " | "))
        self._telegram(msg, silent=is_watch(sig, self.cfg) and not sig.traded)
        self._csv("CLOSED" if sig.status == "closed" else "UPDATE", sig, event_text(ev))

    def info(self, text: str, keyboard: dict | None = None) -> None:
        """Status message (silent notification)."""
        console(plain(text))
        self._telegram(text, silent=True, keyboard=keyboard)

    def reply(self, text: str, keyboard: dict | None = None) -> None:
        """Answer to a phone command (Telegram only)."""
        self._telegram(text, silent=False, keyboard=keyboard)

    def alert(self, text: str) -> None:
        """Important warning (with sound)."""
        console(plain(text))
        self._telegram(text, silent=False)

    def _csv(self, kind: str, sig: Signal, event: str) -> None:
        """Append to the CSV journal. Never raises: on Windows the file is locked while it is open
        in Excel, and a journal row must not abort a scan (and with it alerts or Bybit follow-ups)."""
        row = [time.strftime("%Y-%m-%d %H:%M:%S"), kind, sig.symbol, sig.side, sig.tf,
               time.strftime("%Y-%m-%d %H:%M", time.localtime(sig.time / 1000)),
               sig.entry, sig.sl, json.dumps([round(t, 10) for t in sig.tps]), sig.score,
               "+".join(sig.setups), event, round(sig.realized_r, 3)]
        for attempt in range(3):
            try:
                new = not self.csv.exists()
                with self.csv.open("a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["logged", "kind", "symbol", "side", "tf", "signal_time", "entry", "sl",
                                    "tps", "score", "setups", "event", "realized_r"])
                    w.writerow(row)
                return
            except OSError as e:
                if attempt == 2:
                    log.warning("could not write %s (open in Excel?): %s. Row skipped: %s",
                                self.csv, e, ",".join(map(str, row)))
                    return
                time.sleep(0.2)

    def _telegram(self, text: str, silent: bool = False, keyboard: dict | None = None) -> None:
        """Queue a message. A background thread sends the queue in order, so trading never waits
        for Telegram (a rate limit alone can stall a send for 30 s)."""
        if not self.tg:
            return
        with self._qlock:
            if self._q is None:
                self._q = queue.Queue()
                threading.Thread(target=self._sender, name="telegram-send", daemon=True).start()
        self._q.put((text, silent, keyboard))

    def flush(self, timeout: float = 15.0) -> bool:
        """Wait until every queued message was sent (call before the program exits)."""
        if self._q is None:
            return True
        done = threading.Event()
        self._q.put(done)
        return done.wait(timeout)

    def _sender(self) -> None:
        while True:
            item = self._q.get()
            if isinstance(item, threading.Event):
                item.set()
                continue
            try:
                self._send_now(*item)
            except Exception:                      # never let the sender thread die
                log.exception("telegram send")

    def _send_now(self, text: str, silent: bool = False, keyboard: dict | None = None) -> None:
        if len(text) > 4000:                       # Telegram's limit is 4096 characters
            text = text[:3990] + "\n…"
        params = {"chat_id": self.tg_chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true",
                  "disable_notification": "true" if silent else "false"}
        if keyboard:
            params["reply_markup"] = json.dumps(keyboard)
        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        for attempt in range(3):
            try:
                net.request("POST", url, body=urllib.parse.urlencode(params).encode(),
                            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
                return
            except net.HTTPStatusError as e:
                body = e.body[:300].decode(errors="replace")
                if e.code == 429:                  # rate limited: wait as told, then retry
                    try:
                        wait = json.loads(body)["parameters"]["retry_after"]
                    except (ValueError, KeyError):
                        wait = 3
                    time.sleep(min(int(wait), 30))
                    continue
                if e.code == 400 and "parse" in body and params.get("parse_mode"):
                    params.pop("parse_mode")       # bad HTML: send as plain text rather than lose it
                    params["text"] = plain(text)
                    continue
                log.warning("telegram send failed: %s %s", e.code, body)
                return
            except Exception as e:                 # alerts must never crash the scanner
                if attempt == 2:
                    log.warning("telegram send failed: %s", e)
                    return
                time.sleep(2)
