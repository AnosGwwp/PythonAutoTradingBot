"""Word list (lexicon) readings and CryptoPanic votes (offline)."""
import io
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from signalbot import lexicon, news, news_ai

PATS = news_ai.coin_patterns(["BTC", "ETH", "SOL"])


def read(title, **kw):
    return news_ai.rule_assess({"title": title, **kw}, PATS)


class LexiconTests(unittest.TestCase):
    def test_size_and_every_phrase_compiles(self):
        self.assertGreater(lexicon.size(), 500)

    def test_negation_flips_and_is_never_major(self):
        a = read("Solana Foundation denies exploit, says funds are safe")
        self.assertNotEqual(a["direction"], "bearish")
        self.assertLess(a["severity"], 3)
        self.assertNotEqual(read("No evidence of hack on Ethereum bridge")["severity"], 3)

    def test_longest_phrase_wins(self):
        self.assertEqual(read("US lifts ban on Bitcoin mining")["direction"], "bullish")
        self.assertEqual(read("Short squeeze sends ETH soaring")["direction"], "bullish")
        self.assertEqual(read("Long liquidations top $500M as bitcoin slides")["direction"], "bearish")

    def test_amounts(self):
        self.assertEqual(read("Hackers drain $150 million from Solana DeFi protocol")["severity"], 3)
        self.assertLess(read("Solana memecoin site exploited for $300K")["severity"], 3)   # small incident

    def test_hedges_and_aftermath_are_minor(self):
        self.assertLessEqual(read("Bitcoin could crash to $40K, analyst warns")["severity"], 1)
        self.assertLessEqual(read("Ethereum hacker sentenced to 6 years over $11M theft")["severity"], 1)
        self.assertLessEqual(read("CryptoQuant: bitcoin enters bearish phase")["severity"], 1)

    def test_commentary_does_not_hide_a_hard_fact(self):
        self.assertEqual(read("Bitcoin ETFs see record $1.2 billion outflows, CryptoQuant data shows")["severity"], 2)

    def test_macro_direction(self):
        self.assertEqual(read("Fed cuts rates by 50 basis points")["direction"], "bullish")
        self.assertEqual(read("US inflation hotter than expected in October")["direction"], "bearish")

    def test_court_wins_are_good_news(self):
        a = read("Judge clears Ethereum developers of $25M fraud charges")
        self.assertEqual(a["direction"], "bullish")

    def test_other_coins_are_not_market_news(self):
        self.assertFalse(read("XRP whales dump $270 million during the selloff")["relevant"])

    def test_explanation_lists_the_words(self):
        self.assertIn("'hackers'", read("Hackers drain $150 million from Solana DeFi protocol")["why"])

    def test_aliases(self):
        self.assertEqual(read("Saylor's Strategy buys another 5,000 bitcoin")["coins"], ["BTC"])
        self.assertEqual(read("Vitalik proposes new gas limit")["coins"], ["ETH"])
        self.assertEqual(read("Bitcoin Cash hard fork goes live")["coins"], [])           # not BTC


class VoteTests(unittest.TestCase):
    def story(self, **kw):
        base = {"relevant": True, "direction": "neutral", "severity": 1, "by": "rules", "scope": "coin", "coins": ["BTC"]}
        return {**base, **kw}

    def test_few_votes_change_nothing(self):
        s = self.story(votes={"positive": 2, "negative": 1})
        news_ai.apply_votes(s)
        self.assertEqual((s["direction"], s["severity"]), ("neutral", 1))

    def test_votes_set_direction_and_important_raises_to_notable_only(self):
        s = self.story(votes={"negative": 9, "positive": 1, "important": 12})
        news_ai.apply_votes(s)
        self.assertEqual((s["direction"], s["severity"]), ("bearish", 2))           # never 3 from votes

    def test_ai_is_overruled_only_by_a_strong_crowd(self):
        s = self.story(by="ai", direction="bullish", severity=2, votes={"negative": 6, "positive": 2})
        news_ai.apply_votes(s)
        self.assertEqual(s["direction"], "bullish")
        s = self.story(by="ai", direction="bullish", severity=2, votes={"negative": 18, "positive": 2})
        news_ai.apply_votes(s)
        self.assertEqual(s["direction"], "bearish")

    def test_major_content_keeps_its_direction(self):
        s = self.story(direction="bearish", severity=3, votes={"positive": 30})
        news_ai.apply_votes(s)
        self.assertEqual((s["direction"], s["severity"]), ("bearish", 3))

    def test_updates_start_from_the_original_reading(self):
        s = self.story(votes={"negative": 9, "positive": 1})
        news_ai.apply_votes(s)
        s["votes"] = {"negative": 9, "positive": 30}
        news_ai.apply_votes(s)
        self.assertEqual(s["direction"], "bullish")


POST = {"id": 123, "title": "Spot bitcoin ETFs see record $1.2 billion outflows", "published_at": "2026-11-19T10:00:00Z",
        "url": "https://cryptopanic.com/news/123/spot-bitcoin", "source": {"title": "CoinDesk", "domain": "coindesk.com"},
        "currencies": [{"code": "BTC", "title": "Bitcoin"}], "votes": {"negative": 2, "positive": 0, "important": 1}}


