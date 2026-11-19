"""Stops, news exits, journal completeness, smarter review, readable messages (offline)."""
import json
import re
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from signalbot import learn, notify, remote, scanner, tracker
from signalbot.data import Candle
from signalbot.scanner import State
from signalbot.strategy import Signal
from signalbot.trader import Trader
from test_trader import FakeEx, cfg as trader_cfg, sig as trader_sig

H = 3_600_000


def sig(side="LONG", entry=100.0, sl=98.0, tps=(102.0, 104.0, 106.0), symbol="BTCUSDT"):
    return Signal(symbol, side, "1h", H, entry, sl, list(tps), 70, ["pullback"], ["Trend up"], 1.0,
                  sl_now=sl, last_t=0)


def k(i, o, h, l, c):
    return Candle(i * H, (i + 1) * H, o, h, l, c, 1.0)


def base_cfg(tmp):
    return {"pairs": ["BTCUSDT", "SOLUSDT"], "watch_pairs": [], "entry_tf": "1h", "trend_tf": "4h",
            "data_source": "bybit", "rules": {"min_score": 60, "cooldown_bars": 6, "max_bars_open": 48,
                                              "tp_r": [1.0, 2.0, 3.0], "setups": ["pullback", "breakout"]},
            "risk": {"account_size": 3000, "risk_pct": 1.0, "leverage": 10, "fee_pct": 0.0},
            "backtest": {"bars": 3000}, "alerts": {"log_dir": tmp, "telegram": False}, "news": {}}


class EarlyBreakevenTests(unittest.TestCase):
    def test_stop_moves_to_entry_at_trigger_and_closes_flat(self):
        s = sig()
        ev = tracker.update(s, k(1, 100, 101.2, 99.5, 101), 0.0, 48, H, breakeven_at_r=0.5)
        self.assertEqual([e["kind"] for e in ev], ["breakeven"])
        self.assertEqual(s.sl_now, 100.0)
        ev = tracker.update(s, k(2, 101, 101.1, 99.9, 100), 0.0, 48, H, breakeven_at_r=0.5)
        self.assertEqual((ev[0]["kind"], ev[0]["breakeven"], s.exit_reason), ("stop", True, "stop at breakeven"))
        self.assertAlmostEqual(s.realized_r, 0.0)

    def test_off_by_default(self):
        s = sig()
        self.assertEqual(tracker.update(s, k(1, 100, 101.9, 99.5, 101), 0.0, 48, H), [])
        self.assertEqual(s.sl_now, 98.0)


