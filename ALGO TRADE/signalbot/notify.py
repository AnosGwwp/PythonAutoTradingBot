"""Formatting + delivery: console, CSV journal, Telegram (HTML, mobile-friendly)."""
from __future__ import annotations

import csv
import html
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .strategy import CHASE_ATR, Signal

log = logging.getLogger("signals")


# ---------------------------------------------------------------- helpers
def px(p: float) -> str:
    if p <= 0:
        return f"{p}"
    dec = min(8, max(2, 4 - int(math.floor(math.log10(p)))))
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


def r_usd(sig: Signal, cfg: dict) -> float:
    """Dollars per 1R for this signal (fixed when it was sent)."""
    return sig.risk_usd or sizing(sig, cfg)["risk_amt"]


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
def signal_message(sig: Signal, cfg: dict, note: str = "") -> str:
    s = sizing(sig, cfg)
    if sig.traded:      # show what was really done on the exchange
        s["qty"], s["notional"] = sig.qty, sig.qty * sig.entry
        s["margin"], s["risk_amt"] = s["notional"] / s["lev"], sig.risk_usd
        s["risk_pct"] = sig.risk_usd / s["acct"] * 100
    long = sig.side == "LONG"
    pct = lambda p: f"{(p / sig.entry - 1) * 100:+.1f}%"
    chase = sig.entry + sig.d * CHASE_ATR * sig.atr
    when = time.strftime("%H:%M", time.localtime(sig.time / 1000))
    head = f"{'🟢' if long else '🔴'} <b>{'LONG' if long else 'SHORT'}  {coin(sig.symbol)}</b>  ·  {sig.tf} chart"
    if is_watch(sig, cfg):
        head += "  ·  👀 watchlist"
    lines = [head, f"Strength: {strength(sig.score)}"]
    if note:
        lines += ["", note]
    lines += ["", f"▶️ <b>Entry:</b>  {px(sig.entry)}" + ("  <i>(filled)</i>" if sig.traded else "")]
    if not sig.traded:
        lines.append(f"      <i>don't {'buy above' if long else 'sell below'} {px(chase)}</i>")
    lines += [f"🛑 <b>Stop loss:</b>  {px(sig.sl)}  <i>({pct(sig.sl)})</i>", ""]
    lines += [f"🎯 <b>TP{k + 1}:</b>  {px(tp)}  <i>({pct(tp)})</i>" for k, tp in enumerate(sig.tps)]
    lines += [
        "",
        f"📦 <b>Position:</b>  {s['qty']:.4g} {coin(sig.symbol)}  (≈ ${s['notional']:,.0f})",
        f"⚙️ <b>Leverage:</b>  {s['lev']}x isolated  ·  Margin ${s['margin']:,.0f}",
    ]
    if s["lev"] < s["want"]:
        lines.append(f"      <i>lowered from {s['want']}x so the stop hits before liquidation</i>")
    lines.append(f"⚖️ <b>Risk:</b>  ${s['risk_amt']:,.2f}  ({s['risk_pct']:.1f}% of ${s['acct']:,.0f})"
                 + ("  <i>(reduced: margin limit)</i>" if s["capped"] else ""))
    if s["liq"]:
        lines.append(f"💀 Liquidation ≈ {px(s['liq'])}  <i>(far past your stop)</i>")
    lines += [
        "",
        "💡 <b>Why</b>",
    ]
    lines += [f"• {html.escape(r)}" for r in sig.reasons]
    n = len(sig.tps)
    if sig.traded:
        lines += ["", "📋 <i>The bot manages this trade: it moves the stop to entry after TP1 "
                      "and to TP1 after TP2. Nothing to do.</i>"]
    else:
        lines += [
            "",
            f"📋 <i>Close 1/{n} at each TP. After TP1, move stop to entry.</i>" if n > 1
            else "📋 <i>Close all at TP1.</i>",
            f"🕓 <i>Signal at {when}. Skip it if price already reached TP1.</i>",
        ]
    return "\n".join(lines)


def event_message(sig: Signal, ev: dict, cfg: dict, note: str = "") -> str:
    msg = _event_body(sig, ev, cfg)
    return msg + (f"\n{note}" if note else "")


def _event_body(sig: Signal, ev: dict, cfg: dict) -> str:
    one_r = r_usd(sig, cfg)
    name = f"{coin(sig.symbol)} {sig.side}"
    result = lambda label: f"💵 {label}: <b>{sig.realized_r:+.2f}R  ≈ {usd(sig.realized_r * one_r)}</b>"
    k = ev["kind"]
    if k == "tp":
        where = "entry" if ev["n"] == 1 else f"TP{ev['n'] - 1}"
        if sig.traded:
            cut, stop_txt = (f"✂️ 1/{len(sig.tps)} closed by the take-profit order.",
                             f"🔒 Stop moved to {where} ({px(ev['new_sl'])}) on Bybit.")
        else:
            cut = f"✂️ Close 1/{len(sig.tps)} of the position."
            stop_txt = (f"🔒 Move stop to entry ({px(ev['new_sl'])}). This trade can't lose now." if ev["n"] == 1
                        else f"🔒 Move stop to {px(ev['new_sl'])} (TP{ev['n'] - 1}).")
        return "\n".join([f"🎯 <b>{name}: TP{ev['n']} hit</b> at {px(ev['price'])}", cut, stop_txt,
                          result("Banked so far")])
    if k == "closed":
        return "\n".join([f"✋ <b>{name}: {html.escape(ev.get('why', 'closed'))}</b> at ≈ {px(ev['price'])}",
                          result("Result")])
    if k == "done":
        return "\n".join([f"🏆 <b>{name}: all targets hit!</b>", result("Result")])
    if k == "stop":
        if ev["after_tp"] == 0:
            title = f"🛑 <b>{name}: stopped out</b> at {px(ev['price'])}"
        elif ev["after_tp"] == 1:
            title = f"⚪ <b>{name}: closed at entry</b> (after TP1)"
        else:
            title = f"🔒 <b>{name}: trailing stop hit</b> at {px(ev['price'])}"
        return "\n".join([title, result("Result")])
    if k == "expired":
        return "\n".join([f"⌛ <b>{name}: closed after {ev['bars']} candles</b> at {px(ev['price'])}",
                          "No target or stop was hit in time.", result("Result")])
    return f"{name}: {k}"


def event_text(ev: dict) -> str:
    """Short text for the CSV log."""
    k = ev["kind"]
    if k == "tp":
        return f"TP{ev['n']} @ {ev['price']:.8g}, stop -> {ev['new_sl']:.8g}"
    if k == "stop":
        return f"stop @ {ev['price']:.8g} (after {ev['after_tp']} TP)"
    if k == "expired":
        return f"expired @ {ev['price']:.8g}"
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
        if not self.tg:
            return
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
                urllib.request.urlopen(url, urllib.parse.urlencode(params).encode(), timeout=10).read()
                return
            except urllib.error.HTTPError as e:
                body = e.read()[:300].decode(errors="replace")
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
