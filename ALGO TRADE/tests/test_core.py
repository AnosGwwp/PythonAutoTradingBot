"""Offline tests: python -m unittest discover -s tests"""
import asyncio
import math
import unittest
from decimal import Decimal
from pathlib import Path

from flintbot import config
from flintbot.feed import Book, Fill, Level
from flintbot.paper import PaperExecutor
from flintbot.risk import KILL, OK, PAUSE, RiskManager
from flintbot.strategy import Strategy, dec, microprice, needs_requote

CFG = Path(__file__).resolve().parent.parent / "config.toml"


def cfg():
    return config.load(CFG)


def book(bid=100.0, ask=100.2, bs=1.0, as_=1.0, ts=1000.0):
    return Book((Level(bid, bs),), (Level(ask, as_),), ts, 1)


class StrategyTests(unittest.TestCase):
    def test_microprice_leans_to_thin_side(self):
        # Large bid size, small ask size -> price pressure upward -> microprice near ask.
        m = microprice(book(bs=9.0, as_=1.0))
        self.assertGreater(m, 100.1)
        self.assertLess(m, 100.2)

    def test_microprice_strips_own_quotes(self):
        b = Book((Level(100.0, 1.0), Level(99.9, 1.0)), (Level(100.2, 1.0),), 0, 0)
        # Our 1.0 at 100.0 is the whole best bid -> next real bid is 99.9.
        self.assertAlmostEqual(microprice(b, {100.0: 1.0}, {}), (99.9 + 100.2) / 2)

    def test_own_quotes_stripped_when_tick_rounded(self):
        # our computed bid 99.99637... is shown on-chain rounded to 99.996, next to another maker
        b = Book((Level(99.996, 1.5), Level(99.9, 1.0)), (Level(100.2, 1.0),), 0, 0)
        m = microprice(b, {99.996372718: 1.0}, {})
        self.assertAlmostEqual(m, (99.996 * 1.0 + 100.2 * 0.5) / 1.5)   # only OUR 1.0 removed
        far = microprice(b, {99.95: 1.0}, {})                           # 5 bps away: not ours
        self.assertAlmostEqual(far, microprice(b))

    def test_inventory_skew_and_pull(self):
        c = cfg()
        s = Strategy(c, lot_size=Decimal("0.001"))
        s.build_ladder(100.0)
        flat = s.compute(100.0, 0.0, 0.0)
        long = s.compute(100.0, 0.0, c.max_inventory_base / 2)
        self.assertLess(long.reservation, flat.reservation)       # long -> cheaper quotes
        full = s.compute(100.0, 0.0, c.max_inventory_base)
        self.assertTrue(full.pull_bid)
        self.assertFalse(full.pull_ask)
        self.assertLess(full.bid_fair, 85.0)                       # bid pushed out of market

    def test_spread_bounds(self):
        c = cfg()
        s = Strategy(c)
        s.build_ladder(100.0)
        s.vol.var_rate = 1.0                                       # absurd volatility
        q = s.compute(100.0, 0.0, 0.0)
        self.assertAlmostEqual(q.half_spread_bps, c.max_half_spread_bps)
        bid0 = s.ladder.prices(q.bid_fair, "bid")[0]
        self.assertAlmostEqual((100.0 - bid0) / 100.0 * 1e4, c.max_half_spread_bps, places=6)

    def test_ladder_cumulative_and_lots(self):
        c = cfg()
        s = Strategy(c, lot_size=Decimal("0.001"))
        lad = s.build_ladder(100.0)
        p = lad.prices(100.0, "bid")
        self.assertTrue(all(a > b for a, b in zip(p, p[1:])))
        for sz in lad.sizes:
            self.assertEqual(sz % Decimal("0.001"), 0)

    def test_requote_threshold(self):
        c = cfg()
        s = Strategy(c)
        s.build_ladder(100.0)
        a = s.compute(100.0, 0.0, 0.0)
        b = s.compute(100.0 * (1 + c.requote_bps / 2e4), 0.0, 0.0)
        self.assertFalse(needs_requote(b, a, 0.5, c))
        self.assertTrue(needs_requote(b, a, c.heartbeat_sec, c))
        far = s.compute(100.0 * (1 + 3 * c.requote_bps / 1e4), 0.0, 0.0)
        self.assertTrue(needs_requote(far, a, 0.5, c))

    def test_vol_seed(self):
        s = Strategy(cfg())
        closes = [100 * math.exp(0.001 * (-1) ** i) for i in range(50)]
        s.vol.seed(closes, 60.0)
        self.assertGreater(s.vol.sigma(10.0), 0)

    def test_dec_no_float_artifacts(self):
        self.assertEqual(dec(0.1 + 0.2), "0.3")
        self.assertEqual(dec(71.84720000000001), "71.8472")


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.s = Strategy(self.c, lot_size=Decimal("0.001"))
        self.ex = PaperExecutor(self.c)
        lad = self.s.build_ladder(100.0)
        q = self.s.compute(100.0, 0.0, 0.0)
        asyncio.run(self.ex.install(lad, q, ts=1000.0, with_risk=True))
        self.bid0 = self.ex.bids[0][0]
        self.ask0 = self.ex.asks[0][0]

    def test_trade_through_fills_bid(self):
        ev = self.ex.on_fills([Fill("a", 1001.0, self.bid0 - 0.01, 1.0, "sell")])
        self.assertEqual(ev[0]["side"], "buy")
        self.assertGreater(self.ex.base, float(self.c.start_base))
        self.assertLess(self.ex.quote, float(self.c.start_quote))

    def test_no_fill_when_trade_outside_quotes(self):
        self.assertEqual(self.ex.on_fills([Fill("b", 1001.0, self.bid0 + 0.05, 1.0, "sell")]), [])
        self.assertEqual(self.ex.on_fills([Fill("c", 1001.0, self.ask0 - 0.05, 1.0, "buy")]), [])

    def test_no_lookahead_and_dedupe(self):
        self.assertEqual(self.ex.on_fills([Fill("d", 999.0, 1.0, 5.0, "sell")]), [])  # before quote
        f = Fill("e", 1001.0, self.bid0, 0.02, "sell")
        self.assertEqual(len(self.ex.on_fills([f])), 1)
        self.assertEqual(self.ex.on_fills([f]), [])                                   # duplicate

    def test_inventory_cap_respected(self):
        for i in range(50):
            self.ex.on_fills([Fill(f"x{i}", 1001.0 + i, 1.0, 10.0, "sell")])
        self.assertLessEqual(self.ex.inventory(), self.c.max_inventory_base + 1e-9)
        self.assertGreaterEqual(self.ex.quote, -1e-9)

    def test_round_trip_earns_spread(self):
        self.ex.on_fills([Fill("b1", 1001.0, self.bid0, 0.04, "sell")])
        self.ex.on_fills([Fill("s1", 1002.0, self.ask0, 0.04, "buy")])
        self.assertAlmostEqual(self.ex.inventory(), 0.0, places=9)
        self.assertGreater(self.ex.pnl(100.0), 0)


