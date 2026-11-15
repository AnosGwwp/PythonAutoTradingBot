import time
import unittest
from decimal import Decimal

from signalbot.bybit import floor_step, round_tick
from signalbot.strategy import Signal
from signalbot.trader import Trader


class FakeEx:
    def __init__(self, price=100.0, pos=None, avail=3000.0, equity=3000.0, closed=None):
        self.price, self.pos, self.avail, self.equity = price, pos or {}, avail, equity
        self.closed = closed if closed is not None else []
        self.orders, self.sl = [], None

    def closed_pnl(self, symbol=None, start_ms=None, limit=100):
        if isinstance(self.closed, Exception):
            raise self.closed
        return [r for r in self.closed if symbol in (None, r["symbol"])][:limit]

    def position(self, sym):
        return self.pos.get(sym)

    def positions(self):
        return dict(self.pos)

    def instrument(self, sym):
        return {"qty_step": "0.001", "min_qty": 0.001, "max_mkt_qty": 1000.0, "min_notional": 5.0,
                "tick": "0.01", "max_lev": 100.0}

    def last_price(self, sym):
        return self.price

    def wallet(self):
        return {"equity": self.equity, "available": self.avail, "upnl": 0.0}

    def set_leverage(self, sym, lev):
        self.lev = lev

    def market(self, sym, side, qty, reduce=False, sl=None):
        self.orders.append(("market", side, qty, reduce))
        if reduce:
            self.pos.pop(sym, None)
        else:
            self.pos[sym] = {"side": side, "size": float(qty), "avg": self.price}
            self.sl = sl

    def limit(self, sym, side, qty, price, reduce=True):
        self.orders.append(("limit", side, qty, price))

    def set_sl(self, sym, sl):
        self.sl = sl

    def cancel_all(self, sym):
        self.orders = [o for o in self.orders if o[0] != "limit"]


def cfg():
    return {"pairs": ["BTCUSDT"], "watch_pairs": ["DOGEUSDT"],
            "risk": {"account_size": 3000, "risk_pct": 1.0, "leverage": 10, "max_margin_pct": 30,
                     "maint_margin_pct": 0.5, "fee_pct": 0.05},
            "trading": {"enabled": True, "env": "demo", "max_open_trades": 3, "max_daily_loss_pct": 3}}


def sig(symbol="BTCUSDT", entry=100.0, sl=98.5):
    r = entry - sl
    return Signal(symbol, "LONG", "1h", 0, entry, sl, [entry + r, entry + 2 * r, entry + 3 * r],
                  70, ["pullback"], ["x"], 1.0, sl_now=sl)


