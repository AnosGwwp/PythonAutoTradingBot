"""Strategy filters, limit entries and exchange-accurate fees (offline)."""
import math
import random
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from signalbot import backtest, scanner, strategy, tracker
from signalbot.data import Candle
from signalbot.scanner import State
from signalbot.strategy import Signal
from signalbot.trader import Trader
from test_trader import FakeEx, cfg as trader_cfg, sig as trader_sig

H = 3_600_000
CFG = tomllib.loads((Path(__file__).resolve().parent.parent / "signals.toml").read_text(encoding="utf-8"))
FILTERS = ("skip_wide_stops", "min_trend_spread_pct", "btc_confirm", "max_volume_ratio")


def synthetic():
    """A deterministic trending market that produces pullback signals."""
    rnd, p, cs = random.Random(1), 100.0, []
    for i in range(1200):
        o = p
        p = p * (1 + 0.0015 * math.sin(i / 60) + 0.0008 + rnd.gauss(0, 0.006))
        h, l = max(o, p) * (1 + abs(rnd.gauss(0, 0.002))), min(o, p) * (1 - abs(rnd.gauss(0, 0.002)))
        cs.append(Candle(i * H, (i + 1) * H, o, h, l, p, 100 + rnd.random() * 100))
    agg = [Candle(cs[i].t, cs[i + 3].tc, cs[i].o, max(c.h for c in cs[i:i + 4]), min(c.l for c in cs[i:i + 4]),
                  cs[i + 3].c, 1) for i in range(0, len(cs) - 3, 4)]
    return strategy.prepare(cs), strategy.prepare_trend(agg)


def rules(**kw):
    c = {**CFG, "rules": {**CFG["rules"], **kw}}
    for k in FILTERS:
        if k not in kw:
            c["rules"].pop(k, None)
    return c


class FilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.S, cls.T = synthetic()
        base = rules()
        cls.i = next(i for i in range(300, len(cls.S.candles))
                     if strategy.evaluate(cls.S, i, cls.T, strategy.trend_index(cls.T, cls.S.candles[i].tc),
                                          base, "ETHUSDT", "1h"))

    def ev(self, cfg, symbol="ETHUSDT", btc=None):
        j = strategy.trend_index(self.T, self.S.candles[self.i].tc)
        return strategy.evaluate(self.S, self.i, self.T, j, cfg, symbol, "1h", btc)

    def test_btc_confirm(self):
        s = self.ev(rules())
        c = rules(btc_confirm=True)
        self.assertIsNone(self.ev(c, btc=-s.d))                     # altcoin against Bitcoin's trend
        self.assertIsNone(self.ev(c, btc=0))                        # Bitcoin has no clear trend
        self.assertIsNotNone(self.ev(c, btc=s.d))
        self.assertIsNotNone(self.ev(c, symbol="BTCUSDT", btc=-s.d))   # BTC itself isn't filtered
        self.assertIsNotNone(self.ev(c, btc=None))                  # no BTC data: not blocked

    def test_other_filters(self):
        self.assertIsNone(self.ev(rules(max_volume_ratio=0.01)))
        self.assertIsNone(self.ev(rules(min_trend_spread_pct=99)))
        self.assertIsNone(self.ev(rules(skip_wide_stops=True, sl_atr_max=0.01, sl_atr_min=0.005)))
        self.assertIsNotNone(self.ev(rules(max_volume_ratio=99, min_trend_spread_pct=0, skip_wide_stops=True,
                                           sl_atr_max=99)))       # lenient settings let it through

    def test_new_features_recorded(self):
        f = self.ev(rules()).features
        self.assertIn("adx_slope", f)
        self.assertTrue(0 <= f["close_pos"] <= 1)


class FeeTests(unittest.TestCase):
    def sig(self):
        return Signal("X", "LONG", "1h", H, 100.0, 98.0, [102.0, 104.0, 106.0], 70, [], [], 1.0, sl_now=98.0)

    def test_flat_fee_unchanged(self):
        s = self.sig()
        tracker.close_now(s, 100.0, 0, 0.05)
        self.assertAlmostEqual(s.realized_r, -2 * 0.05 / 100 * 100 / 2)

    def test_takeprofits_pay_maker_and_the_rest_taker(self):
        s = self.sig()
        fees = {"entry": 0.02, "maker": 0.02, "taker": 0.055}
        tracker.update(s, Candle(H, 2 * H, 100, 102.5, 99.5, 102, 1), fees, 48, H)        # TP1
        tracker.update(s, Candle(2 * H, 3 * H, 102, 102, 99.0, 99.5, 1), fees, 48, H)     # stop at entry
        cost_r = (0.02 * 100 + 0.02 * (1 / 3) * 102 + 0.055 * (2 / 3) * 100) / 100 / 2
        self.assertAlmostEqual(s.realized_r, 1 / 3 - cost_r)

    def test_fees_of(self):
        c = {"risk": {"fee_pct": 0.055, "maker_fee_pct": 0.02}, "trading": {"limit_entry": True}}
        self.assertEqual(tracker.fees_of(c), {"taker": 0.055, "maker": 0.02, "entry": 0.02})
        c["trading"]["limit_entry"] = False
        self.assertEqual(tracker.fees_of(c)["entry"], 0.055)
        self.assertEqual(tracker.fees_of({"risk": {"fee_pct": 0.05}}), 0.05)


