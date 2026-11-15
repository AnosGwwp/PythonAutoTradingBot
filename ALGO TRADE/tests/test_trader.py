import time
import unittest
from decimal import Decimal

from signalbot.bybit import floor_step, round_tick
from signalbot.strategy import Signal
from signalbot.trader import Trader


class FakeEx:
    def __init__(self, price=100.0, pos=None, avail=3000.0):
        self.price, self.pos, self.avail = price, pos or {}, avail
        self.orders, self.sl = [], None

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
        return {"equity": 3000.0, "available": self.avail, "upnl": 0.0}

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

    def test_daily_loss_limit(self):
        loser = sig()
        loser.traded, loser.realized_r, loser.risk_usd = True, -1.0, 100.0
        loser.closed_at = int(time.time() * 1000)
        self.assertIn("loss limit", self.make().open(sig(), [], [loser]))   # $100 > 3% of 3000

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

    def test_rounding(self):
        self.assertEqual(floor_step(0.02149, "0.001"), Decimal("0.021"))
        self.assertEqual(round_tick(64740.66, "0.10"), Decimal("64740.70"))


if __name__ == "__main__":
    unittest.main()
