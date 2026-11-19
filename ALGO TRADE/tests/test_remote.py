import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from signalbot import remote
from signalbot.notify import signal_message, sizing
from signalbot.scanner import State
from signalbot.strategy import Signal


def sig(entry=100.0, sl=98.5, side="LONG", symbol="BTCUSDT"):
    d = 1 if side == "LONG" else -1
    risk = abs(entry - sl)
    return Signal(symbol, side, "1h", 0, entry, sl, [entry + d * risk * k for k in (1, 2, 3)],
                  70, ["pullback"], ["why"], 1.0, sl_now=sl)


def cfg(tmp, **risk):
    r = {"account_size": 3000, "risk_pct": 1.0, "leverage": 10, "max_margin_pct": 30,
         "maint_margin_pct": 0.5, "fee_pct": 0.05}
    r.update(risk)
    return {"pairs": ["BTCUSDT"], "watch_pairs": [], "watch_min_score": 75, "entry_tf": "1h",
            "trend_tf": "4h", "data_source": "binance", "rules": {"min_score": 60, "cooldown_bars": 6},
            "risk": r, "alerts": {"log_dir": tmp, "telegram": False}}


class FakeNotifier:
    tg, tg_chat, tg_token = False, "1", ""

    def __init__(self):
        self.sent = []

    def reply(self, text, keyboard=None):
        self.sent.append(text)

    def _csv(self, *a):
        pass


class SizingTest(unittest.TestCase):
    def test_risk_dollars_independent_of_leverage(self):
        with tempfile.TemporaryDirectory() as t:
            a = sizing(sig(), cfg(t, leverage=5))
            b = sizing(sig(), cfg(t, leverage=20))
        self.assertAlmostEqual(a["risk_amt"], 30.0)          # 1% of 3000
        self.assertAlmostEqual(b["risk_amt"], 30.0)
        self.assertAlmostEqual(a["notional"], 2000.0)        # 30 / 1.5% stop
        self.assertAlmostEqual(a["margin"], 400.0)
        self.assertAlmostEqual(b["margin"], 100.0)

    def test_leverage_lowered_when_stop_near_liquidation(self):
        with tempfile.TemporaryDirectory() as t:
            s = sizing(sig(sl=94.0), cfg(t, leverage=50))    # 6% stop
        self.assertLess(s["lev"], 50)
        self.assertLess(s["liq"], 94.0)                      # long: liquidation below the stop
        self.assertGreater(94.0 - s["liq"], 0)

    def test_short_liquidation_above_stop(self):
        with tempfile.TemporaryDirectory() as t:
            s = sizing(sig(sl=102.0, side="SHORT"), cfg(t, leverage=20))
        self.assertGreater(s["liq"], 102.0)

    def test_margin_cap_reduces_risk(self):
        with tempfile.TemporaryDirectory() as t:
            s = sizing(sig(), cfg(t, leverage=1))            # $2000 notional > 30% of 3000 at 1x
        self.assertTrue(s["capped"])
        self.assertAlmostEqual(s["margin"], 900.0)
        self.assertLess(s["risk_amt"], 30.0)

    def test_message_shows_leverage(self):
        with tempfile.TemporaryDirectory() as t:
            m = signal_message(sig(), cfg(t))
        self.assertIn("10x isolated", m)
        self.assertIn("Liquidation", m)


class RemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg(self.tmp.name)
        self.st = State(Path(self.tmp.name) / "state.json")
        self.n = FakeNotifier()
        self.r = remote.Remote(self.cfg, self.st, self.n, threading.RLock(), threading.Event())

    def tearDown(self):
        self.tmp.cleanup()

    def saved(self):
        return json.loads((Path(self.tmp.name) / "settings.json").read_text())

    def test_buttons_and_commands(self):
        self.assertIn("Status", self.r.handle("📊 Status"))
        self.assertIn("Phone controls", self.r.handle("/help"))
        self.assertIn("don't know", self.r.handle("/nonsense"))
        self.assertIn("Status", self.r.handle("/status@MyBot"))

    def test_pause_resume_persist(self):
        self.r.handle("⏸ Pause")
        self.assertTrue(self.cfg["paused"])
        self.assertTrue(self.saved()["paused"])
        self.r.handle("/resume")
        self.assertFalse(self.cfg["paused"])

    def test_money_settings_validated_and_saved(self):
        self.assertIn("$45", self.r.handle("/risk 1.5"))
        self.assertEqual(self.cfg["risk"]["risk_pct"], 1.5)
        self.assertIn("between", self.r.handle("/risk 50"))
        self.assertEqual(self.cfg["risk"]["risk_pct"], 1.5)
        self.r.handle("/lev 15x")
        self.r.handle("/capital $3,000")
        self.assertEqual(self.saved(), {"risk_pct": 1.5, "leverage": 15, "account_size": 3000.0})
        fresh = cfg(self.tmp.name)
        remote.apply_overrides(fresh)
        self.assertEqual(fresh["risk"]["leverage"], 15)

    def test_tf_sets_trend(self):
        self.r.handle("/tf 4h")
        self.assertEqual((self.cfg["entry_tf"], self.cfg["trend_tf"]), ("4h", "1d"))

    def test_add_remove_pair(self):
        with mock.patch.object(remote, "price", return_value=1.0):
            self.r.handle("/watch sui/usdc")
            self.assertEqual(self.cfg["watch_pairs"], ["SUIUSDT"])
            self.r.handle("/add SUI")                         # moves it from watchlist to main
        self.assertEqual(self.cfg["pairs"], ["BTCUSDT", "SUIUSDT"])
        self.assertEqual(self.cfg["watch_pairs"], [])
        self.r.handle("/remove sui")
        self.assertEqual(self.cfg["pairs"], ["BTCUSDT"])

    def test_extra_pair_and_coin_names(self):
        self.assertEqual(remote.normalize_pair("zcash"), "ZECUSDT")
        self.assertEqual(remote.normalize_pair("Hyperliquid"), "HYPEUSDT")
        with mock.patch.object(remote, "price", return_value=1.0):
            self.assertIn("half risk", self.r.handle("/extra ZCASH"))
            self.r.handle("/watch hype")
            self.r.handle("/extra hype")                      # moves it from watchlist to extra
        self.assertEqual(self.cfg["extra_pairs"], ["ZECUSDT", "HYPEUSDT"])
        self.assertEqual(self.cfg["watch_pairs"], [])
        self.assertEqual(self.saved()["extra_pairs"], ["ZECUSDT", "HYPEUSDT"])
        self.assertIn("Extra", self.r.handle("/pairs"))
        self.r.handle("/remove zec")
        self.assertEqual(self.cfg["extra_pairs"], ["HYPEUSDT"])

    def test_close_trade(self):
        self.st.open["BTCUSDT"] = sig()
        with mock.patch.object(remote, "price", return_value=101.5):
            out = self.r.handle("/close btc")
        self.assertIn("closed", out)
        self.assertEqual(self.st.open, {})
        self.assertAlmostEqual(self.st.closed[0].realized_r, 1.0 - 2 * 0.0005 * 100 / 1.5, places=6)


if __name__ == "__main__":
    unittest.main()
