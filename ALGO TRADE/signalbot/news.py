"""News and event awareness: protects trades, doesn't trade headlines.

Honest view: free news feeds arrive seconds to minutes after professional terminals, and the
price usually moves before a headline is public. Trading headlines "before others" isn't
realistic with free data. This module uses news defensively instead:

  1. Economic calendar: no NEW auto-trades shortly before and after high-impact releases
     (US CPI, FOMC, jobs report...), plus a heads-up on Telegram.
  2. Shock guard: detects sudden price moves itself from 1-minute candles. Price is the fastest
     news feed there is. You get an alert within ~1-2 minutes, and new entries on that coin
     pause for a while.
  3. Headlines: important crypto/macro headlines for your coins, silent and rate-limited.
  4. Bybit delisting announcements for your coins.
"""
from __future__ import annotations

import email.utils
import html
import json
import logging
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .data import Candle

log = logging.getLogger("signals")

CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FEEDS = {"CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
         "Cointelegraph": "https://cointelegraph.com/rss",
         "The Block": "https://www.theblock.co/rss.xml",
         "Decrypt": "https://decrypt.co/feed"}
BYBIT_DELIST = "https://api.bybit.com/v5/announcements/index?locale=en-US&type=delistings&limit=20"

NAMES = {"BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"], "XRP": ["ripple"],
         "DOGE": ["dogecoin"], "BNB": ["bnb chain"], "ADA": ["cardano"], "AVAX": ["avalanche"],
         "LINK": ["chainlink"], "DOT": ["polkadot"], "LTC": ["litecoin"], "SUI": ["sui network"],
         "TON": ["toncoin"], "TRX": ["tron"], "PEPE": ["pepe"], "SHIB": ["shiba inu"],
         "ARB": ["arbitrum"], "OP": ["optimism"], "APT": ["aptos"], "NEAR": ["near protocol"]}
MACRO = r"\b(fed|fomc|powell|cpi|inflation|interest rates?|rate (cut|hike)s?|payrolls|jobs report|tariffs?|" \
        r"recession|treasury|sec|etf|sanctions?|war)\b"
IMPACT = r"\b(hack(ed)?|exploit(ed)?|drain(ed)?|stolen|lawsuit|sues?|charges?|approv(e|es|ed|al)|" \
         r"reject(s|ed)?|bans?|banned|delist(s|ed|ing)?|halt(s|ed)?|outage|bankrupt(cy)?|insolven(t|cy)|" \
         r"emergency|crash(es|ed)?|plunges?|plummets?|surges?|soars?|record high|all-time high|" \
         r"liquidat(ed|ions?)|reserve|blackrock|executive order)\b"


