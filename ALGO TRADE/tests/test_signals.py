"""Offline tests for the signal scanner: python -m unittest discover -s tests"""
import math
import unittest

from signalbot import indicators as ind
from signalbot import tracker
from signalbot.data import Candle
from signalbot.strategy import Signal, shift_entry

H = 3_600_000


def sig(side="LONG", entry=100.0, sl=98.0, tps=(102.0, 104.0, 106.0)):
    return Signal("TESTUSDT", side, "1h", time=H, entry=entry, sl=sl, tps=list(tps), score=70,
                  setups=["pullback"], reasons=[], atr=1.0, sl_now=sl, last_t=0)


def k(i, o, h, l, c):
    return Candle(i * H, (i + 1) * H, o, h, l, c, 1.0)


class TrackerTests(unittest.TestCase):
    def test_stop_loss_is_minus_one_r(self):
        s = sig()
        tracker.update(s, k(1, 100, 100.5, 97.5, 98), 0.0, 48, H)
        self.assertEqual(s.status, "closed")
        self.assertAlmostEqual(s.realized_r, -1.0)

    def test_all_targets(self):
        s = sig()
        tracker.update(s, k(1, 100, 107, 99.5, 106.5), 0.0, 48, H)
        self.assertEqual(s.status, "closed")
        self.assertAlmostEqual(s.realized_r, (1 + 2 + 3) / 3)

    def test_tp1_then_breakeven(self):
        s = sig()
        tracker.update(s, k(1, 100, 102.5, 99.5, 102), 0.0, 48, H)
        self.assertEqual((s.tp_hit, s.sl_now), (1, 100.0))
        tracker.update(s, k(2, 102, 102.2, 99.9, 100), 0.0, 48, H)
        self.assertAlmostEqual(s.realized_r, 1 / 3)       # 1/3 at +1R, rest at breakeven

    def test_tp_touch_is_not_a_fill(self):
        s = sig()
        tracker.update(s, k(1, 100, 102.0, 99.5, 101), 0.0, 48, H)   # high == TP1 exactly
        self.assertEqual(s.tp_hit, 0)
        tracker.update(s, k(2, 101, 102.01, 100.5, 101), 0.0, 48, H)  # traded through
        self.assertEqual(s.tp_hit, 1)

    def test_close_back_through_moved_stop(self):
        s = sig()
        ev = tracker.update(s, k(1, 100, 102.5, 99.0, 99.8), 0.0, 48, H)  # TP1, then closed below entry
        self.assertEqual([e["kind"] for e in ev], ["tp", "stop"])
        self.assertEqual(s.exit_reason, "stop after TP1")
        self.assertAlmostEqual(s.realized_r, 1 / 3)                 # 1/3 at +1R, rest at breakeven

    def test_same_candle_stop_first(self):
        s = sig()
        tracker.update(s, k(1, 100, 106.5, 97.0, 100), 0.0, 48, H)
        self.assertAlmostEqual(s.realized_r, -1.0)          # conservative

    def test_short_side(self):
        s = sig("SHORT", 100, 102, (98, 96, 94))
        tracker.update(s, k(1, 100, 100.5, 97.5, 98), 0.0, 48, H)
        self.assertEqual(s.tp_hit, 1)
        self.assertEqual(s.sl_now, 100)

    def test_expiry_and_fees(self):
        s = sig()
        tracker.update(s, k(3, 100, 100.5, 99.5, 101), 0.1, 2, H)
        self.assertEqual(s.exit_reason, "expired")
        fee_r = 2 * 0.1 / 100 * 100 / 2
        self.assertAlmostEqual(s.realized_r, 0.5 - fee_r)

    def test_no_double_processing(self):
        s = sig()
        c = k(1, 100, 102.5, 99.5, 102)
        tracker.update(s, c, 0.0, 48, H)
        self.assertEqual(tracker.update(s, c, 0.0, 48, H), [])
        self.assertEqual(s.tp_hit, 1)

    def test_shift_entry_keeps_r(self):
        s = sig()
        shift_entry(s, 101.0)
        self.assertAlmostEqual(s.sl, 99.0)
        self.assertEqual([round(t, 9) for t in s.tps], [103.0, 105.0, 107.0])


class IndicatorTests(unittest.TestCase):
    def test_ema_constant(self):
        e = ind.ema([5.0] * 30, 10)
        self.assertTrue(math.isnan(e[8]))
        self.assertAlmostEqual(e[-1], 5.0)

    def test_rsi_extremes(self):
        up = [float(i) for i in range(40)]
        self.assertAlmostEqual(ind.rsi(up)[-1], 100.0)
        down = [float(40 - i) for i in range(40)]
        self.assertAlmostEqual(ind.rsi(down)[-1], 0.0)

    def test_atr_constant_range(self):
        h = [11.0] * 30
        l = [9.0] * 30
        c = [10.0] * 30
        self.assertAlmostEqual(ind.atr(h, l, c)[-1], 2.0)

    def test_adx_trending_high(self):
        c = [100 + i for i in range(80)]
        a = ind.adx([x + 0.5 for x in c], [x - 0.5 for x in c], c)
        self.assertGreater(a[-1], 50)


if __name__ == "__main__":
    unittest.main()