class StopCareTests(unittest.TestCase):
    def make(self):
        t = Trader(trader_cfg())
        t.ex = FakeEx()
        s = trader_sig()
        t.open(s, [], [])
        return t, s

    def test_stop_move_is_confirmed_from_bybit(self):
        t, s = self.make()
        t.ex.pos["BTCUSDT"]["sl"] = "100.00"
        note = t.on_event(s, {"kind": "tp", "n": 1, "price": 101.5, "new_sl": 100.0})
        self.assertIn("Bybit confirms", note)
        t.ex.pos["BTCUSDT"]["sl"] = "98.50"                  # Bybit didn't take it
        self.assertIn("Bybit shows the stop at 98.50", t.on_event(s, {"kind": "tp", "n": 1, "price": 101.5,
                                                                      "new_sl": 100.0}))

    def test_audit_repairs_missing_or_looser_stop_but_respects_tighter(self):
        t, s = self.make()
        s.tp_hit, s.sl_now = 1, s.entry                       # tracker: stop should be at entry
        for have, fixed in (("", True), ("98.50", True), ("100.50", False)):
            t._stop_fix_tried.clear()
            t.ex.sl = None
            out = t.audit_stops([s], {"BTCUSDT": {"size": 20, "sl": have}})
            self.assertEqual(bool(out), fixed, have)
            if fixed:
                self.assertEqual(t.ex.sl, Decimal("100.00"))
                self.assertIn("repaired", out[0][1])

    def test_audit_reports_a_refused_fix_once(self):
        t, s = self.make()
        from signalbot.bybit import BybitError

        def refuse(sym, sl):
            raise BybitError(10001, "no")
        t.ex.set_sl = refuse
        first = t.audit_stops([s], {"BTCUSDT": {"size": 20, "sl": ""}})
        again = t.audit_stops([s], {"BTCUSDT": {"size": 20, "sl": ""}})
        self.assertIn("refused", first[0][1])
        self.assertEqual(again, [])

    def test_realized_pnl_sums_bybit_records_of_the_trade(self):
        t, _ = self.make()
        t.ex.closed = [{"symbol": "BTCUSDT", "closedPnl": "10.5", "createdTime": "5000"},
                       {"symbol": "BTCUSDT", "closedPnl": "-3.0", "createdTime": "6000"},
                       {"symbol": "BTCUSDT", "closedPnl": "99", "createdTime": str(10 ** 12)}]   # a later trade
        self.assertEqual(t.realized_pnl("BTCUSDT", 4000, 7000), (7.5, 2))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = base_cfg(self.tmp.name)
        self.st = State(Path(self.tmp.name) / "s.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_exchange_pnl_is_collected_and_attached(self):
        s = sig()
        s.traded, s.traded_at, s.status, s.realized_r, s.closed_at = True, 1000, "closed", -1.0, 9000
        learn.on_closed(self.cfg, self.st, s)
        trader = mock.Mock(**{"realized_pnl.return_value": (-29.4, 1)})
        learn.collect_exchange_pnl(self.cfg, self.st, trader, int(time.time() * 1000))   # too early
        self.assertEqual(len(self.st.meta["pnl_pending"]), 1)
        later = int(time.time() * 1000) + 200_000
        learn.collect_exchange_pnl(self.cfg, self.st, trader, later)
        self.assertEqual(self.st.meta["pnl_pending"], [])
        self.assertEqual(learn.load_journal(Path(self.tmp.name))[0]["exchange_pnl"], -29.4)

    def test_backfill_adds_older_trades_once(self):
        old = sig()
        old.status, old.realized_r = "closed", 1.2
        self.assertEqual(learn.backfill(self.cfg, [old]), 1)
        self.assertEqual(learn.backfill(self.cfg, [old]), 0)
        self.assertTrue(learn.load_journal(Path(self.tmp.name))[0]["backfilled"])

    def test_slippage_and_events_recorded(self):
        s = sig()
        s.features["signal_price"] = 100.0
        from signalbot.strategy import shift_entry
        shift_entry(s, 100.4)                                 # filled 0.2R worse for a long (R = 2)
        s.events.append("11-19 10:00 TP1 @ 102")
        rec = learn.to_record(s, "live")
        self.assertAlmostEqual(rec["slippage_r"], 0.2)
        self.assertEqual(rec["events"], ["11-19 10:00 TP1 @ 102"])

    def test_diagnose(self):
        gave = {"realized_r": -1.0, "exit_reason": "stop loss", "mfe_r": 0.8, "bars": 6, "features": {"news": "against"}}
        text, tags = learn.diagnose(gave)
        self.assertIn("gave back a profit", text)
        self.assertEqual(tags, ["gave_back", "against_news"])
        fast = {"realized_r": -1.0, "exit_reason": "stop loss", "mfe_r": 0.1, "bars": 1}
        self.assertEqual(learn.diagnose(fast)[1], ["fast_fail"])


class WhatIfTests(unittest.TestCase):
    def test_only_changes_better_in_both_halves_are_suggested(self):
        def rec(t, r):
            return {"time": t * H, "realized_r": r, "symbol": "BTCUSDT", "side": "LONG", "setups": ["pullback"]}
        baseline = [rec(i, 0.0) for i in range(60)]
        results = {75: [rec(i, 0.3) for i in range(60)],                         # better everywhere
                   80: [rec(i, 2.0 if i < 30 else -0.5) for i in range(60)]}     # better only in one half

        def fake_sim(sym, S, T, c, btc_T=None):
            ms = c["rules"].get("min_score")
            return [SimpleNamespace(**r) for r in results.get(ms, [])] if sym == "BTCUSDT" else []
        cfg = base_cfg("x")
        with mock.patch("signalbot.backtest.simulate", side_effect=fake_sim), \
                mock.patch.object(learn, "to_record", side_effect=lambda s, src: vars(s)):
            out = learn.what_if({"BTCUSDT": (None, None)}, cfg, baseline, 30)
        by = {w["value"]: w for w in out if w["param"] == "min_score"}
        self.assertTrue(by[75]["robust"])
        self.assertFalse(by[80]["robust"])                    # more total R, but luck in one half

    def test_apply_setting_from_phone(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = base_cfg(t)
            r = remote.Remote(cfg, State(Path(t) / "s.json"), mock.Mock(tg=False, tg_chat="1"),
                              threading.RLock(), threading.Event())
            learn.save_suggestions(Path(t), [{"type": "setting", "label": "min strength 75", "param": "min_score",
                                              "value": 75, "id": "set:min_score=75"}])
            self.assertIn("Changed", r.handle("/apply 1"))
            self.assertEqual(cfg["rules"]["min_score"], 75)
            self.assertEqual(json.loads((Path(t) / "settings.json").read_text())["min_score"], 75)


class NewsExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = base_cfg(self.tmp.name)
        self.st = State(Path(self.tmp.name) / "s.json")
        self.sent = []
        self.n = SimpleNamespace(alert=self.sent.append, event=lambda s, ev, note="": self.sent.append((ev, note)))

    def tearDown(self):
        self.tmp.cleanup()

    def story(self, sev):
        return {"title": "Bitcoin exchange hacked", "why": "Large hack.", "by": "ai", "severity": sev,
                "direction": "bearish", "key": "k"}

    def test_major_news_closes_a_bybit_trade(self):
        s = sig()
        s.traded = True
        self.st.open["BTCUSDT"] = s
        trader = mock.Mock(**{"ex.last_price.return_value": 99.0})
        scanner.react_to_news([{"pair": "BTCUSDT", "side": "LONG", "story": self.story(3), "act": "close"}],
                              self.cfg, self.st, trader, self.n)
        trader.flatten.assert_called_once_with(s)
        self.assertEqual(self.st.open, {})
        self.assertEqual(s.exit_reason, "closed: major bad news")
        self.assertEqual(learn.load_journal(Path(self.tmp.name))[0]["exit_reason"], "closed: major bad news")
        self.assertIn("major news", plain_all(self.sent))

    def test_notable_news_protects_a_winner_and_only_warns_on_a_loser(self):
        s = sig()
        self.st.open["BTCUSDT"] = s
        act = [{"pair": "BTCUSDT", "side": "LONG", "story": self.story(2), "act": "protect"}]
        with mock.patch.object(scanner, "price", return_value=101.0):          # +0.5R: protect
            scanner.react_to_news(act, self.cfg, self.st, None, self.n)
        self.assertEqual(s.sl_now, 100.0)
        self.assertIn("stop moved to entry", plain_all(self.sent))
        s2 = sig(symbol="SOLUSDT")
        self.st.open["SOLUSDT"] = s2
        with mock.patch.object(scanner, "price", return_value=99.0):           # losing: just warn
            scanner.react_to_news([dict(act[0], pair="SOLUSDT")], self.cfg, self.st, None, self.n)
        self.assertEqual(s2.sl_now, 98.0)
        self.assertIn("/close SOL", plain_all(self.sent))


def plain_all(items):
    return " ".join(notify.plain(x if isinstance(x, str) else str(x)) for x in items)


class MessageTests(unittest.TestCase):
    TAGS = re.compile(r"</?(b|i|code|pre|a|blockquote)( [^>]*)?>")

    def assert_valid_html(self, text):
        bare = self.TAGS.sub("", text).replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")
        self.assertNotIn("<", bare)
        self.assertNotIn(">", bare)

    def test_signal_message_has_aligned_levels_and_risk_first(self):
        cfg = base_cfg("x")
        msg = notify.signal_message(sig(), cfg, "🤖 traded")
        self.assert_valid_html(msg)
        self.assertIn("<pre>", msg)
        self.assertRegex(notify.plain(msg), r"Stop\s+98\.00\s+-2\.0%")
        self.assertIn("Risk <b>$30.00</b>", msg)

    def test_trade_progress_bar(self):
        s = sig()
        self.assertIn("▓▓▓▓▓░░░░░", notify.trade_progress(s, 100.0))   # halfway between stop 98 and TP1 102
        self.assertEqual(notify.money(-7.014), "-$7.01")

    def test_status_and_trades_are_readable(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = base_cfg(t)
            st = State(Path(t) / "s.json")
            st.open["BTCUSDT"] = sig()
            done = sig()
            done.realized_r, done.status = -1.0, "closed"
            st.closed.append(done)
            r = remote.Remote(cfg, st, mock.Mock(tg=False, tg_chat="1"), threading.RLock(), threading.Event())
            with mock.patch.object(remote, "price", return_value=99.0):
                status, trades = r.handle("/status"), r.handle("/trades")
            for text in (status, trades):
                self.assert_valid_html(text)
            self.assertIn("BTC long  <b>-$15.00</b> (-0.50R)", status)
            self.assertIn("Total <b>-$30.00</b>", status)
            self.assertIn("1.0% away", trades)
            self.assertIn("▓", trades)

    def test_history_command(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = base_cfg(t)
            s = sig()
            s.status, s.realized_r, s.exit_reason, s.risk_usd, s.closed_at = "closed", -1.0, "stop loss", 30.0, 2 * H
            s.events += ["11-19 10:00 signal LONG @ 100", "11-19 11:00 stop @ 98 (after 0 TP)"]
            learn.record(cfg, s)
            text = learn.history_text(cfg)
            self.assert_valid_html(text)
            self.assertIn("-1.00R ≈ -$30.00", text)
            self.assertIn("stop @ 98", text)


if __name__ == "__main__":
    unittest.main()
