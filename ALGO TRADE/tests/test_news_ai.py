"""News intelligence: rules engine, AI engine (simulated client), trading reactions (offline)."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from signalbot import news, news_ai
from signalbot.news_ai import NewsAnalyst, rule_assess

COINS = ["BTC", "ETH", "SOL"]
PATS = news_ai.coin_patterns(COINS)
H = 3_600_000


def rules(title, **kw):
    return rule_assess({"title": title, **kw}, PATS)


class RulesTests(unittest.TestCase):
    def test_major_events(self):
        a = rules("Solana network halted after exploit drains $120M from bridge")
        self.assertEqual((a["severity"], a["direction"], a["coins"]), (3, "bearish", ["SOL"]))
        a = rules("SEC approves spot Solana ETF")
        self.assertEqual((a["severity"], a["direction"]), (3, "bullish"))
        a = rules("Bybit Will Delist the SOLUSDT Perpetual Contract", delist=True)
        self.assertEqual((a["severity"], a["direction"], a["coins"]), (3, "bearish", ["SOL"]))

    def test_notable_vs_minor(self):
        self.assertEqual(rules("Bitcoin ETFs lose $240M as CryptoQuant flags bearish market phase")["severity"], 2)
        macro = rules("Bitcoin, stocks wobble as Fed's Waller talks of 'shock' 50-point rate hike")
        self.assertEqual((macro["severity"], macro["direction"]), (2, "bearish"))   # hikes hurt crypto
        price = rules("SOL comes out of Breakpoint 7% higher as Solana exits beta")
        self.assertEqual((price["severity"], price["kind"]), (1, "price_move"))      # already happened
        opinion = rules("Bitcoin's 12% slide through key support looks scary, but history says give it 3 months")
        self.assertLessEqual(opinion["severity"], 1)

    def test_irrelevant(self):
        self.assertFalse(rules("Hong Kong wants banks to use AI to catch crypto crime")["relevant"])
        self.assertFalse(rules("Solar panels are cheap")["relevant"])
        self.assertFalse(rules("Delisting of XRPUSDT", delist=True)["relevant"])   # not your coin

    def test_duplicate_across_sites(self):
        earlier = [{"key": "k1", "title": "Bitcoin ETFs lose $240M as CryptoQuant flags bearish phase"}]
        a = rule_assess({"title": "Bitcoin ETFs lose $240M, CryptoQuant flags bearish phase"}, PATS, earlier)
        self.assertEqual(a["dup"], "k1")


class FakeMessages:
    def __init__(self, reply=None, exc=None, stop="end_turn"):
        self.reply, self.exc, self.stop, self.calls = reply, exc, stop, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return SimpleNamespace(stop_reason=self.stop, usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
                               content=[SimpleNamespace(type="thinking", thinking=""),   # read by type, not position
                                        SimpleNamespace(type="text", text=json.dumps(self.reply))])


def analyst(**kw):
    fake = FakeMessages(**kw)
    return NewsAnalyst({"news": {}}, client=SimpleNamespace(messages=fake)), fake


def row(i, **kw):
    base = {"id": i, "relevant": True, "scope": "coin", "coins": ["BTC"], "direction": "bearish", "severity": 2,
            "kind": "etf_flows", "why": "Big outflows.", "duplicate_of": 0}
    return {**base, **kw}


ITEMS = [{"title": "Spot bitcoin ETFs see record $2B outflow", "source": "CoinDesk", "t": 0, "summary": "s"},
         {"title": "Something about DOGE", "source": "Decrypt", "t": 0}]


class AITests(unittest.TestCase):
    def test_structured_request_and_mapping(self):
        a, fake = analyst(reply={"items": [row(1), row(2, relevant=False, scope="none", coins=["DOGE"], severity=7)]})
        out = a.assess(ITEMS, COINS, [{"key": "old", "title": "Earlier story"}])
        kw = fake.calls[0]
        self.assertEqual(kw["model"], "claude-haiku-5-5")
        self.assertEqual(kw["output_config"]["effort"], "low")
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kw["output_config"]["format"]["schema"]["properties"]["items"]["items"]
                         ["properties"]["coins"]["items"]["enum"], COINS)
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, kw)                        # rejected by Haiku 5.5
        self.assertIn("untrusted", kw["system"])
        self.assertEqual(kw["messages"][-1]["role"], "user")    # no prefill
        self.assertEqual((out[0]["severity"], out[0]["direction"], out[0]["by"]), (2, "bearish", "ai"))
        self.assertFalse(out[1]["relevant"])
        self.assertEqual(out[1]["coins"], [])                   # unknown coin dropped
        self.assertEqual(out[1]["severity"], 0)                 # invalid value sanitised
        self.assertEqual(a.cost_today()[0], 1)

    def test_duplicate_id_maps_to_earlier_story(self):
        a, _ = analyst(reply={"items": [row(1, duplicate_of=1001)]})
        out = a.assess(ITEMS[:1], COINS, [{"key": "old-key", "title": "Earlier story"}])
        self.assertEqual(out[0]["dup"], "old-key")

    def test_duplicate_within_the_same_batch(self):
        a, _ = analyst(reply={"items": [row(1), row(2, duplicate_of=1), row(3, duplicate_of=3)]})
        items = [dict(ITEMS[0], key="k1"), dict(ITEMS[0], key="k2"), dict(ITEMS[0], key="k3")]
        out = a.assess(items, COINS)
        self.assertEqual([o["dup"] for o in out], [None, "k1", None])   # can't be a duplicate of itself

    def test_refusal_or_bad_reply_falls_back_to_rules(self):
        for kw in ({"reply": {"items": []}, "stop": "refusal"}, {"reply": {"items": []}, "stop": "max_tokens"}):
            a, _ = analyst(**kw)
            out = a.assess(ITEMS[:1], COINS)
            self.assertEqual(out[0]["by"], "rules")
        a, fake = analyst(reply={"items": [row(1)]})
        fake.reply = "not json at all"
        self.assertEqual(a.assess(ITEMS[:1], COINS)[0]["by"], "rules")

    def test_rate_limit_pauses_ai_and_bad_key_disables_it(self):
        a, fake = analyst()
        fake.exc = a._sdk.RateLimitError()
        self.assertEqual(a.assess(ITEMS[:1], COINS)[0]["by"], "rules")
        self.assertFalse(a.active)                              # paused, not hammering the API
        self.assertIn("paused", a.label)
        a.pause_until = 0
        fake.exc = a._sdk.AuthenticationError()
        a.assess(ITEMS[:1], COINS)
        self.assertIsNone(a.client)
        self.assertIn("rejected", a.label)

    def test_no_key_means_rules(self):
        with mock.patch("signalbot.news_ai.env", return_value=""):
            a = NewsAnalyst({"news": {}})
        self.assertIn("ANTHROPIC_API_KEY", a.label)
        self.assertEqual(a.assess(ITEMS[:1], COINS)[0]["by"], "rules")


def story(title, direction="bearish", severity=3, coins=("SOL",), scope="coin", age_h=0.5, key=None):
    now = time.time() * 1000
    return {"key": key or title, "title": title, "source": "X", "link": "", "t": now - age_h * H, "seen_at": now,
            "relevant": True, "scope": scope, "coins": list(coins), "direction": direction, "severity": severity,
            "kind": "hack", "why": "Bridge exploited.", "by": "ai", "sources": 1}


class ReactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = {"news": {"calendar": False, "shock_guard": False, "ai": False, "block_hours": 4}}
        self.n = news.News(cfg, Path(self.tmp.name))
        self.now = time.time() * 1000

    def tearDown(self):
        self.tmp.cleanup()

    def test_major_news_blocks_only_trades_against_it(self):
        self.n.headlines.recent.appendleft(story("Solana bridge exploited for $120M"))
        self.assertIn("major bearish news on SOL", self.n.block_reason("SOLUSDT", self.now, "LONG"))
        self.assertEqual(self.n.block_reason("SOLUSDT", self.now, "SHORT"), "")   # shorts are fine
        self.assertEqual(self.n.block_reason("BTCUSDT", self.now, "LONG"), "")    # other coins are fine
        self.assertEqual(self.n.block_reason("SOLUSDT", self.now + 5 * H, "LONG"), "")   # expires

    def test_market_wide_major_news_blocks_every_coin(self):
        self.n.headlines.recent.appendleft(story("Fed makes surprise emergency rate hike", coins=(), scope="market"))
        self.assertIn("whole market", self.n.block_reason("BTCUSDT", self.now, "LONG"))
        self.assertIn("whole market", self.n.block_reason("ETHUSDT", self.now, "LONG"))

    def test_notable_news_does_not_block_but_sets_mood(self):
        for i in range(2):
            self.n.headlines.recent.appendleft(story(f"BTC outflows {i}", severity=2, coins=("BTC",)))
        self.assertEqual(self.n.block_reason("BTCUSDT", self.now, "LONG"), "")
        self.assertEqual(self.n.mood("BTC", self.now)[0], "bearish")
        self.assertEqual(self.n.alignment("BTCUSDT", "LONG", self.now), "against")
        self.assertEqual(self.n.alignment("BTCUSDT", "SHORT", self.now), "with")
        self.assertIn("leans against", self.n.context("BTCUSDT", "LONG", self.now))
        self.assertNotIn("leans against", self.n.context("BTCUSDT", "SHORT", self.now))
        self.assertEqual(self.n.context("ETHUSDT", "LONG", self.now), "")         # no ETH news: say nothing

    def test_news_against_open_trades_decides_once_per_story(self):
        self.n.headlines.last_new = [story("Solana staking outflows", severity=2),
                                     story("Solana bridge exploited for $120M", severity=3)]
        acts = self.n._react_open({"SOLUSDT": "LONG", "BTCUSDT": "LONG", "ETHUSDT": "SHORT"})
        self.assertEqual([(a["pair"], a["act"]) for a in acts], [("SOLUSDT", "protect"), ("SOLUSDT", "close")])
        self.assertEqual(self.n._react_open({"SOLUSDT": "LONG"}), [])            # once per story
        self.assertEqual(self.n._react_open({"SOLUSDT": "SHORT"}), [])           # bad news helps shorts

    def test_keyword_rules_never_close_everything_on_market_news(self):
        self.n.headlines.last_new = [dict(story("SEC bans crypto", coins=(), scope="market"), by="rules"),
                                     dict(story("Fed emergency hike", coins=(), scope="market", key="k2"), by="ai")]
        acts = self.n._react_open({"BTCUSDT": "LONG"})
        self.assertEqual([a["act"] for a in acts], ["protect", "close"])        # rules: protect; AI: close

    def test_exits_can_be_switched_off(self):
        self.n.cfg["news"].update(exit_on_major=False, protect_on_notable=False)
        self.n.headlines.last_new = [story("Solana bridge exploited", severity=3)]
        self.assertEqual(self.n._react_open({"SOLUSDT": "LONG"})[0]["act"], "warn")

    def test_poll_dedupes_counts_sources_and_alerts_major_loudly(self):
        h = self.n.headlines
        with mock.patch.object(h, "_fetch_all", return_value=[]):
            h.poll(COINS)                                       # first run: learn what exists
        now = time.time() * 1000
        feed = [{"title": "Solana network halted after exploit drains $120M", "link": "a", "t": now, "source": "A"},
                {"title": "Solana network halted after exploit drains $120M bridge", "link": "b", "t": now, "source": "B"},
                {"title": "Solar panels are cheap", "link": "c", "t": now, "source": "C"}]
        h.next_poll = 0
        with mock.patch.object(h, "_fetch_all", return_value=feed):
            out = h.poll(COINS)
        self.assertEqual(len(out), 1)                           # one story, not two
        self.assertEqual(out[0]["sources"], 2)                  # ...covered by two sites
        self.assertEqual(len(h.recent), 1)                      # irrelevant one not kept
        sent = []
        notifier = SimpleNamespace(alert=lambda m: sent.append(("loud", m)), info=lambda m, **k: sent.append(("quiet", m)))
        h.next_poll = 0
        h.seen.clear()
        h.recent.clear()
        with mock.patch.object(h, "_fetch_all", return_value=feed[:1]):
            acts = self.n.tick(["SOLUSDT"], lambda p: [], notifier, {"SOLUSDT": "LONG"})
        self.assertEqual([k for k, _ in sent], ["loud"])         # major headline, with sound
        self.assertIn("major", sent[0][1])
        self.assertEqual([(a["pair"], a["act"]) for a in acts], [("SOLUSDT", "close")])   # for the scanner

    def test_stories_survive_a_restart(self):
        h = self.n.headlines
        h.recent.appendleft(story("Solana bridge exploited"))
        h._save()
        again = news.News({"news": {"calendar": False, "shock_guard": False, "ai": False}}, Path(self.tmp.name))
        self.assertIn("major bearish", again.block_reason("SOLUSDT", self.now, "LONG"))

    def test_summary_shows_mood_and_engine(self):
        self.n.coins = ["SOL"]
        self.n.headlines.recent.appendleft(story("Solana bridge exploited"))
        text = self.n.summary(self.now)
        self.assertIn("SOL 🔴 bearish", text)
        self.assertIn("No new SOL longs", text)
        self.assertIn("keyword rules", text)


if __name__ == "__main__":
    unittest.main()