class FeedTests(unittest.TestCase):
    def test_decodes_json_and_event_stream(self):
        from flintbot.feed import _decode
        self.assertEqual(_decode(b'{"a": 1}'), {"a": 1})
        self.assertEqual(_decode(b'event: message\ndata: {"a": 2}\n\n'), {"a": 2})

    def test_malformed_reply_is_a_feed_error(self):
        from flintbot.feed import FeedError, McpFeed
        f = McpFeed("https://example.invalid/mcp")

        async def bad_call(tool, args):
            return {"l2": {"bids": [{"price": {}}]}}             # missing fields
        f.call = bad_call
        with self.assertRaises(FeedError):
            asyncio.run(f.book("SOL/main"))


class EngineTests(unittest.TestCase):
    def run_engine(self, fail_times: int, seconds: float):
        import tempfile
        import time as _time
        from unittest import mock

        from flintbot import engine
        from flintbot.feed import PairInfo
        c = cfg()
        c.tick_sec, c.max_consecutive_errors = 0.01, 3
        calls = []

        class Feed:
            def __init__(self, url):
                pass

            async def pair_info(self, label):
                return PairInfo(label, "SOL", "USD", 1, 2, "", 0, None, Decimal("0.001"))

            async def candle_closes(self, label):
                return []

            async def book(self, label):
                return book(ts=_time.time())

            async def fills_since(self, label, ts):
                return []

        class Ex(PaperExecutor):
            async def install(self, *a, **kw):
                calls.append(1)
                if len(calls) <= fail_times:
                    raise RuntimeError("landing timeout")
                await super().install(*a, **kw)

        with tempfile.TemporaryDirectory() as t:
            c.journal_dir = t
            with mock.patch.object(engine, "McpFeed", Feed), mock.patch.object(engine, "PaperExecutor", Ex), \
                    self.assertLogs("flintbot", "WARNING"):
                asyncio.run(engine.run(c, duration_sec=seconds))
            events = [l for p in Path(t).glob("*.jsonl") for l in p.read_text().splitlines()]
        return calls, events

    def test_transient_submission_error_is_retried(self):
        calls, events = self.run_engine(fail_times=1, seconds=0.3)
        self.assertEqual(len(calls), 2)                              # failed once, then installed
        self.assertFalse(any('"kill"' in e for e in events))

    def test_repeated_submission_errors_kill(self):
        calls, events = self.run_engine(fail_times=99, seconds=5)
        self.assertEqual(len(calls), 3)                              # max_consecutive_errors
        self.assertTrue(any('"kill"' in e for e in events))


class RiskTests(unittest.TestCase):
    def test_gates(self):
        c = cfg()
        r = RiskManager(c)
        self.assertEqual(r.check(book(ts=100.0), 100.1, 0.0, 100.5).action, OK)
        self.assertEqual(r.check(book(ts=100.0), 100.1, 0.0, 100.0 + c.max_book_age_sec + 1).action, PAUSE)
        self.assertEqual(r.check(book(ts=200.0), None, 0.0, 200.0).action, PAUSE)
        jump = 100.1 * (1 + 2 * c.jump_bps / 1e4)
        self.assertEqual(r.check(book(ts=300.0), jump, 0.0, 300.0).action, PAUSE)
        self.assertEqual(r.check(book(ts=301.0), jump, 0.0, 301.0).action, PAUSE)  # cooldown
        r.pause_until = 0
        self.assertEqual(r.check(book(ts=400.0), jump, -c.max_drawdown_quote, 400.0).action, KILL)

    def test_error_kill(self):
        c = cfg()
        r = RiskManager(c)
        acts = [r.record_error().action for _ in range(c.max_consecutive_errors)]
        self.assertEqual(acts[-1], KILL)
        self.assertTrue(all(a == PAUSE for a in acts[:-1]))


if __name__ == "__main__":
    unittest.main()