class LimitBacktestTests(unittest.TestCase):
    def test_limit_entries_fill_at_the_signal_price_and_skip_runaways(self):
        S, T = synthetic()
        c = rules()
        c["trading"] = {**CFG["trading"], "limit_entry": True}
        limit = backtest.simulate("ETHUSDT", S, T, c)
        c["trading"]["limit_entry"] = False
        market = backtest.simulate("ETHUSDT", S, T, c)
        self.assertTrue(limit and market)
        closes = {k.tc: k.c for k in S.candles}
        for t in limit:                       # filled exactly at the signal candle's close
            self.assertAlmostEqual(t.entry, closes[t.time])


class LimitEx(FakeEx):
    """Fake Bybit with resting limit orders."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.orders_open = {}

    def entry_limit(self, sym, side, qty, price, sl):
        link = f"L{len(self.orders_open) + 1}"
        self.orders_open[link] = {"sym": sym, "side": side, "qty": float(qty), "price": float(price), "filled": 0.0,
                                  "status": "New", "sl": sl}
        return link

    def fill(self, link, qty=None):
        o = self.orders_open[link]
        o["filled"] = o["qty"] if qty is None else qty
        o["status"] = "Filled" if qty is None else "PartiallyFilled"
        self.pos[o["sym"]] = {"side": o["side"], "size": o["filled"], "avg": o["price"]}

    def order_status(self, sym, link):
        o = self.orders_open.get(link)
        return o and {"status": o["status"], "filled": o["filled"], "avg": o["price"]}

    def cancel_order(self, sym, link):
        self.orders_open[link]["status"] = "Cancelled"


class LimitTradingTests(unittest.TestCase):
    def make(self):
        c = trader_cfg()
        c["trading"]["limit_entry"] = True
        t = Trader(c)
        t.ex = LimitEx()
        return t

    def test_order_placed_then_filled_then_managed(self):
        t, s = self.make(), trader_sig()
        note = t.open(s, [], [])
        self.assertIn("Limit order", note)
        self.assertTrue(s.pending and not s.traded)
        self.assertEqual(t.ex.orders_open["L1"]["price"], 100.0)            # at the signal price
        self.assertEqual(t.check_pending([s]), [])                          # waiting
        t.ex.fill("L1")
        out = t.check_pending([s])
        self.assertIn("entry filled", out[0][1])
        self.assertTrue(s.traded and not s.pending)
        self.assertEqual(len([o for o in t.ex.orders if o[0] == "limit"]), 3)   # take-profits placed

    def test_unfilled_order_expires_and_is_cancelled(self):
        t, s = self.make(), trader_sig()
        t.open(s, [], [])
        s.pending["until"] = 0
        out = t.check_pending([s])
        self.assertIn("fill in time", out[0][1])
        self.assertEqual(t.ex.orders_open["L1"]["status"], "Cancelled")
        self.assertFalse(s.pending or s.traded)

    def test_partial_fill_at_expiry_is_managed(self):
        t, s = self.make(), trader_sig()
        t.open(s, [], [])
        t.ex.fill("L1", qty=5.0)
        s.pending["until"] = 0
        out = t.check_pending([s])
        self.assertIn("partly filled", out[0][1])
        self.assertTrue(s.traded)
        self.assertEqual(s.qty, 5.0)

    def test_no_chasing_after_tp1(self):
        t, s = self.make(), trader_sig()
        t.open(s, [], [])
        s.tp_hit = 1
        out = t.check_pending([s])
        self.assertIn("cancelled", out[0][1])
        self.assertEqual(t.ex.orders_open["L1"]["status"], "Cancelled")

    def test_pending_counts_towards_limits(self):
        t = self.make()
        waiting = [trader_sig("ETHUSDT"), trader_sig("SOLUSDT")]
        for w in waiting:
            w.pending = {"link": "x"}
        self.assertIn("double the same bet", t.open(trader_sig(), waiting, []))

    def test_closing_the_signal_cancels_the_waiting_order(self):
        t, s = self.make(), trader_sig()
        t.open(s, [], [])
        with tempfile.TemporaryDirectory() as d:
            cfg = {**trader_cfg(), "alerts": {"log_dir": d}, "entry_tf": "1h", "rules": {"cooldown_bars": 6}}
            st = State(Path(d) / "s.json")
            st.open[s.symbol] = s
            s.status = "closed"
            scanner.close_trade(cfg, st, s, int(time.time() * 1000), t, mock.Mock())
        self.assertEqual(t.ex.orders_open["L1"]["status"], "Cancelled")
        self.assertFalse(s.pending)


if __name__ == "__main__":
    unittest.main()
