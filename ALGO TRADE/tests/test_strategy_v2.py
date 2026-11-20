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

    def test_alt_must_lag_btc(self):
        s = self.ev(rules())
        S, i = self.S, self.i
        coin_ret = S.c[i] / S.c[i - 24] - 1
        c = rules(alt_must_lag_btc=True, lag_hours=24)
        j = strategy.trend_index(self.T, S.candles[i].tc)
        ev = lambda sym, btc_ret: strategy.evaluate(S, i, self.T, j, c, sym, "1h", None, btc_ret)
        self.assertIsNone(ev("ETHUSDT", coin_ret - 0.01 * s.d))       # coin already ahead of Bitcoin
        self.assertIsNotNone(ev("ETHUSDT", coin_ret + 0.01 * s.d))    # coin lagging: the catch-up is ahead
        self.assertIsNotNone(ev("BTCUSDT", coin_ret - 0.01 * s.d))    # Bitcoin itself isn't filtered
        self.assertIsNotNone(ev("ETHUSDT", None))                     # no Bitcoin data: not blocked
        self.assertLess(ev("ETHUSDT", coin_ret + 0.01 * s.d).features["rs_vs_btc"], 0)   # recorded

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
        self.bid, self.ask = self.price, self.price + 0.01
        self.moves = []                 # (bid, ask) the market jumps to right after each order

    def best(self, sym):
        return self.bid, self.ask

    def entry_limit(self, sym, side, qty, price, sl, post_only=False):
        link = f"L{len(self.orders_open) + 1}"
        if self.moves:                  # price moves between reading the book and the order landing
            self.bid, self.ask = self.moves.pop(0)
        crosses = float(price) >= self.ask if side == "Buy" else float(price) <= self.bid
        self.orders_open[link] = {"sym": sym, "side": side, "qty": float(qty), "price": float(price), "filled": 0.0,
                                  "status": "Cancelled" if post_only and crosses else "New", "sl": sl,
                                  "post_only": post_only}
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
    def setUp(self):
        p = mock.patch("signalbot.trader.POST_ONLY_CHECK_S", 0)
        p.start()
        self.addCleanup(p.stop)

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


class ExtraPairsTest(unittest.TestCase):
    """HYPE/ZEC/BNB-style coins: their own slot, half risk, never crowding the main pairs."""

    def make(self):
        c = trader_cfg()
        c["extra_pairs"] = ["ZECUSDT", "HYPEUSDT"]
        c["trading"].update(max_open_trades=2, max_same_direction=2, extra_max_open=1, extra_risk_mult=0.5)
        t = Trader(c)
        t.ex = FakeEx()
        return t

    def busy(self, *syms):
        out = []
        for s in syms:
            x = trader_sig(s)
            x.traded = True
            out.append(x)
        return out

    def test_half_risk(self):
        t, s = self.make(), trader_sig("ZECUSDT")
        t.open(s, [], [])
        self.assertTrue(s.traded)
        self.assertAlmostEqual(s.qty, 10.0)              # $15 risk (half of $30) / $1.5 stop

    def test_main_lane_full_does_not_block_extra(self):
        t, s = self.make(), trader_sig("ZECUSDT")
        t.open(s, self.busy("BTCUSDT", "ETHUSDT"), [])
        self.assertTrue(s.traded)

    def test_extra_does_not_take_a_main_slot(self):
        t, s = self.make(), trader_sig("ETHUSDT")
        t.open(s, self.busy("BTCUSDT", "ZECUSDT"), [])
        self.assertTrue(s.traded)

    def test_one_extra_at_a_time(self):
        t, s = self.make(), trader_sig("HYPEUSDT")
        self.assertIn("extra-pairs slot", t.open(s, self.busy("ZECUSDT"), []))
        self.assertFalse(s.traded)

    def test_a_main_pair_listed_as_extra_stays_main(self):
        from signalbot.notify import is_extra, pair_cfg
        c = self.make().cfg
        c["extra_pairs"].append("BTCUSDT")
        self.assertFalse(is_extra("BTCUSDT", c))
        self.assertEqual(pair_cfg(c, "BTCUSDT")["risk"]["risk_pct"], 1.0)
        self.assertEqual(pair_cfg(c, "ZECUSDT")["risk"]["risk_pct"], 0.5)

    def test_config_file_extras(self):
        with open(Path(__file__).parent.parent / "signals.toml", "rb") as f:
            c = tomllib.load(f)
        self.assertEqual(set(c["extra_pairs"]), {"BNBUSDT", "HYPEUSDT", "ZECUSDT"})
        self.assertFalse(set(c["extra_pairs"]) & set(c["pairs"]))
        self.assertEqual(c["trading"]["extra_max_open"], 1)