class TraderTest(unittest.TestCase):
    def make(self, **kw):
        t = Trader(cfg())
        t.ex = FakeEx(**kw)
        return t

    def test_opens_with_stop_and_three_tps(self):
        t = self.make()
        s = sig()
        note = t.open(s, [], [])
        self.assertTrue(s.traded, note)
        self.assertAlmostEqual(s.qty, 20.0)                  # $30 risk / $1.5 stop
        self.assertEqual(t.ex.sl, Decimal("98.50"))
        tps = [o for o in t.ex.orders if o[0] == "limit"]
        self.assertEqual(len(tps), 3)
        self.assertEqual(sum(o[2] for o in tps), Decimal("20.000"))
        self.assertTrue(all(o[1] == "Sell" for o in tps))

    def test_skips(self):
        self.assertIn("watchlist", self.make().open(sig("DOGEUSDT"), [], []))
        self.assertIn("ran past", self.make(price=101.0).open(sig(), [], []))   # chase = 100.25
        self.assertIn("already hold", self.make(pos={"BTCUSDT": {"side": "Buy", "size": 1, "avg": 1}})
                      .open(sig(), [], []))
        self.assertIn("margin", self.make(avail=10).open(sig(), [], []))
        busy = [sig() for _ in range(3)]
        for b in busy:
            b.traded = True
        self.assertIn("max open", self.make().open(sig(), busy, []))

    def loser(self):
        s = sig("ETHUSDT")
        s.traded, s.realized_r, s.risk_usd = True, -1.0, 100.0
        s.closed_at = int(time.time() * 1000)
        return s

    def test_daily_loss_limit_uses_bybit_pnl(self):
        booked = [{"symbol": "ETHUSDT", "closedPnl": "-100"}]
        self.assertIn("loss limit", self.make(closed=booked).open(sig(), [], [self.loser()]))  # $100 > $90
        # Bybit booked only -$20 (e.g. the tracker's stop was worse than the real fill): keep trading
        self.assertNotIn("loss limit",
                         self.make(closed=[{"symbol": "ETHUSDT", "closedPnl": "-20"}]).open(sig(), [], [self.loser()]))
        # other coins on the account (not bot trades) don't count
        other = [{"symbol": "XRPUSDT", "closedPnl": "-500"}]
        self.assertNotIn("loss limit", self.make(closed=other).open(sig(), [], [self.loser()]))

    def test_daily_loss_limit_falls_back_to_tracked_results(self):
        from signalbot.bybit import BybitError
        t = self.make(closed=BybitError(-1, "network"))
        self.assertIn("loss limit", t.open(sig(), [], [self.loser()]))       # tracked: -1R x $100

    def test_daily_loss_limit_counts_open_losers(self):
        running = sig("SOLUSDT")
        running.traded, running.side = True, "SHORT"
        pos = {"SOLUSDT": {"side": "Sell", "size": 5, "avg": 100, "upnl": -95.0}}
        self.assertIn("loss limit", self.make(pos=pos).open(sig(), [running], []))

    def test_same_direction_limit(self):
        longs = [sig("ETHUSDT"), sig("SOLUSDT")]
        for s in longs:
            s.traded = True
        self.assertIn("double the same bet", self.make().open(sig(), longs, []))
        shorts = list(longs)
        for s in shorts:
            s.side = "SHORT"
        self.assertTrue(self.make().open(sig(), shorts, []).startswith("🤖 <b>Auto-traded"))

    def test_sized_from_equity_after_losses(self):
        t = self.make(equity=1500.0)
        s = sig()
        note = t.open(s, [], [])
        self.assertAlmostEqual(s.qty, 10.0)                  # 1% of $1500 = $15 / $1.5 stop
        self.assertIn("Bybit equity", note)

    def test_uncertain_order_error_checks_for_position(self):
        from signalbot.bybit import UNCERTAIN, BybitError

        class Flaky(FakeEx):
            def market(self, sym, side, qty, reduce=False, sl=None):
                super().market(sym, side, qty, reduce, sl)  # Bybit executed it...
                raise BybitError(UNCERTAIN, "HTTP 502 (outcome unknown)")   # ...but the reply was lost
        t = Trader(cfg())
        t.ex = Flaky()
        s = sig()
        t.open(s, [], [])
        self.assertTrue(s.traded)                            # found and managed, TPs placed
        self.assertEqual(len([o for o in t.ex.orders if o[0] == "limit"]), 3)

    def test_unsplittable_position_tracks_single_tp(self):
        t = self.make()
        t.ex.instrument = lambda sym: {"qty_step": "1", "min_qty": 10.0, "max_mkt_qty": 1000.0,
                                       "min_notional": 5.0, "tick": "0.01", "max_lev": 100.0}
        s = sig()                                            # 20 coins: thirds would be < min 10
        t.open(s, [], [])
        self.assertEqual(len(s.tps), 1)                      # tracker now closes all at TP1, like Bybit

    def test_sync_uses_real_exit_price(self):
        t = self.make(closed=[{"symbol": "BTCUSDT", "avgExitPrice": "101.25"}])
        s = sig()
        s.traded, s.traded_at = True, 0
        t.sync([s])
        self.assertEqual(t.sync([s])[0][1], 101.25)

    def test_disabled_does_nothing(self):
        t = self.make()
        t.cfg["trading"]["enabled"] = False
        s = sig()
        self.assertEqual(t.open(s, [], []), "")
        self.assertFalse(s.traded)

    def test_events_follow_tracker(self):
        t = self.make()
        s = sig()
        t.open(s, [], [])
        t.on_event(s, {"kind": "tp", "n": 1, "price": 101.5, "new_sl": 100.0})
        self.assertEqual(t.ex.sl, Decimal("100.00"))
        t.on_event(s, {"kind": "expired", "price": 101, "bars": 48})
        self.assertIsNone(t.ex.position("BTCUSDT"))

    def test_sync_needs_two_misses(self):
        t = self.make()
        s = sig()
        s.traded, s.traded_at = True, 0
        self.assertEqual(t.sync([s]), [])                    # 1st miss: wait (stop may be in flight)
        self.assertEqual(len(t.sync([s])), 1)                # 2nd miss: closed on Bybit

    def test_bybit_5xx_is_uncertain_4xx_is_rejected(self):
        import io
        import urllib.error
        from unittest import mock

        from signalbot.bybit import UNCERTAIN, Bybit, BybitError
        ex = Bybit("k", "s", "demo")
        for status, want in ((502, UNCERTAIN), (403, 403)):
            err = urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(b"oops"))
            with mock.patch("urllib.request.urlopen", side_effect=err):
                with self.assertRaises(BybitError) as cm:
                    ex.post("/v5/order/create", {})
            self.assertEqual(cm.exception.code, want)

    def test_rounding(self):
        self.assertEqual(floor_step(0.02149, "0.001"), Decimal("0.021"))
        self.assertEqual(round_tick(64740.66, "0.10"), Decimal("64740.70"))


if __name__ == "__main__":
    unittest.main()
