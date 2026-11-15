"""Learning: journal, review statistics, suggestions, rules (offline)."""
import json
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from signalbot import learn, remote, scanner, tracker
from signalbot.data import Candle
from signalbot.scanner import State, scan_once
from signalbot.strategy import Signal

H = 3_600_000


def trade(i, r, symbol="BTCUSDT", setups=("pullback",), side="LONG", adx=30.0, **feat):
    f = {"adx": adx, "vol_ratio": 1.3, "htf_spread_pct": 0.5, "stretch_atr": 0.8, "stop_atr": 1.5,
         "hour_utc": 10, **feat}
    return {"source": "backtest", "symbol": symbol, "side": side, "time": i * H, "setups": list(setups),
            "score": 70, "features": f, "realized_r": r, "exit_reason": "stop loss" if r < 0 else "all targets",
            "tp_hit": 0 if r < 0 else 3, "mfe_r": 0.6 if r < 0 else 2.0, "mae_r": 1.0 if r < 0 else 0.3}


def history(bad_r=(-1.0, -1.0, 0.5), n=200, seed=1):
    """Mixed history in which SOL breakouts keep losing (pattern bad_r), everything else is ~+0.2R."""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        if i % 4 == 0:
            out.append(trade(i, bad_r[i // 4 % len(bad_r)], "SOLUSDT", ("breakout",)))
        else:
            out.append(trade(i, rnd.choice([-1.0, 0.33, 2.0]), rnd.choice(["BTCUSDT", "ETHUSDT"])))
    return out


def sig(symbol="SOLUSDT", setups=("breakout",)):
    return Signal(symbol, "LONG", "1h", 0, 100.0, 98.0, [102.0, 104.0, 106.0], 70, list(setups), ["x"], 1.0,
                  sl_now=98.0, features={"adx": 30.0, "vol_ratio": 1.3, "htf_spread_pct": 0.5,
                                         "stretch_atr": 0.8, "stop_atr": 1.5, "hour_utc": 10})


class SuggestionTests(unittest.TestCase):
    def test_finds_consistently_losing_condition(self):
        sugg = learn.find_suggestions(history(), [], [], 30)
        self.assertTrue(sugg)
        self.assertEqual(sugg[0]["cond"], {"setup": "breakout"})       # broadest losing condition first
        self.assertNotIn({"setup": "breakout", "coin": "SOL"}, [s["cond"] for s in sugg])  # covered by it
        self.assertLess(sugg[0]["avg"], 0)

    def test_too_few_trades_suggests_nothing(self):
        self.assertEqual(learn.find_suggestions(history(n=60), [], [], 30), [])

    def test_losing_only_in_one_half_is_noise(self):
        h = history()
        mid = sorted(t["time"] for t in h)[len(h) // 2]
        for t in h:                           # SOL breakouts win in the newer half
            if t["symbol"] == "SOLUSDT" and t["time"] >= mid:
                t["realized_r"] = 1.0
        self.assertEqual(learn.find_suggestions(h, [], [], 30), [])

    def test_live_trades_can_veto(self):
        live = [dict(trade(i, 1.5, "SOLUSDT", ("breakout",)), source="live") for i in range(6)]
        self.assertEqual(learn.find_suggestions(history(), live, [], 30), [])

    def test_rule_removes_trades_and_drop_suggested_when_recovered(self):
        rule = {"id": "setup=breakout", "cond": {"setup": "breakout"}, "label": "breakout setups"}
        self.assertNotIn("setup=breakout", [s["id"] for s in learn.find_suggestions(history(), [], [rule], 30)])
        self.assertEqual(learn.find_drops(history(), [rule], 30), [])
        recovered = history(bad_r=(1.0, -1.0, 1.0))
        self.assertEqual(learn.find_drops(recovered, [rule], 30)[0]["index"], 1)

    def test_report_is_valid_html_and_short(self):
        text, sugg = learn.build_report(history(), [], [], {"pairs": ["BTCUSDT", "SOLUSDT"], "entry_tf": "1h",
                                                            "backtest": {"bars": 3000}})
        self.assertIn("/apply 1", text)
        self.assertEqual(len(sugg), text.count("/apply "))
        self.assertLess(len(text), 4000)                                       # fits one Telegram message
        bare = text
        for tag in ("<b>", "</b>", "<i>", "</i>"):
            bare = bare.replace(tag, "")
        self.assertNotIn("<", bare)              # labels like "volume <1x" are escaped for Telegram HTML

    def test_label_with_angle_bracket_is_escaped(self):
        h = history()
        for t in h:                              # make "volume <1x" the losing condition
            t["features"]["vol_ratio"] = 0.8 if t["symbol"] == "SOLUSDT" else 1.3
        text, _ = learn.build_report(h, [], [], {"pairs": ["SOLUSDT"], "entry_tf": "1h", "backtest": {"bars": 3000}})
        self.assertIn("volume &lt;1x", text)


class JournalTests(unittest.TestCase):
    def test_record_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as t:
            s = sig()
            s.realized_r, s.exit_reason, s.mfe_r = -1.0, "stop loss", 0.4
            learn.record({"alerts": {"log_dir": t}}, s)
            rows = learn.load_journal(Path(t))
            self.assertEqual(rows[0]["symbol"], "SOLUSDT")
            self.assertEqual(rows[0]["features"]["adx"], 30.0)
            self.assertEqual(learn.DIMS["volume"](rows[0]), "1-1.5x")

    def test_mfe_mae_tracked(self):
        s = Signal("X", "LONG", "1h", H, 100.0, 98.0, [102.0, 104.0, 106.0], 70, [], [], 1.0, sl_now=98.0)
        tracker.update(s, Candle(1 * H, 2 * H, 100, 101.5, 99.0, 101, 1), 0.0, 48, H)
        self.assertAlmostEqual(s.mfe_r, 0.75)
        self.assertAlmostEqual(s.mae_r, 0.5)

    def test_old_state_without_features_still_loads(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "s.json"
            legacy ={"symbol": "BTCUSDT", "side": "SHORT", "tf": "1h", "time": 0, "entry": 1.0, "sl": 2.0,
                      "tps": [0.5], "score": 70, "setups": ["pullback"], "reasons": [], "atr": 0.1}
            p.write_text(json.dumps({"open": {"BTCUSDT": legacy}, "closed": []}))
            st = State(p)
            self.assertEqual(st.open["BTCUSDT"].features, {})
            self.assertIsNone(learn.DIMS["adx"](learn.to_record(st.open["BTCUSDT"], "live")))


class RuleInScanTests(unittest.TestCase):
    def test_rule_blocks_auto_trade_but_alerts_and_tracks(self):
        with tempfile.TemporaryDirectory() as t:
            st = State(Path(t) / "s.json")
            cfg = {"pairs": ["SOLUSDT"], "watch_pairs": [], "entry_tf": "1h", "trend_tf": "4h",
                   "data_source": "bybit", "rules": {"max_bars_open": 48, "cooldown_bars": 6, "min_score": 60},
                   "risk": {"account_size": 3000, "risk_pct": 1, "leverage": 10, "fee_pct": 0.05},
                   "alerts": {"log_dir": t},
                   "learn_rules": [{"id": "setup=breakout", "cond": {"setup": "breakout"},
                                    "label": "breakout setups"}]}
            trader = mock.Mock(active=True)
            trader.sync.return_value = []
            sent = []
            n = mock.Mock()
            n.signal.side_effect = lambda s, note="": sent.append(note)
            close = int(time.time() * 1000) - 60_000
            with mock.patch.object(scanner, "_evaluate_pair", return_value=("SOLUSDT", sig(), close)):
                scan_once(cfg, st, n, trader=trader)
            trader.open.assert_not_called()
            self.assertIn("your rule", sent[0])
            self.assertIn("SOLUSDT", st.open)


class RemoteLearnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"pairs": ["BTCUSDT"], "watch_pairs": [], "entry_tf": "1h", "trend_tf": "4h",
                    "rules": {"min_score": 60, "cooldown_bars": 6}, "risk": {"account_size": 3000, "risk_pct": 1},
                    "alerts": {"log_dir": self.tmp.name, "telegram": False}}
        n = mock.Mock(tg=False, tg_chat="1")
        self.r = remote.Remote(self.cfg, State(Path(self.tmp.name) / "s.json"), n, threading.RLock(),
                               threading.Event())

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_rules_drop(self):
        self.assertIn("/review first", self.r.handle("/apply 1"))
        learn.save_suggestions(Path(self.tmp.name), learn.find_suggestions(history(), [], [], 30))
        self.assertIn("Rule 1 added", self.r.handle("/apply 1"))
        self.assertIn("already active", self.r.handle("/apply 1"))
        self.assertEqual(self.cfg["learn_rules"][0]["cond"], {"setup": "breakout"})
        saved = json.loads((Path(self.tmp.name) / "settings.json").read_text())
        self.assertEqual(saved["learn_rules"][0]["id"], "setup=breakout")      # survives restarts
        self.assertIn("breakout setups", self.r.handle("/rules"))
        self.assertIn("auto-traded again", self.r.handle("/drop 1"))
        self.assertEqual(self.cfg["learn_rules"], [])


if __name__ == "__main__":
    unittest.main()