class PostOnlyEntryTests(LimitTradingTests):
    """Entries always pay the maker fee and never a worse price than the signal."""

    def test_rests_at_the_signal_price_as_post_only(self):
        t, s = self.make(), trader_sig()
        t.open(s, [], [])
        o = t.ex.orders_open["L1"]
        self.assertTrue(o["post_only"])
        self.assertEqual(o["price"], 100.0)

    def test_uses_the_better_market_price(self):
        t, s = self.make(), trader_sig()
        t.ex.bid, t.ex.ask = 99.5, 99.51          # price already dipped below the long's signal price
        note = t.open(s, [], [])
        self.assertEqual(s.pending["price"], 99.5)
        self.assertIn("better than the signal price", note)

    def test_never_pays_more_than_the_signal(self):
        t, s = self.make(), trader_sig()
        t.ex.bid, t.ex.ask = 100.4, 100.41        # price above the long's signal: wait for it to come back
        t.open(s, [], [])
        self.assertEqual(s.pending["price"], 100.0)

    def test_reprices_when_bybit_cancels_it(self):
        t, s = self.make(), trader_sig()
        t.ex.bid, t.ex.ask = 99.5, 99.51
        t.ex.moves = [(99.0, 99.01)]               # price drops as the 1st order lands: it would cross
        t.open(s, [], [])
        self.assertEqual(t.ex.orders_open["L1"]["status"], "Cancelled")
        self.assertEqual(s.pending["link"], "L2")
        self.assertEqual(s.pending["price"], 99.0)

    def test_gives_up_after_three_tries(self):
        t, s = self.make(), trader_sig()
        t.ex.bid, t.ex.ask = 99.5, 99.51
        t.ex.moves = [(99.0, 99.01), (98.95, 98.96), (98.9, 98.91)]
        self.assertIn("couldn't rest on the book", t.open(s, [], []))
        self.assertFalse(s.pending)

    def test_waiting_order_cancelled_by_bybit_is_replaced(self):
        t, s = self.make(), trader_sig()
        t.open(s, [], [])
        t.ex.orders_open["L1"]["status"] = "Cancelled"
        self.assertEqual(t.check_pending([s]), [])
        self.assertEqual(s.pending["link"], "L2")
        self.assertEqual(s.pending["tries"], 2)

    def test_no_entry_at_or_beyond_the_stop(self):
        t, s = self.make(), trader_sig()            # stop 98.5
        t.open(s, [], [])
        t.ex.orders_open["L1"]["status"] = "Cancelled"
        t.ex.bid, t.ex.ask = 98.4, 98.41
        t.check_pending([s])
        self.assertEqual(len(t.ex.orders_open), 1)   # not re-placed below the stop

    def test_plain_limit_when_post_only_is_off(self):
        t, s = self.make(), trader_sig()
        t.cfg["trading"]["post_only"] = False
        t.ex.bid, t.ex.ask = 99.5, 99.51
        t.open(s, [], [])
        o = t.ex.orders_open["L1"]
        self.assertFalse(o["post_only"])
        self.assertEqual(o["price"], 100.0)
