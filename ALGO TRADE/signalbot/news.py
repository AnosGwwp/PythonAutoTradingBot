"""News and event awareness: protects trades, doesn't trade headlines.

Honest view: free news feeds arrive seconds to minutes after professional terminals, and the
price usually moves before a headline is public. Trading headlines "before others" isn't
realistic with free data. This module uses news defensively instead:

  1. Economic calendar: no NEW auto-trades shortly before and after high-impact releases
     (US CPI, FOMC, jobs report...), plus a heads-up on Telegram.
  2. Shock guard: detects sudden price moves itself from 1-minute candles. Price is the fastest
     news feed there is. You get an alert within ~1-2 minutes, and new entries on that coin
     pause for a while.
  3. Headlines, read like a trader would (news_ai.py: Claude, or keyword rules as fallback):
     which coin, good or bad, how serious, repeated on other sites or new. Then:
       * MAJOR news blocks new auto-trades AGAINST it on that coin for `block_hours`
         (market-wide major news: on every coin)
       * serious news against an OPEN trade sends a loud warning (the bot doesn't close it)
       * every signal shows the coin's "news mood"; the mood is journaled for /review
  4. Bybit delisting announcements for your coins (always major).
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
from .news_ai import SEVERITY, NewsAnalyst, _similar, coin_patterns  # noqa: F401  (coin_patterns re-exported)

log = logging.getLogger("signals")

CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FEEDS = {"CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
         "Cointelegraph": "https://cointelegraph.com/rss",
         "The Block": "https://www.theblock.co/rss.xml",
         "Decrypt": "https://decrypt.co/feed"}
BYBIT_DELIST = "https://api.bybit.com/v5/announcements/index?locale=en-US&type=delistings&limit=20"

H = 3_600_000
FRESH_MS = 12 * H          # older headlines are ignored entirely
ALERT_FRESH_MS = 2 * H     # only alert on news published in the last 2 hours
MOOD_HOURS = 12
DIR_ICON = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}


def _get(url: str, timeout: float = 15, limit: int = 3_000_000, validators: dict | None = None) -> bytes | None:
    """Download url. With `validators` (a dict kept by the caller) the request is conditional:
    an unchanged feed answers 304 with no body, and this returns None."""
    headers = {"User-Agent": "Mozilla/5.0 (signalbot/1.0)"}
    seen = (validators or {}).get(url, {})
    if seen.get("etag"):
        headers["If-None-Match"] = seen["etag"]
    if seen.get("modified"):
        headers["If-Modified-Since"] = seen["modified"]
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if validators is not None:
                validators[url] = {"etag": r.headers.get("ETag"), "modified": r.headers.get("Last-Modified")}
            return r.read(limit)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None                      # not modified since the last download
        raise


def _fmt(ms: float) -> str:
    return time.strftime("%a %H:%M", time.localtime(ms / 1000))


def _ago(ms: float) -> str:
    m = max(0, int(ms / 60_000))
    return f"{m} min ago" if m < 60 else f"{m // 60} h ago"


def _coin(pair: str) -> str:
    return pair[:-4] if pair.endswith("USDT") else pair


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
        summary = html.unescape(re.sub(r"<[^>]+>", " ", it.findtext("description") or ""))
        items.append({"title": title, "link": link, "t": t, "source": source,
                      "summary": " ".join(summary.split())[:300]})
    return items


def story_time(s: dict) -> float:
    """When the story happened: its publish time, else when the bot first saw it."""
    return s.get("t") or s.get("seen_at") or 0.0


def concerns(s: dict, coin: str) -> bool:
    return s.get("relevant") and (s.get("scope") == "market" or coin in (s.get("coins") or []))


class Headlines:
    """Fetches the feeds, has each NEW headline assessed (news_ai), keeps the stories."""

    def __init__(self, cfg: dict, seen_path: Path, analyst: NewsAnalyst | None = None):
        n = cfg.get("news", {})
        self.every = n.get("headline_every_sec", 180)
        self.max_per_hour = n.get("headline_max_per_hour", 6)
        self.delist = n.get("bybit_delistings", True)
        self.analyst = analyst or NewsAnalyst(cfg)
        self.path = seen_path
        self.stories_path = seen_path.with_name("news_stories.json")
        self.seen: dict[str, float] = {}
        self.first = True
        if seen_path.exists():
            try:
                self.seen = json.loads(seen_path.read_text(encoding="utf-8"))
                self.first = False
            except ValueError:
                pass
        self._validators: dict[str, dict] = {}   # ETag / Last-Modified per feed (conditional GET)
        self.recent: deque = deque(maxlen=300)   # relevant stories, newest first (kept 48 h)
        try:
            cut = time.time() * 1000 - 48 * H
            stories = json.loads(self.stories_path.read_text(encoding="utf-8"))
            self.recent.extend(s for s in stories if s.get("seen_at", 0) > cut)
        except (OSError, ValueError):
            pass
        self.sent: deque = deque(maxlen=100)
        self.last_new: list[dict] = []           # stories added by the latest poll
        self.next_poll = 0.0

    def _fetch_all(self) -> list[dict]:
        def one(item):
            name, url = item
            try:
                raw = _get(url, validators=self._validators)
                return [] if raw is None else parse_rss(raw, name)      # None: feed unchanged
            except Exception as e:
                log.debug("feed %s: %s", name, e)
                return []
        with ThreadPoolExecutor(max_workers=5) as pool:
            out = [x for lst in pool.map(one, FEEDS.items()) for x in lst]
        if self.delist:
            try:
                d = json.loads(_get(BYBIT_DELIST))
                for a in d.get("result", {}).get("list", []):
                    out.append({"title": a.get("title", ""), "link": a.get("url", ""), "summary": "",
                                "t": float(a.get("publishTime") or 0), "source": "Bybit", "delist": True})
            except Exception as e:
                log.debug("bybit announcements: %s", e)
        return out

    def poll(self, coins: list[str]) -> list[dict]:
        """Fetch, assess new headlines, store the relevant ones. Returns the stories to alert on
        (rate-limited; major ones always pass). The first run only learns what exists."""
        if time.time() < self.next_poll:
            return []
        self.next_poll = time.time() + self.every
        now = time.time() * 1000
        new = []
        for it in self._fetch_all():
            key = it["link"] or it["title"]
            if key in self.seen:
                continue
            self.seen[key] = now
            if it["t"] and now - it["t"] > FRESH_MS:
                continue                                   # old news
            new.append(dict(it, key=key))
        self.last_new, alerts = [], []
        if new:
            earlier = [s for s in self.recent if s.get("severity", 0) >= 1]
            by_key = {s["key"]: s for s in self.recent if s.get("key")}
            for it, a in zip(new, self.analyst.assess(new, coins, earlier)):
                if not a.get("dup"):                       # also catch repeats within this poll
                    a["dup"] = next((s["key"] for s in self.last_new if _similar(it["title"], s["title"])), None)
                if a.get("dup") in by_key:                 # the same story from another site
                    by_key[a["dup"]]["sources"] = by_key[a["dup"]].get("sources", 1) + 1
                    continue
                if not a.get("relevant"):
                    continue
                story = {**it, **a, "sources": 1, "seen_at": now}
                self.recent.appendleft(story)
                by_key[story["key"]] = story
                self.last_new.append(story)
                if story["severity"] >= 2 and not self.first and (not it["t"] or now - it["t"] < ALERT_FRESH_MS):
                    alerts.append(story)
        if self.first:
            self.last_new = []                             # nothing is "new" on the very first run
        self.first = False
        self._save()
        out = []
        for s in sorted(alerts, key=lambda x: -x["severity"]):
            if s["severity"] < 3 and sum(1 for t in self.sent if now - t < H) >= self.max_per_hour:
                continue
            self.sent.append(now)
            out.append(s)
        return out

    def _save(self) -> None:
        cut = time.time() * 1000 - 7 * 86_400_000
        self.seen = {k: v for k, v in self.seen.items() if v > cut}
        try:
            self.path.write_text(json.dumps(self.seen), encoding="utf-8")
            self.stories_path.write_text(json.dumps(list(self.recent)), encoding="utf-8")
        except OSError as e:
            log.warning("news cache: %s", e)


def headline_message(s: dict) -> str:
    sev = s.get("severity", 0)
    head = "🚨 <b>Bybit delisting</b>" if s.get("delist") else ("🚨" if sev >= 3 else "📰") + " <b>News</b>"
    who = ", ".join(s.get("coins") or []) or ("whole market" if s.get("scope") == "market" else "")
    tags = "  ·  ".join(x for x in (who, f"{DIR_ICON.get(s.get('direction'), '')} {s.get('direction', '')}",
                                    SEVERITY.get(sev, "")) if x.strip())
    lines = [f"{head}  ·  {html.escape(tags)}", html.escape(s["title"])]
    if s.get("why") and s.get("by") == "ai":
        lines.append(f"<i>{html.escape(s['why'])}</i>")
    src = html.escape(s.get("source", ""))
    lines.append(f'<a href="{html.escape(s["link"], quote=True)}">{src}</a>' if s.get("link") else src)
    return "\n".join(lines)


# ---------------------------------------------------------------- orchestration
class News:
    def __init__(self, cfg: dict, log_dir: Path, analyst: NewsAnalyst | None = None):
        n = cfg.get("news", {})
        self.cfg = cfg
        self.calendar = Calendar(cfg, log_dir / "calendar.json") if n.get("calendar", True) else None
        self.shock = ShockGuard(cfg) if n.get("shock_guard", True) else None
        self.headlines = (Headlines(cfg, log_dir / "news_seen.json", analyst or NewsAnalyst(cfg))
                          if n.get("headlines", True) else None)
        self.heads_up = n.get("heads_up_min", 30) * 60_000
        self.block_ms = n.get("block_hours", 4) * H
        self.block_sev = n.get("block_min_severity", 3)
        self.warn_open = n.get("warn_open_trades", True)
        self.warned: set[str] = set()
        self.warned_open: set[tuple[str, str]] = set()
        self.coins: list[str] = []

    def status_line(self) -> str:
        parts = [name for name, on in (("calendar", self.calendar), ("price shocks", self.shock),
                                       ("headlines", self.headlines)) if on]
        line = "📰 News guard: " + (", ".join(parts) if parts else "off")
        if self.headlines:
            line += f"\n🧠 Headlines read by: {html.escape(self.headlines.analyst.label)}"
        return line

    # ---- what the stories mean for one coin
    def stories(self, coin: str, now_ms: float, hours: float) -> list[dict]:
        if not self.headlines:
            return []
        return [s for s in self.headlines.recent if concerns(s, coin) and now_ms - story_time(s) <= hours * H]

    def mood(self, coin: str, now_ms: float) -> tuple[str, float, list[dict]]:
        """Recent news leaning for a coin: ('bullish'|'bearish'|'mixed'|'quiet', score, stories)."""
        items = [s for s in self.stories(coin, now_ms, MOOD_HOURS) if s.get("severity", 0) >= 1]
        score = 0.0
        for s in items:
            sign = {"bullish": 1, "bearish": -1}.get(s.get("direction"), 0)
            weight = {1: 0.5, 2: 1.0, 3: 2.0}.get(s.get("severity"), 0) * (0.5 if s.get("scope") == "market" else 1)
            score += sign * weight * 0.5 ** ((now_ms - story_time(s)) / (6 * H))
        if not items:
            return "quiet", 0.0, []
        return ("bullish" if score >= 0.75 else "bearish" if score <= -0.75 else "mixed"), score, items

    def alignment(self, pair: str, side: str, now_ms: float) -> str:
        """'with' / 'against' / 'none': how a new trade sits with the coin's news (for /review)."""
        m, _, _ = self.mood(_coin(pair), now_ms)
        if m not in ("bullish", "bearish"):
            return "none"
        return "with" if (m == "bullish") == (side == "LONG") else "against"

    def context(self, pair: str, side: str, now_ms: float) -> str:
        """One line for a signal alert: the coin's news mood, flagged if it leans against the trade."""
        coin = _coin(pair)
        m, _, items = self.mood(coin, now_ms)
        if not items:
            return ""
        top = max(items, key=lambda s: (s.get("severity", 0), story_time(s)))
        line = (f"📰 News on {coin} (12 h): <b>{m}</b>, {len(items)} stor{'y' if len(items) == 1 else 'ies'}. "
                f"Latest important: {html.escape(top['title'][:90])}")
        if self.alignment(pair, side, now_ms) == "against":
            line += "\n⚠️ The news leans against this trade."
        return line

    def block_reason(self, pair: str, now_ms: float, side: str | None = None) -> str:
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
        if side:
            against = "bearish" if side == "LONG" else "bullish"
            for s in self.stories(_coin(pair), now_ms, self.block_ms / H):
                if s.get("severity", 0) >= self.block_sev and s.get("direction") == against:
                    where = _coin(pair) if s.get("scope") == "coin" else "the whole market"
                    return (f"major {against} news on {where}: {html.escape(s['title'][:90])} "
                            f"(no new {side.lower()}s until {_fmt(story_time(s) + self.block_ms)})")
        return ""

    def tick(self, pairs: list[str], get_1m, notifier, open_sides: dict[str, str] | None = None) -> list[dict]:
        """Run once per scanner loop. get_1m(pair) -> recent closed 1m candles.
        open_sides: {pair: "LONG"/"SHORT"} of open trades. Returns what news means for them
        (see _react_open); the scanner carries it out, since it holds the trades and the exchange."""
        now = time.time() * 1000
        actions: list[dict] = []
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
        if self.headlines:
            self.coins = sorted({_coin(p) for p in pairs})
            for s in self.headlines.poll(self.coins):
                (notifier.alert if s["severity"] >= 3 else notifier.info)(headline_message(s))
            if self.warn_open and open_sides:
                actions = self._react_open(open_sides)
        if self.shock:
            for p in pairs:
                try:
                    s = self.shock.check(p, get_1m(p), now)
                except Exception as e:
                    log.debug("shock %s: %s", p, e)
                    continue
                if s:
                    coin = _coin(p)
                    related = sorted(self.stories(coin, now, 1), key=lambda x: -x.get("severity", 0))[:2]
                    news = "".join(f"\n• {DIR_ICON.get(h.get('direction'), '')} {html.escape(h['title'])}"
                                   for h in related)
                    notifier.alert(f"⚡ <b>{coin} moved {s['pct']:+.1f}% in 5 min</b> (≈{s['x']:.0f}× normal)\n"
                                   f"Now {s['price']:,.6g}. "
                                   + (f"Likely cause, from the last hour:{news}" if news else
                                      "No related headline yet: price often moves before the news is out.")
                                   + f"\nNo new {coin} entries until {_fmt(s['until'])}. "
                                     "Open trades keep their stop-loss.")
        return actions

    def _react_open(self, open_sides: dict[str, str]) -> list[dict]:
        """What new stories mean for OPEN trades. Each story acts once per trade:
          close   - major news against the trade (market-wide major news only when the AI read it:
                    keyword rules alone never close every trade at once)
          protect - notable news against the trade: stop to entry if it is in profit, else warn
          warn    - when exits are switched off in signals.toml"""
        n = self.cfg.get("news", {})
        out = []
        for s in self.headlines.last_new:
            if s.get("severity", 0) < 2 or s.get("direction") not in ("bullish", "bearish"):
                continue
            if s.get("scope") == "market" and s["severity"] < 3:
                continue                                   # market-wide: only major news counts
            for pair, side in open_sides.items():
                against = "bearish" if side == "LONG" else "bullish"
                if s["direction"] != against or not concerns(s, _coin(pair)) or (s["key"], pair) in self.warned_open:
                    continue
                self.warned_open.add((s["key"], pair))
                trusted = s.get("scope") == "coin" or s.get("by") == "ai"
                if s["severity"] >= 3 and trusted and n.get("exit_on_major", True):
                    act = "close"
                elif n.get("protect_on_notable", True):
                    act = "protect"
                else:
                    act = "warn"
                out.append({"pair": pair, "side": side, "story": s, "act": act})
        return out

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
                lines += [f"{_coin(s['pair'])} {s['pct']:+.1f}% until {_fmt(s['until'])}" for s in active]
        if self.headlines:
            if self.coins:
                icon = {"bullish": "🟢", "bearish": "🔴", "mixed": "⚪", "quiet": "💤"}
                moods = [(c, self.mood(c, now_ms)[0]) for c in self.coins]
                lines.append("\n🧭 <b>Mood (12 h)</b>   " + "  ·  ".join(f"{c} {icon[m]} {m}" for c, m in moods))
                for c in self.coins:
                    for side in ("LONG", "SHORT"):
                        why = self.block_reason(c + "USDT", now_ms, side)
                        if why.startswith("major"):
                            lines.append(f"🚫 No new {c} {side.lower()}s: {why}")
            lines.append("\n🗞 <b>Top stories (12 h)</b>")
            top = sorted((s for s in self.headlines.recent
                          if s.get("severity", 0) >= 1 and now_ms - story_time(s) <= 12 * H),
                         key=lambda s: (-s.get("severity", 0), -story_time(s)))[:6]
            for s in top:
                who = ", ".join(s.get("coins") or []) or "market"
                more = f" +{s['sources'] - 1} sites" if s.get("sources", 1) > 1 else ""
                lines.append(f"{DIR_ICON.get(s.get('direction'), '')} <b>{SEVERITY.get(s.get('severity'), '')}</b> · "
                             f"{who} · {_ago(now_ms - story_time(s))}{more}\n{html.escape(s['title'])}"
                             + (f"\n<i>{html.escape(s['why'])}</i>" if s.get("by") == "ai" and s.get("why") else ""))
            if not top:
                lines.append("nothing important (checks every few minutes)")
            a = self.headlines.analyst
            calls, cost = a.cost_today()
            lines.append(f"\n🧠 Read by {html.escape(a.label)}" + (f"  ·  today {calls} requests ≈ ${cost:.3f}" if calls else ""))
        return "\n".join(lines)