def _get(url: str, timeout: float = 15, limit: int = 3_000_000) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (signalbot/1.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def _fmt(ms: float) -> str:
    return time.strftime("%a %H:%M", time.localtime(ms / 1000))


# ---------------------------------------------------------------- 1. economic calendar
class Calendar:
    def __init__(self, cfg: dict, cache: Path):
        n = cfg.get("news", {})
        self.countries, self.impact = set(n.get("countries", ["USD"])), set(n.get("impact", ["High"]))
        self.before, self.after = n.get("pause_before_min", 15) * 60_000, n.get("pause_after_min", 30) * 60_000
        self.cache, self.events, self.next_fetch = cache, [], 0.0
        if cache.exists():
            try:
                self._parse(json.loads(cache.read_text(encoding="utf-8")))
                self.next_fetch = cache.stat().st_mtime + 3 * 3600   # reuse a fresh copy across restarts
            except (ValueError, OSError):
                pass

    def _parse(self, rows: list) -> None:
        ev = []
        for e in rows:
            if e.get("country") in self.countries and e.get("impact") in self.impact:
                try:
                    t = datetime.fromisoformat(e["date"]).timestamp() * 1000
                except (KeyError, ValueError):
                    continue
                ev.append({"title": e.get("title", "?"), "country": e["country"], "t": t,
                           "forecast": e.get("forecast", ""), "previous": e.get("previous", ""),
                           "id": f"{e['country']}|{e.get('title')}|{int(t)}"})
        self.events = sorted(ev, key=lambda x: x["t"])

    def refresh(self) -> None:
        if time.time() < self.next_fetch:
            return
        try:
            raw = _get(CAL_URL)
            rows = json.loads(raw)
            self._parse(rows)
            self.cache.write_bytes(raw)
            self.next_fetch = time.time() + 3 * 3600    # the feed asks for gentle polling
        except Exception as e:
            log.warning("calendar: %s (using the saved copy)", e)
            limited = isinstance(e, urllib.error.HTTPError) and e.code == 429
            self.next_fetch = time.time() + (7200 if limited else 1800)

    def blackout(self, now_ms: float) -> dict | None:
        for e in self.events:
            if e["t"] - self.before <= now_ms <= e["t"] + self.after:
                return e
        return None

    def upcoming(self, now_ms: float, hours: float = 48) -> list[dict]:
        return [e for e in self.events if now_ms - self.after <= e["t"] <= now_ms + hours * 3_600_000]

    def label(self, e: dict) -> str:
        return f"{e['country']} {e['title']}"


# ---------------------------------------------------------------- 2. shock guard
class ShockGuard:
    def __init__(self, cfg: dict):
        n = cfg.get("news", {})
        self.mult, self.min_pct = n.get("shock_mult", 8), n.get("shock_min_pct", 1.2)
        self.pause = n.get("shock_pause_min", 30) * 60_000
        self.until: dict[str, float] = {}
        self.info: dict[str, dict] = {}

    def check(self, pair: str, cs: list[Candle], now_ms: float) -> dict | None:
        """cs: recent CLOSED 1m candles. Returns a shock dict the first time a shock is seen."""
        if len(cs) < 40 or self.until.get(pair, 0) > now_ms:
            return None
        base_rows = cs[-65:-5]
        base = sum(c.h - c.l for c in base_rows) / len(base_rows)
        ref, last = cs[-6].c, cs[-1].c
        move = last - ref
        pct = move / ref * 100
        if base <= 0 or abs(move) <= self.mult * base or abs(pct) < self.min_pct:
            return None
        self.until[pair] = now_ms + self.pause
        shock = {"pair": pair, "pct": pct, "x": abs(move) / base, "price": last, "t": now_ms,
                 "until": now_ms + self.pause}
        self.info[pair] = shock
        return shock

    def blocked(self, pair: str, now_ms: float) -> dict | None:
        return self.info.get(pair) if self.until.get(pair, 0) > now_ms else None


# ---------------------------------------------------------------- 3 + 4. headlines
def coin_patterns(coins: list[str]) -> dict[str, re.Pattern]:
    out = {}
    for c in coins:
        names = "|".join(re.escape(n) for n in NAMES.get(c, []))
        # ticker must be UPPERCASE as a whole word (so "sol" in normal text doesn't match)
        out[c] = re.compile(rf"(?<![A-Za-z]){re.escape(c)}(?![A-Za-z])" + (rf"|(?i:\b({names})\b)" if names else ""))
    return out


def parse_rss(raw: bytes, source: str) -> list[dict]:
    items = []
    root = ET.fromstring(raw)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title:
            continue
        t = 0.0
        try:
            t = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "").timestamp() * 1000
        except (TypeError, ValueError):
            pass
        items.append({"title": title, "link": link, "t": t, "source": source})
    return items


