import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from signalbot import data, news, news_ai, scanner
from signalbot.data import Candle, CandleCache
from signalbot.scanner import State, scan_once
from signalbot.strategy import Signal

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Bitcoin plunges after Fed signals rate hike</title><link>https://x/1</link>
<pubDate>Sun, 15 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>SOL ecosystem grows</title><link>https://x/2</link></item>
<item><title>Solar panels are cheap</title><link>https://x/3</link></item>
</channel></rss>"""


def flat(n, price=100.0, rng=0.1, start=0):
    return [Candle(start + i * 60_000, start + (i + 1) * 60_000, price, price + rng / 2, price - rng / 2, price, 1.0)
            for i in range(n)]


class NewsTest(unittest.TestCase):
    def test_parse_rss(self):
        items = news.parse_rss(RSS, "Test")
        self.assertEqual(len(items), 3)
        self.assertGreater(items[0]["t"], 0)
        self.assertEqual(items[1]["t"], 0)

    def test_coin_patterns(self):
        p = news_ai.coin_patterns(["BTC", "SOL"])
        self.assertTrue(p["BTC"].search("Bitcoin plunges"))
        self.assertTrue(p["SOL"].search("SOL ecosystem grows"))
        self.assertTrue(p["SOL"].search("Solana outage"))
        self.assertFalse(p["SOL"].search("Solar panels are cheap"))
        self.assertFalse(p["SOL"].search("the sol of it"))       # lowercase ticker isn't a match

    def test_delisting_names_the_contract(self):
        p = news_ai.coin_patterns(["SOL", "ARB"])
        self.assertTrue(p["SOL"].search("Bybit Will Delist the SOLUSDT Perpetual Contract"))
        self.assertTrue(p["ARB"].search("Delisting of ARBUSDT, XYZUSDT Perpetual Contracts"))
        self.assertTrue(p["ARB"].search("Delisting of ARB/USDT Spot Trading Pair"))
        self.assertFalse(p["SOL"].search("Delisting of SOLVUSDT Perpetual Contract"))   # other coin


class NotifierTest(unittest.TestCase):
    def test_locked_csv_never_breaks_a_scan(self):
        from signalbot.notify import Notifier
        with tempfile.TemporaryDirectory() as t:
            n = Notifier({"alerts": {"log_dir": t, "telegram": False}, "risk": {"account_size": 3000,
                          "risk_pct": 1, "leverage": 10}})
            n.csv = Path(t) / "locked"
            n.csv.mkdir()                                          # opening a folder fails like a lock
            s = Signal("BTCUSDT", "LONG", "1h", 0, 100, 98.5, [101.5], 70, ["pullback"], ["x"], 1, sl_now=98.5)
            with mock.patch.object(time, "sleep"), self.assertLogs("signals", "WARNING"):
                n.signal(s)                                        # must not raise
                n.event(s, {"kind": "done"})

    def test_headline_importance_and_first_run_quiet(self):
        with tempfile.TemporaryDirectory() as t:
            h = news.Headlines({"news": {"ai": False}}, Path(t) / "seen.json")
            now_rss = RSS.replace(b"Sun, 15 Nov 2026 10:00:00 GMT",
                                  time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()).encode())
            with mock.patch.object(news, "_get", return_value=now_rss):
                self.assertEqual(h.poll(["BTC", "SOL"]), [])          # first run: just learn what exists
                self.assertEqual(len(h.recent), 2)                    # BTC+macro and SOL items kept
                h.next_poll, h.seen = 0, {}
                h.recent.clear()                                      # forget the stories: they are new again
                alerts = h.poll(["BTC", "SOL"])
            titles = [a["title"] for a in alerts]
            self.assertIn("Bitcoin plunges after Fed signals rate hike", titles)
            self.assertNotIn("SOL ecosystem grows", titles)            # no impact keyword

    def test_calendar_blackout(self):
        with tempfile.TemporaryDirectory() as t:
            c = news.Calendar({"news": {"pause_before_min": 15, "pause_after_min": 30}}, Path(t) / "c.json")
            c._parse([{"title": "CPI m/m", "country": "USD", "impact": "High", "date": "2026-11-13T08:30:00-05:00"},
                      {"title": "Minor", "country": "USD", "impact": "Low", "date": "2026-11-13T08:30:00-05:00"},
                      {"title": "ECB", "country": "EUR", "impact": "High", "date": "2026-11-13T08:30:00-05:00"}])
            self.assertEqual(len(c.events), 1)
            t0 = c.events[0]["t"]
            self.assertIsNone(c.blackout(t0 - 16 * 60_000))
            self.assertIsNotNone(c.blackout(t0 - 14 * 60_000))
            self.assertIsNotNone(c.blackout(t0 + 29 * 60_000))
            self.assertIsNone(c.blackout(t0 + 31 * 60_000))

    def test_shock_guard(self):
        g = news.ShockGuard({"news": {"shock_mult": 8, "shock_min_pct": 1.2, "shock_pause_min": 30}})
        cs = flat(70)
        self.assertIsNone(g.check("BTCUSDT", cs, 0))
        last = cs[-1]
        cs[-1] = Candle(last.t, last.tc, 100, 102, 99.9, 102, 1)      # +2% in 5 min, 20x normal range
        s = g.check("BTCUSDT", cs, 1000)
        self.assertIsNotNone(s)
        self.assertAlmostEqual(s["pct"], 2.0)
        self.assertIsNone(g.check("BTCUSDT", cs, 2000))                # no repeat alert while paused
        self.assertTrue(g.blocked("BTCUSDT", 2000))
        self.assertFalse(g.blocked("BTCUSDT", 1000 + 31 * 60_000))


class CacheTest(unittest.TestCase):
    def test_incremental_update(self):
        now = int(time.time() * 1000) // 60_000 * 60_000
        hist = flat(100, start=now - 100 * 60_000)                     # last candle closes at `now`
        calls = []

        def fake(symbol, tf, n, source="binance"):
            calls.append(n)
            return hist[-n:]
        c = CandleCache()
        with mock.patch.object(data, "candles", side_effect=fake):
            self.assertEqual(len(c.get("X", "1m", 90)), 90)
            self.assertEqual(c.get("X", "1m", 90)[-1], hist[-1])       # nothing new: no download
            self.assertEqual(calls, [90])
            newer = flat(2, price=101, start=now)
            hist += newer
            with mock.patch("time.time", return_value=(now + 2 * 60_000 + 1000) / 1000):
                got = c.get("X", "1m", 90)
            self.assertEqual(got[-1], newer[-1])
            self.assertEqual(len(got), 90)
            self.assertLess(calls[-1], 10)                             # only the new candles


class FakeNotifier:
    tg = False

    def __init__(self):
        self.signals, self.events = [], []

    def signal(self, sig, note=""):
        self.signals.append((sig, note))

    def event(self, sig, ev, note=""):
        self.events.append(ev)


def cfg(tmp):
    return {"pairs": ["BTCUSDT"], "watch_pairs": [], "entry_tf": "1h", "trend_tf": "4h", "data_source": "bybit",
            "rules": {"max_bars_open": 48, "cooldown_bars": 6, "min_score": 60},
            "risk": {"account_size": 3000, "risk_pct": 1, "leverage": 10, "fee_pct": 0.05},
            "alerts": {"log_dir": tmp}}


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = State(Path(self.tmp.name) / "s.json")
        self.sig = Signal("BTCUSDT", "LONG", "1h", 1000, 100, 98.5, [101.5, 103, 104.5], 70, ["pullback"], ["x"], 1,
                          sl_now=98.5)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scan(self, trader=None, news_obj=None, close=None):
        n = FakeNotifier()
        close = close or int(time.time() * 1000) - 60_000          # candle closed a minute ago
        self.close = close
        with mock.patch.object(scanner, "_evaluate_pair", return_value=("BTCUSDT", self.sig, close)):
            res = scan_once(cfg(self.tmp.name), self.st, n, trader=trader, news=news_obj)
        return res, n

    def test_stale_candle_after_downtime_is_skipped(self):
        res, n = self.run_scan(close=int(time.time() * 1000) - 40 * 60_000)
        self.assertEqual(res.new, 0)
        self.assertEqual(n.signals, [])

    def test_news_block_skips_trade_but_still_alerts(self):
        trader = mock.Mock(active=True)
        trader.sync.return_value = []
        trader.audit_stops.return_value = []
        trader.check_pending.return_value = []
        blocker = mock.Mock(**{"alignment.return_value": "none", "context.return_value": ""})
        blocker.block_reason.return_value = "high-impact news: USD CPI"
        res, n = self.run_scan(trader, blocker)
        blocker.block_reason.assert_called_with("BTCUSDT", mock.ANY, "LONG")     # direction-aware
        self.assertEqual(res.new, 1)
        trader.open.assert_not_called()
        self.assertIn("Not auto-traded", n.signals[0][1])
        self.assertIn("BTCUSDT", self.st.open)

    def test_trades_when_clear_and_state_saved(self):
        trader = mock.Mock(active=True)
        trader.open.return_value = "🤖 traded"
        trader.sync.return_value = []
        trader.audit_stops.return_value = []
        trader.check_pending.return_value = []
        clear = mock.Mock(**{"alignment.return_value": "against",
                             "context.return_value": "📰 News on BTC (12 h): bearish"})
        clear.block_reason.return_value = ""
        res, n = self.run_scan(trader, clear)
        trader.open.assert_called_once()
        self.assertEqual(n.signals[0][1], "🤖 traded\n📰 News on BTC (12 h): bearish")   # mood shown
        self.assertEqual(self.st.open["BTCUSDT"].features["news"], "against")          # journaled
        saved = json.loads((Path(self.tmp.name) / "s.json").read_text())
        self.assertIn("BTCUSDT", saved["open"])
        self.assertEqual(saved["last_eval"]["BTCUSDT"], self.close)

    def test_same_candle_not_signalled_twice(self):
        self.run_scan()
        self.st.open.clear()
        res, n = self.run_scan(close=self.close)              # same candle again
        self.assertEqual(res.new, 0)
        self.assertEqual(n.signals, [])

    def test_paused_skips_evaluation(self):
        c = cfg(self.tmp.name)
        c["paused"] = True
        with mock.patch.object(scanner, "_evaluate_pair") as ev:
            res = scan_once(c, self.st, FakeNotifier())
        ev.assert_not_called()
        self.assertEqual(res.new, 0)


class LockTest(unittest.TestCase):
    def test_single_instance(self):
        with tempfile.TemporaryDirectory() as t:
            a = scanner._single_instance(Path(t) / "x.lock")
            self.assertIsNotNone(a)
            self.assertIsNone(scanner._single_instance(Path(t) / "x.lock"))
            a.close()
            b = scanner._single_instance(Path(t) / "x.lock")
            self.assertIsNotNone(b)
            b.close()


if __name__ == "__main__":
    unittest.main()
