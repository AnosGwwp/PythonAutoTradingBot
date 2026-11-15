"""`python signals.py check`: verifies everything the bot needs. Run it first on a new VPS."""
from __future__ import annotations

import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

from . import data
from .bybit import Bybit
from .notify import Notifier, env

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


def run(cfg: dict) -> int:
    results: list[tuple[str, str, str, bool]] = []

    def check(name: str, fn, critical: bool = True):
        t = time.time()
        try:
            level, detail = fn()
        except Exception as e:
            level, detail = FAIL, f"{type(e).__name__}: {str(e)[:160]}"
        ms = (time.time() - t) * 1000
        results.append((name, level, detail, critical))
        print(f"[{level}] {name:<26} {detail}  ({ms:.0f} ms)", flush=True)

    print(f"\nHealth check on {socket.gethostname()}\n")
    check("Python", lambda: (OK, sys.version.split()[0]))

    def logs():
        d = Path(cfg["alerts"]["log_dir"])
        d.mkdir(parents=True, exist_ok=True)
        (d / ".write_test").write_text("ok")
        (d / ".write_test").unlink()
        return OK, f"{d.resolve()} writable"
    check("Log folder", logs)

    def clock():
        r = json.loads(urllib.request.urlopen("https://api.bybit.com/v5/market/time", timeout=10).read())
        off = int(r["result"]["timeNano"]) / 1e6 - time.time() * 1000
        lvl = OK if abs(off) < 1000 else WARN
        return lvl, f"clock is {off:+.0f} ms vs Bybit" + ("" if lvl == OK else " (enable NTP: timedatectl set-ntp true)")
    check("Clock", clock)

    def source(fn, name):
        def inner():
            cs = data._fetch(fn, "BTCUSDT", "1h", 5)
            return OK, f"BTC 1h close {cs[-1].c:,.1f}"
        return inner
    check("Binance data", source(data._binance, "binance"), critical=False)
    check("Bybit data", source(data._bybit, "bybit"), critical=False)

    def tg():
        if not cfg["alerts"].get("telegram"):
            return WARN, "telegram = false in signals.toml"
        n = Notifier(cfg)
        if not n.tg:
            return FAIL, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set (.env)"
        me = json.loads(urllib.request.urlopen(f"https://api.telegram.org/bot{n.tg_token}/getMe", timeout=10).read())
        n.info(f"✅ Health check from <b>{socket.gethostname()}</b>: Telegram works.")
        return OK, f"@{me['result']['username']} -> test message sent to chat {n.tg_chat}"
    check("Telegram", tg)

    t = cfg.get("trading", {})

    def bybit_account():
        key, secret = env("BYBIT_API_KEY"), env("BYBIT_API_SECRET")
        if not (key and secret):
            return (FAIL if t.get("enabled") else WARN), "BYBIT_API_KEY / BYBIT_API_SECRET not set (.env)"
        ex = Bybit(key, secret, t.get("env", "demo"))
        ex.sync_time()
        w = ex.wallet()
        info = ex.get("/v5/user/query-api")
        notes = [f"{t.get('env', 'demo')}: equity ${w['equity']:,.2f}, free ${w['available']:,.2f}"]
        level = OK
        perms = info.get("permissions", {})
        if info.get("readOnly"):
            level, notes = FAIL, notes + ["key is READ-ONLY, can't trade"]
        if "Withdraw" in json.dumps(perms.get("Wallet", [])):
            level = WARN
            notes.append("key can WITHDRAW, make a new key without that permission")
        if info.get("ips") in (["*"], [], None) and t.get("env") == "mainnet":
            level = WARN if level == OK else level
            notes.append("not IP-restricted; lock it to the VPS IP")
        if info.get("expiredAt"):
            notes.append(f"expires {info['expiredAt'][:10]}")
        return level, "; ".join(notes)
    check("Bybit account", bybit_account, critical=bool(t.get("enabled")))

    def public_ip():
        ip = urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode()
        return OK, f"{ip} (use this for the Bybit key's IP restriction)"
    check("Public IP", public_ip, critical=False)

    def feed(url, what):
        def inner():
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (signalbot/1.0)"})
            n = len(urllib.request.urlopen(req, timeout=15).read())
            return OK, f"{what} reachable ({n // 1024} KB)"
        return inner
    from .news import FEEDS, Calendar

    def calendar():
        # goes through the bot's own cache, so this check doesn't burn the feed's rate limit
        c = Calendar(cfg, Path(cfg["alerts"]["log_dir"]) / "calendar.json")
        c.refresh()
        if not c.events and not c.cache.exists():
            return WARN, "not reachable right now (the bot retries later)"
        return OK, f"{len(c.events)} high-impact events this week"
    check("Economic calendar", calendar, critical=False)
    check("News feed", feed(FEEDS["CoinDesk"], "CoinDesk"), critical=False)

    data_ok = any(n in ("Binance data", "Bybit data") and lvl == OK for n, lvl, _, _ in results)
    bad = [n for n, lvl, _, crit in results if lvl == FAIL and crit]
    if not data_ok:
        bad.append("market data (neither Binance nor Bybit reachable from here)")
    print()
    if bad:
        print("PROBLEMS: " + ", ".join(bad))
        return 1
    print("All critical checks passed." + (" (see WARN lines)" if any(r[1] == WARN for r in results) else ""))
    return 0