class Headlines:
    def __init__(self, cfg: dict, seen_path: Path):
        n = cfg.get("news", {})
        self.every = n.get("headline_every_sec", 180)
        self.max_per_hour = n.get("headline_max_per_hour", 6)
        self.delist = n.get("bybit_delistings", True)
        self.path = seen_path
        self.seen: dict[str, float] = {}
        self.first = True
        if seen_path.exists():
            try:
                self.seen = json.loads(seen_path.read_text(encoding="utf-8"))
                self.first = False
            except ValueError:
                pass
        self.recent: deque = deque(maxlen=30)
        self.sent: deque = deque(maxlen=100)
        self.next_poll = 0.0

    def _fetch_all(self) -> list[dict]:
        def one(item):
            name, url = item
            try:
                return parse_rss(_get(url), name)
            except Exception as e:
                log.debug("feed %s: %s", name, e)
                return []
        with ThreadPoolExecutor(max_workers=5) as pool:
            out = [x for lst in pool.map(one, FEEDS.items()) for x in lst]
        if self.delist:
            try:
                d = json.loads(_get(BYBIT_DELIST))
                for a in d.get("result", {}).get("list", []):
                    out.append({"title": a.get("title", ""), "link": a.get("url", ""),
                                "t": float(a.get("publishTime") or 0), "source": "Bybit", "delist": True})
            except Exception as e:
                log.debug("bybit announcements: %s", e)
        return out

    def poll(self, coins: list[str]) -> list[dict]:
        """New important items to alert on (already rate-limited)."""
        if time.time() < self.next_poll:
            return []
        self.next_poll = time.time() + self.every
        pats = coin_patterns(coins)
        now = time.time() * 1000
        alerts = []
        for it in self._fetch_all():
            key = it["link"] or it["title"]
            if key in self.seen:
                continue
            self.seen[key] = now
            hit = [c for c, p in pats.items() if p.search(it["title"])]
            macro = re.search(MACRO, it["title"], re.I)
            impact = re.search(IMPACT, it["title"], re.I)
            if it.get("delist"):
                important = bool(hit)
            else:
                important = bool(impact and (hit or macro)) or bool(macro and hit)
            if not (hit or macro or it.get("delist") and hit):
                continue
            it["coins"] = hit
            self.recent.appendleft(it)
            fresh = not it["t"] or now - it["t"] < 2 * 3_600_000
            if important and fresh and not self.first:
                alerts.append(it)
        self.first = False
        self._save()
        # rate limit
        out = []
        for it in alerts:
            if sum(1 for t in self.sent if now - t < 3_600_000) >= self.max_per_hour:
                break
            self.sent.append(now)
            out.append(it)
        return out

    def _save(self) -> None:
        cut = time.time() * 1000 - 7 * 86_400_000
        self.seen = {k: v for k, v in self.seen.items() if v > cut}
        try:
            self.path.write_text(json.dumps(self.seen), encoding="utf-8")
        except OSError as e:
            log.warning("news cache: %s", e)


def headline_message(it: dict) -> str:
    tag = "🚨 <b>Bybit delisting</b>" if it.get("delist") else "📰 <b>News</b>"
    coins = f"  ·  {', '.join(it['coins'])}" if it.get("coins") else ""
    link = f'\n<a href="{html.escape(it["link"], quote=True)}">{html.escape(it["source"])}</a>' if it["link"] else ""
    return f"{tag}{coins}\n{html.escape(it['title'])}{link}"