class CryptoPanicTests(unittest.TestCase):
    def panic(self):
        with mock.patch("signalbot.news.env", return_value="tok"):
            return news.CryptoPanic({"news": {"cryptopanic": True}})

    def test_parses_posts_and_remembers_the_working_endpoint(self):
        p = self.panic()
        calls = []

        def fake_get(url, *a, **k):
            calls.append(url)
            if "/developer/v2/" in url:
                raise urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO(b""))
            return json.dumps({"results": [POST]}).encode()
        with mock.patch.object(news, "_get", side_effect=fake_get):
            items = p.fetch(["BTC"])
        self.assertEqual(p.urls, ["https://cryptopanic.com/api/v1/posts/"])     # fell back, then remembered
        self.assertIn("currencies=BTC", calls[-1])
        it = items[0]
        self.assertEqual((it["cp_coins"], it["source"], it["cp_url"]), (["BTC"], "CoinDesk via CryptoPanic", POST["url"]))
        self.assertGreater(it["t"], 0)

    def test_blocked_or_web_page_pauses_with_a_reason(self):
        p = self.panic()
        with mock.patch.object(news, "_get", return_value=b"<html>Attention Required! | Cloudflare</html>"):
            self.assertEqual(p.fetch(["BTC"]), [])
        self.assertFalse(p.active)
        self.assertIn("Cloudflare", p.label)
        p = self.panic()
        err = urllib.error.HTTPError("u", 403, "forbidden", {}, io.BytesIO(b""))
        with mock.patch.object(news, "_get", side_effect=err):
            p.fetch(["BTC"])
        self.assertIn("403", p.label)

    def test_no_token_means_off(self):
        with mock.patch("signalbot.news.env", return_value=""):
            p = news.CryptoPanic({"news": {"cryptopanic": True}})
        self.assertEqual(p.fetch(["BTC"]), [])
        self.assertIn("CRYPTOPANIC_TOKEN", p.label)

    def test_votes_arriving_later_upgrade_a_story_and_alert_once(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = {"news": {"ai": False}}
            h = news.Headlines(cfg, Path(t) / "seen.json", panic=mock.Mock(**{"fetch.return_value": []}))
            h.first = False
            now = time.time() * 1000
            item = {"title": "Bitcoin treasury firm moves coins", "link": "L", "t": now, "source": "X via CryptoPanic",
                    "summary": "", "cp_id": "9", "cp_url": "https://cryptopanic.com/news/9/x", "cp_coins": ["BTC"],
                    "votes": {}}
            with mock.patch.object(h, "_fetch_all", return_value=[item]):
                self.assertEqual(h.poll(["BTC"]), [])                       # minor, no votes yet
            h.next_poll = 0
            voted = dict(item, votes={"negative": 8, "positive": 1, "important": 6})
            with mock.patch.object(h, "_fetch_all", return_value=[voted]):
                out = h.poll(["BTC"])
            self.assertEqual(len(out), 1)
            self.assertEqual((out[0]["direction"], out[0]["severity"]), ("bearish", 2))
            self.assertIn(out[0], h.last_new)                                # open trades get to react
            msg = news.headline_message(out[0])
            self.assertIn("CryptoPanic voters: 🟢 1 · 🔴 8", msg)
            self.assertIn("https://cryptopanic.com/news/9/x", msg)            # link back to CryptoPanic
            h.next_poll = 0
            with mock.patch.object(h, "_fetch_all", return_value=[dict(voted, votes={"negative": 9, "important": 7})]):
                self.assertEqual(h.poll(["BTC"]), [])                       # alerted once only

    def test_cryptopanic_copy_of_an_rss_story_brings_its_votes(self):
        with tempfile.TemporaryDirectory() as t:
            h = news.Headlines({"news": {"ai": False}}, Path(t) / "seen.json", panic=mock.Mock(**{"fetch.return_value": []}))
            h.first = False
            now = time.time() * 1000
            rss = {"title": "Spot bitcoin ETFs see record $1.2 billion outflows", "link": "rss", "t": now,
                   "source": "CoinDesk", "summary": ""}
            cp = dict(rss, link="orig", source="CoinDesk via CryptoPanic", cp_id="123", cp_url="https://cryptopanic.com/news/123/x",
                      cp_coins=["BTC"], votes={"negative": 7, "important": 2})
            with mock.patch.object(h, "_fetch_all", return_value=[rss, cp]):
                h.poll(["BTC"])
            self.assertEqual(len(h.recent), 1)
            story = h.recent[0]
            self.assertEqual((story["sources"], story["cp_id"]), (2, "123"))
            self.assertEqual(story["community"]["neg"], 7)


if __name__ == "__main__":
    unittest.main()


class CryptoPanicOffByDefaultTest(unittest.TestCase):
    def test_paid_service_is_off_and_silent_unless_switched_on(self):
        with mock.patch("signalbot.news.env", return_value="tok"):
            p = news.CryptoPanic({"news": {}})
        self.assertFalse(p.enabled)
        self.assertEqual(p.fetch(["BTC"]), [])
        with tempfile.TemporaryDirectory() as t:
            n = news.News({"news": {"calendar": False, "shock_guard": False, "ai": False}}, Path(t))
            self.assertNotIn("CryptoPanic", n.status_line())
            self.assertNotIn("CryptoPanic", n.summary(time.time() * 1000))
