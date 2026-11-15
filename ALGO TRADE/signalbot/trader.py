"""Auto-trading on Bybit.

For every new signal:
  1. skip if: watchlist pair, too many open trades, daily loss limit hit, you already hold that
     coin, or price has already run past the "don't chase" level
  2. size the position from your $ risk (same maths as the alerts), set leverage
  3. market order WITH the stop-loss attached, then 1 reduce-only limit order per take-profit
Afterwards the exchange follows the tracker:
  * TP hit  -> stop moved on Bybit (entry after TP1, TP1 after TP2)
  * stop / all TPs / expiry -> cancel leftover orders, close any remainder at market
  * position closed by you in the Bybit app -> trade marked closed here too
"""
from __future__ import annotations

import html
import logging
import time
from dataclasses import replace
from decimal import Decimal

from .bybit import Bybit, BybitError, floor_step, round_tick
from .notify import coin, env, px, r_usd, sizing
from .strategy import Signal, shift_entry

log = logging.getLogger("signals")


class Trader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        cfg.setdefault("trading", {})
        self.ex: Bybit | None = None
        self.error = ""
        self.missing: dict[str, int] = {}

    # ---------------------------------------------------------------- setup
    @property
    def t(self) -> dict:
        return self.cfg["trading"]

    @property
    def active(self) -> bool:
        return self.ex is not None and bool(self.t.get("enabled"))

    @property
    def label(self) -> str:
        e = self.t.get("env", "demo")
        return {"demo": "Bybit DEMO", "testnet": "Bybit testnet", "mainnet": "Bybit REAL MONEY"}[e]

    def connect(self) -> bool:
        name = self.t.get("env", "demo")
        if name == "mainnet" and not self.t.get("real_money_ok"):
            self.error = 'env = "mainnet" needs real_money_ok = true in signals.toml'
            return False
        key, secret = env("BYBIT_API_KEY"), env("BYBIT_API_SECRET")
        if not (key and secret):
            self.error = "BYBIT_API_KEY / BYBIT_API_SECRET are not set"
            return False
        ex = Bybit(key, secret, name)
        try:
            ex.sync_time()
            ex.wallet()
        except BybitError as e:
            self.error = str(e)
            return False
        for step in (ex.one_way_mode, lambda: self._isolated(ex)):
            try:
                step()
            except BybitError as e:
                log.warning("bybit setup: %s", e)
        self.ex, self.error = ex, ""
        if name != "testnet":
            self.cfg["data_source"] = "bybit"    # track the exact contract we trade
        return True

    def _isolated(self, ex: Bybit) -> None:
        if self.t.get("isolated", True) and ex.get("/v5/account/info").get("marginMode") != "ISOLATED_MARGIN":
            ex.post("/v5/account/set-margin-mode", {"setMarginMode": "ISOLATED_MARGIN"})
            log.info("bybit account switched to isolated margin")

    def status_line(self) -> str:
        if self.ex is None:
            return "🤖 Auto-trading: ⚠️ not connected" + (f" ({html.escape(self.error)})" if self.error else "")
        return f"🤖 Auto-trading: {'✅ ON' if self.t.get('enabled') else '⏹ OFF'} ({self.label})"

    # ---------------------------------------------------------------- new signal
    def open(self, sig: Signal, open_sigs: list[Signal], closed: list[Signal]) -> str:
        if not self.active:
            return ""
        t = self.t
        if sig.symbol in self.cfg.get("watch_pairs", []) and not t.get("trade_watchlist", False):
            return "🤖 Not auto-traded (watchlist pair)."
        if sum(s.traded for s in open_sigs) >= t.get("max_open_trades", 3):
            return "🤖 Not auto-traded: max open trades reached."
        limit = self.cfg["risk"]["account_size"] * t.get("max_daily_loss_pct", 3) / 100
        lost = self.today_loss(closed)
        if lost >= limit:
            return f"🤖 Not auto-traded: today's loss limit (${limit:,.0f}) reached. Resumes tomorrow."
        try:
            return self._open(sig)
        except Exception as e:      # never let an exchange problem kill the scanner or lose the alert
            log.exception("auto-trade %s failed", sig.symbol)
            extra = " A position WAS opened (with stop-loss); the bot manages it." if sig.traded else ""
            return f"⚠️ Auto-trade failed: {html.escape(str(e))}.{extra}"

    def _open(self, sig: Signal) -> str:
        ex, sym, long = self.ex, sig.symbol, sig.d == 1
        if ex.position(sym):
            return f"🤖 Not auto-traded: you already hold a {coin(sym)} position on Bybit."
        info = ex.instrument(sym)
        now = ex.last_price(sym)
        chase = sig.entry + sig.d * 0.25 * sig.atr
        if sig.d * (now - chase) > 0:
            return f"🤖 Not auto-traded: price {px(now)} already ran past {px(chase)}."
        if sig.d * (now - sig.sl) <= 0:
            return "🤖 Not auto-traded: price is already beyond the stop."
        plan = replace(sig)                        # size on a copy anchored at the current price;
        shift_entry(plan, now)                     # the alert keeps its levels if we skip
        s = sizing(plan, self.cfg)
        lev = max(1, min(s["lev"], int(info["max_lev"])))
        qty = floor_step(min(s["qty"], info["max_mkt_qty"]), info["qty_step"])
        if float(qty) < info["min_qty"] or float(qty) * now < info["min_notional"]:
            return f"🤖 Not auto-traded: size {qty} {coin(sym)} is below Bybit's minimum."
        margin = float(qty) * now / lev
        avail = ex.wallet()["available"]
        if avail < margin * 1.05:
            return f"🤖 Not auto-traded: needs ${margin:,.0f} margin, only ${avail:,.0f} available."

        ex.set_leverage(sym, lev)
        try:
            ex.market(sym, "Buy" if long else "Sell", qty, sl=round_tick(plan.sl, info["tick"]))
        except BybitError as e:
            if e.code != -1:                       # rejected by Bybit: nothing was opened
                raise
            log.warning("order network error, checking whether it went through: %s", e)
        pos = None
        for _ in range(15):
            pos = ex.position(sym)
            if pos:
                break
            time.sleep(0.4)
        if not pos:
            return "⚠️ Order not confirmed and no position on Bybit, so nothing was opened."
        sig.traded, sig.qty, sig.traded_at = True, pos["size"], int(time.time() * 1000)
        shift_entry(sig, pos["avg"])
        sig.risk_usd = pos["size"] * sig.risk
        problems = []
        try:
            ex.set_sl(sym, round_tick(sig.sl, info["tick"]))
        except BybitError as e:
            problems.append(f"stop not re-anchored ({e}); the original stop is active")
        placed = self._place_tps(sig, info, problems)
        msg = (f"🤖 <b>Auto-traded on {self.label}</b>\n"
               f"      {'Bought' if long else 'Sold'} {pos['size']:g} {coin(sym)} @ {px(pos['avg'])}  ·  {lev}x  ·  "
               f"margin ≈ ${pos['size'] * pos['avg'] / lev:,.0f}\n"
               f"      Stop-loss + {placed} take-profit order(s) are on the exchange.")
        if problems:
            msg += "\n⚠️ " + html.escape("; ".join(problems))
        return msg

    def _place_tps(self, sig: Signal, info: dict, problems: list) -> int:
        ex, sym = self.ex, sig.symbol
        side = "Sell" if sig.d == 1 else "Buy"
        total, n = Decimal(str(sig.qty)), len(sig.tps)
        part = floor_step(sig.qty / n, info["qty_step"])
        if float(part) < info["min_qty"]:
            slices = [(sig.tps[0], total)]
            problems.append("position too small to split; one take-profit at TP1 for all of it")
        else:
            slices = [(tp, part) for tp in sig.tps[:-1]] + [(sig.tps[-1], total - part * (n - 1))]
        placed = 0
        for tp, q in slices:
            try:
                ex.limit(sym, side, q, round_tick(tp, info["tick"]))
                placed += 1
            except BybitError as e:
                problems.append(f"TP {px(tp)} not placed ({e})")
        return placed

    # ---------------------------------------------------------------- follow the tracker
    def on_event(self, sig: Signal, ev: dict) -> str:
        """Mirror a tracker event on the exchange. Returns '' on success, else a warning."""
        if not sig.traded or self.ex is None:
            return ""
        sym = sig.symbol
        try:
            if ev["kind"] == "tp":
                if not self.ex.position(sym):
                    return ""
                try:
                    self.ex.set_sl(sym, round_tick(ev["new_sl"], self.ex.instrument(sym)["tick"]))
                except BybitError:
                    now = self.ex.last_price(sym)
                    if sig.d * (now - ev["new_sl"]) <= 0:      # already back through the new stop
                        self.flatten(sig)
                        return "🤖 Price was already back at the new stop, so the rest was closed at market."
                    raise
                return ""
            if ev["kind"] in ("stop", "done", "expired"):
                self.flatten(sig)
            return ""
        except Exception as e:
            log.exception("bybit follow-up %s", sym)
            return f"⚠️ Bybit didn't accept the update ({html.escape(str(e))}). Check the position in the app."

    def flatten(self, sig: Signal) -> float | None:
        """Cancel the bot's orders on this coin and close what's left. Returns closed size."""
        if self.ex is None:
            return None
        sym = sig.symbol
        self.ex.cancel_all(sym)
        pos = self.ex.position(sym)
        if pos:
            self.ex.market(sym, "Sell" if pos["side"] == "Buy" else "Buy", Decimal(str(pos["size"])), reduce=True)
            return pos["size"]
        return 0.0

    def sync(self, open_sigs: list[Signal]) -> list[tuple[Signal, float]]:
        """Trades whose Bybit position is gone (closed in the app). Needs 2 checks in a row so a
        stop that just triggered is reported by the tracker as a normal stop first."""
        traded = [s for s in open_sigs if s.traded]
        if not traded or self.ex is None:
            return []
        try:
            pos = self.ex.positions()
        except BybitError as e:
            log.warning("bybit sync: %s", e)
            return []
        now, gone = int(time.time() * 1000), []
        for s in traded:
            if s.symbol in pos or now - s.traded_at < 60_000:
                self.missing.pop(s.symbol, None)
                continue
            self.missing[s.symbol] = self.missing.get(s.symbol, 0) + 1
            if self.missing[s.symbol] >= 2:
                self.missing.pop(s.symbol)
                try:
                    self.ex.cancel_all(s.symbol)
                    gone.append((s, self.ex.last_price(s.symbol)))
                except BybitError as e:
                    log.warning("bybit sync %s: %s", s.symbol, e)
        return gone

    def orphans(self, open_sigs: list[Signal]) -> list[str]:
        """Coins with a Bybit position that this bot isn't managing."""
        if self.ex is None:
            return []
        mine = {s.symbol for s in open_sigs if s.traded}
        try:
            return [sym for sym in self.ex.positions() if sym not in mine]
        except BybitError:
            return []

    # ---------------------------------------------------------------- reporting
    def today_loss(self, closed: list[Signal]) -> float:
        today = time.strftime("%Y-%m-%d")
        pnl = sum(s.realized_r * r_usd(s, self.cfg) for s in closed
                  if s.traded and time.strftime("%Y-%m-%d", time.localtime(s.closed_at / 1000)) == today)
        return max(0.0, -pnl)

    def balance_text(self, open_sigs: list[Signal] | None = None) -> str:
        if self.ex is None:
            return self.status_line()
        w = self.ex.wallet()
        pos = self.ex.positions()
        mine = {s.symbol for s in (open_sigs or []) if s.traded}
        lines = [f"💰 <b>{self.label}</b>",
                 f"Equity ${w['equity']:,.2f}  ·  Free ${w['available']:,.2f}",
                 f"Open P&amp;L {'+' if w['upnl'] >= 0 else '-'}${abs(w['upnl']):,.2f}", ""]
        if not pos:
            lines.append("No open positions.")
        for sym, p in pos.items():
            lines.append(f"{'🟢' if p['side'] == 'Buy' else '🔴'} {coin(sym)} {'LONG' if p['side'] == 'Buy' else 'SHORT'} "
                         f"{p['size']:g} @ {px(p['avg'])}  ·  {p['lev']}x\n"
                         f"      P&amp;L {'+' if p['upnl'] >= 0 else '-'}${abs(p['upnl']):,.2f}  ·  "
                         f"stop {p['sl'] if p['sl'] not in (None, '', '0') else 'NONE ⚠️'}"
                         + ("" if open_sigs is None or sym in mine else "\n      <i>not managed by the bot</i>"))
        lines.append("\n" + self.status_line())
        return "\n".join(lines)