# ---------------------------------------------------------------- orchestration
class News:
    def __init__(self, cfg: dict, log_dir: Path):
        n = cfg.get("news", {})
        self.cfg = cfg
        self.calendar = Calendar(cfg, log_dir / "calendar.json") if n.get("calendar", True) else None
        self.shock = ShockGuard(cfg) if n.get("shock_guard", True) else None
        self.headlines = Headlines(cfg, log_dir / "news_seen.json") if n.get("headlines", True) else None
        self.heads_up = n.get("heads_up_min", 30) * 60_000
        self.warned: set[str] = set()

    def status_line(self) -> str:
        parts = [name for name, on in (("calendar", self.calendar), ("price shocks", self.shock),
                                       ("headlines", self.headlines)) if on]
        return "📰 News guard: " + (", ".join(parts) if parts else "off")

    def block_reason(self, pair: str, now_ms: float) -> str:
        """Why a NEW trade on this pair should wait right now ('' = fine)."""
        if self.calendar:
            e = self.calendar.blackout(now_ms)
            if e:
                return (f"high-impact news: {html.escape(self.calendar.label(e))} at {_fmt(e['t'])} "
                        f"(paused until {_fmt(e['t'] + self.calendar.after)})")
        if self.shock:
            s = self.shock.blocked(pair, now_ms)
            if s:
                return f"price shock {s['pct']:+.1f}% at {_fmt(s['t'])} (paused until {_fmt(s['until'])})"
        return ""

    def tick(self, pairs: list[str], get_1m, notifier) -> None:
        """Run once per scanner loop. get_1m(pair) -> recent closed 1m candles."""
        now = time.time() * 1000
        if self.calendar:
            self.calendar.refresh()
            for e in self.calendar.events:
                if 0 <= e["t"] - now <= self.heads_up and e["id"] not in self.warned:
                    self.warned.add(e["id"])
                    extra = "  ·  ".join(x for x in (f"forecast {e['forecast']}" if e["forecast"] else "",
                                                       f"previous {e['previous']}" if e["previous"] else "") if x)
                    notifier.info(f"📅 <b>{html.escape(self.calendar.label(e))}</b> at {_fmt(e['t'])} "
                                  f"(in {int((e['t'] - now) / 60_000)} min)"
                                  + (f"\n{html.escape(extra)}" if extra else "")
                                  + f"\nBig moves are likely. New auto-trades pause "
                                    f"{self.calendar.before // 60_000} min before until "
                                    f"{self.calendar.after // 60_000} min after.")
        if self.shock:
            for p in pairs:
                try:
                    s = self.shock.check(p, get_1m(p), now)
                except Exception as e:
                    log.debug("shock %s: %s", p, e)
                    continue
                if s:
                    coin = p[:-4] if p.endswith("USDT") else p
                    related = [h for h in (self.headlines.recent if self.headlines else [])
                               if coin in h.get("coins", []) and now - (h["t"] or now) < 3_600_000][:2]
                    news = "".join(f"\n• {html.escape(h['title'])}" for h in related)
                    notifier.alert(f"⚡ <b>{coin} moved {s['pct']:+.1f}% in 5 min</b> (≈{s['x']:.0f}× normal)\n"
                                   f"Now {s['price']:,.6g}. Possible news."
                                   + (f"\nRecent headlines:{news}" if news else "")
                                   + f"\nNo new {coin} entries until {_fmt(s['until'])}. "
                                     "Open trades keep their stop-loss.")
        if self.headlines:
            coins = sorted({p[:-4] if p.endswith("USDT") else p for p in pairs})
            for it in self.headlines.poll(coins):
                notifier.info(headline_message(it))

    def summary(self, now_ms: float) -> str:
        lines = ["📰 <b>News</b>"]
        if self.calendar:
            ev = self.calendar.upcoming(now_ms)
            lines.append("\n📅 <b>Big economic events (48 h)</b>")
            lines += [f"{'⏳ ' if e['t'] > now_ms else '✔️ '}{_fmt(e['t'])}  {html.escape(self.calendar.label(e))}"
                      for e in ev] or ["none scheduled"]
        if self.shock:
            active = [s for p, s in self.shock.info.items() if self.shock.blocked(p, now_ms)]
            if active:
                lines.append("\n⚡ <b>Paused after price shocks</b>")
                lines += [f"{s['pair'][:-4]} {s['pct']:+.1f}% until {_fmt(s['until'])}" for s in active]
        if self.headlines:
            lines.append("\n🗞 <b>Latest relevant headlines</b>")
            latest = sorted(self.headlines.recent, key=lambda x: x["t"], reverse=True)[:6]
            lines += ([f"• {html.escape(h['title'])} <i>({html.escape(h['source'])})</i>" for h in latest]
                      or ["none yet (checks every few minutes)"])
        return "\n".join(lines)
