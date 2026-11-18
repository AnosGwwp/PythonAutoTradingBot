"""Auto-trading on Bybit.

For every new signal:
  1. skip if: watchlist pair, too many open trades (in total or in one direction), daily loss
     limit hit, you already hold that coin, or price has already run past the "don't chase" level
  2. size the position from your $ risk (same maths as the alerts, but never from more capital
     than the Bybit equity), set leverage
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

from .bybit import UNCERTAIN, Bybit, BybitError, floor_step, round_tick
from .notify import coin, env, px, r_usd, sizing
from .strategy import CHASE_ATR, Signal, shift_entry

log = logging.getLogger("signals")


def _price(v) -> float | None:
    """A Bybit price field ("" / "0" / None mean: not set)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _same_price(a, b, tick: str) -> bool:
    fa, fb = _price(a), _price(b)
    return fa is not None and fb is not None and round_tick(fa, tick) == round_tick(fb, tick)


class Trader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        cfg.setdefault("trading", {})
        self.ex: Bybit | None = None
        self.error = ""
        self.missing: dict[str, int] = {}
        self._moved: dict[str, int] = {}     # symbol -> TP fills whose stop move is already done
        self._stop_fix_tried: set[str] = set()
        self.last_positions: dict = {}       # positions seen by the last sync (for stop audits)

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
        same = sum(s.traded and s.side == sig.side for s in open_sigs)
        if same >= t.get("max_same_direction", 2):
            return (f"🤖 Not auto-traded: {same} {sig.side} trade(s) already open. Coins mostly move "
                    "together, so another one would just double the same bet.")
        try:
            positions = self.ex.positions()          # one call serves both checks below
            limit = self.cfg["risk"]["account_size"] * t.get("max_daily_loss_pct", 3) / 100
            if self.today_loss(closed, open_sigs, positions) >= limit:
                return f"🤖 Not auto-traded: today's loss limit (${limit:,.0f}) reached. Resumes tomorrow."
            return self._open(sig, positions)
        except Exception as e:      # never let an exchange problem kill the scanner or lose the alert
            log.exception("auto-trade %s failed", sig.symbol)
            extra = " A position WAS opened (with stop-loss); the bot manages it." if sig.traded else ""
            return f"⚠️ Auto-trade failed: {html.escape(str(e))}.{extra}"

    def _open(self, sig: Signal, positions: dict | None = None) -> str:
        ex, sym, long = self.ex, sig.symbol, sig.d == 1
        if (positions.get(sym) if positions is not None else ex.position(sym)):
            return f"🤖 Not auto-traded: you already hold a {coin(sym)} position on Bybit."
        self._moved.pop(sym, None)
        info = ex.instrument(sym)
        now = ex.last_price(sym)
        chase = sig.entry + sig.d * CHASE_ATR * sig.atr
        if sig.d * (now - chase) > 0:
            return f"🤖 Not auto-traded: price {px(now)} already ran past {px(chase)}."
        if sig.d * (now - sig.sl) <= 0:
            return "🤖 Not auto-traded: price is already beyond the stop."
        wallet = ex.wallet()
        capital = self.cfg["risk"]["account_size"]
        if 0 < wallet["equity"] < capital:         # after losses: risk % of what is really there
            capital = wallet["equity"]
        plan = replace(sig)                        # size on a copy anchored at the current price;
        shift_entry(plan, now)                     # the alert keeps its levels if we skip
        s = sizing(plan, {**self.cfg, "risk": {**self.cfg["risk"], "account_size": capital}})
        lev = max(1, min(s["lev"], int(info["max_lev"])))
        qty = floor_step(min(s["qty"], info["max_mkt_qty"]), info["qty_step"])
        if float(qty) < info["min_qty"] or float(qty) * now < info["min_notional"]:
            return f"🤖 Not auto-traded: size {qty} {coin(sym)} is below Bybit's minimum."
        margin = float(qty) * now / lev
        avail = wallet["available"]
        if avail < margin * 1.05:
            return f"🤖 Not auto-traded: needs ${margin:,.0f} margin, only ${avail:,.0f} available."

        ex.set_leverage(sym, lev)
        attached_sl = round_tick(plan.sl, info["tick"])
        try:
            ex.market(sym, "Buy" if long else "Sell", qty, sl=attached_sl)
        except BybitError as e:
            if e.code != UNCERTAIN:                # rejected by Bybit: nothing was opened
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
        # track the exact tick-rounded prices the orders sit at on Bybit
        sig.sl = sig.sl_now = float(round_tick(sig.sl, info["tick"]))
        sig.tps = [float(round_tick(tp, info["tick"])) for tp in sig.tps]
        sig.risk_usd = pos["size"] * sig.risk
        problems = []
        if capital < self.cfg["risk"]["account_size"]:
            problems.append(f"sized from your Bybit equity ${capital:,.0f}, which is below the "
                            f"${self.cfg['risk']['account_size']:,.0f} capital setting")
        if round_tick(sig.sl, info["tick"]) != attached_sl:       # filled away from the planned price
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
            sig.tps = sig.tps[:1]                  # the tracker follows the same plan as the exchange
            slices = [(sig.tps[0], total)]
            problems.append("position too small to split; one take-profit at TP1 for all of it")
        else:
            slices = [(tp, part) for tp in sig.tps[:-1]] + [(sig.tps[-1], total - part * (n - 1))]
        errors = ex.limit_batch(sym, side, [(q, round_tick(tp, info["tick"])) for tp, q in slices])
        for (tp, _), err in zip(slices, errors):
            if err:
                problems.append(f"TP {px(tp)} not placed ({err})")
        return sum(e is None for e in errors)

    # ---------------------------------------------------------------- follow the tracker
    def on_event(self, sig: Signal, ev: dict) -> str:
        """Mirror a tracker event on the exchange. Returns '' on success, else a warning."""
        if not sig.traded or self.ex is None:
            return ""
        sym = sig.symbol
        try:
            if ev["kind"] in ("tp", "breakeven"):
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
                return self.confirm_stop(sig, ev["new_sl"])
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
        self.last_positions = pos
        now, gone = int(time.time() * 1000), []
        for s in traded:
            if s.symbol in pos:
                self._early_stop(s, pos[s.symbol])
            if s.symbol in pos or now - s.traded_at < 60_000:
                self.missing.pop(s.symbol, None)
                continue
            self.missing[s.symbol] = self.missing.get(s.symbol, 0) + 1
            if self.missing[s.symbol] >= 2:
                self.missing.pop(s.symbol)
                try:
                    self.ex.cancel_all(s.symbol)
                    gone.append((s, self._exit_price(s)))
                except BybitError as e:
                    log.warning("bybit sync %s: %s", s.symbol, e)
        return gone

    # ---------------------------------------------------------------- stop checks
    def desired_stop(self, sig: Signal) -> float:
        """Where the stop should be: the tracker's stop, or tighter if Bybit already shows TP fills."""
        k = self._moved.get(sig.symbol, 0)
        if k <= sig.tp_hit:
            return sig.sl_now
        early = sig.entry if k == 1 else sig.tps[k - 2]
        return early if sig.d * (early - sig.sl_now) > 0 else sig.sl_now

    def confirm_stop(self, sig: Signal, want: float) -> str:
        """Read the stop back from Bybit. Returns a short note for the alert."""
        try:
            p = self.ex.positions().get(sig.symbol)
            tick = self.ex.instrument(sig.symbol)["tick"]
        except BybitError as e:
            return f"⚠️ Couldn't read the stop back from Bybit ({html.escape(str(e))})."
        if not p:
            return ""
        if _same_price(p.get("sl"), want, tick):
            return f"✅ Bybit confirms the stop at {px(want)}."
        return f"⚠️ Bybit shows the stop at {p.get('sl') or 'NONE'}, not {px(want)}. The bot retries on its next check."

    def audit_stops(self, open_sigs: list[Signal], positions: dict) -> list[tuple[Signal, str]]:
        """Each poll: is the stop on Bybit where it should be? Missing or looser stops are put back.
        A stop you moved CLOSER yourself is left alone. Returns (trade, message) for each repair."""
        out = []
        for s in open_sigs:
            p = positions.get(s.symbol)
            if not s.traded or not p:
                continue
            want = self.desired_stop(s)
            tick = self.ex.instrument(s.symbol)["tick"]
            have = _price(p.get("sl"))
            if have is not None and (_same_price(have, want, tick) or s.d * (have - want) > 0):
                self._stop_fix_tried.discard(s.symbol)
                continue                                    # right, or tighter than needed
            if s.symbol in self._stop_fix_tried:
                continue                                    # already tried and reported once
            self._stop_fix_tried.add(s.symbol)
            try:
                self.ex.set_sl(s.symbol, round_tick(want, tick))
                out.append((s, f"🔧 <b>{coin(s.symbol)} stop repaired on Bybit</b>: it was "
                               f"{'missing' if have is None else px(have)}, now {px(want)}."))
            except BybitError as e:
                out.append((s, f"⚠️ <b>{coin(s.symbol)}: the stop on Bybit is "
                               f"{'missing' if have is None else px(have)}, should be {px(want)}</b>, and Bybit "
                               f"refused the fix ({html.escape(str(e))}). Check it in the app."))
        return out

    def realized_pnl(self, symbol: str, start_ms: int, end_ms: int) -> tuple[float, int] | None:
        """What Bybit booked for one trade: (net P&L in USDT incl. fees, number of closing fills)."""
        if self.ex is None:
            return None
        try:
            rows = self.ex.closed_pnl(symbol, start_ms=max(0, start_ms - 1000), limit=50)
        except BybitError as e:
            log.warning("bybit closed P&L %s: %s", symbol, e)
            return None
        mine = [r for r in rows if int(r.get("createdTime") or r.get("updatedTime") or 0) <= end_ms + 600_000]
        if not mine:
            return None
        return sum(float(r.get("closedPnl") or 0) for r in mine), len(mine)

    def _early_stop(self, sig: Signal, p: dict) -> None:
        """A take-profit order filled on Bybit before the tracker saw it (the tracker works on
        CLOSED 1-minute candles): move the stop now, not a minute or two later. The tracker still
        books the TP when its candle closes; its own stop update then finds nothing to change."""
        n = len(sig.tps)
        if n < 2 or sig.qty <= 0:
            return
        filled = min(int((sig.qty - p["size"]) / (sig.qty / n) + 0.25), n - 1)   # TP slices gone
        if filled <= max(sig.tp_hit, self._moved.get(sig.symbol, 0)):
            return
        new_sl = sig.entry if filled == 1 else sig.tps[filled - 2]
        try:
            self.ex.set_sl(sig.symbol, round_tick(new_sl, self.ex.instrument(sig.symbol)["tick"]))
            self._moved[sig.symbol] = filled
            p["sl"] = str(new_sl)                    # keep the positions snapshot true for the stop audit
            log.info("%s: TP%d filled on Bybit, stop moved to %s right away", sig.symbol, filled, px(new_sl))
        except BybitError as e:
            log.warning("%s: early stop move failed (%s); the tracker retries at the candle close", sig.symbol, e)

    def _exit_price(self, sig: Signal) -> float:
        """Average price of the latest closing fill on Bybit since the trade opened, so a trade you
        closed in the app is recorded at your real exit (falls back to the last price)."""
        try:
            for r in self.ex.closed_pnl(sig.symbol, start_ms=sig.traded_at or None, limit=1):
                p = float(r.get("avgExitPrice") or 0)
                if p > 0:
                    return p
        except (BybitError, ValueError, TypeError) as e:
            log.warning("bybit exit price %s: %s", sig.symbol, e)
        return self.ex.last_price(sig.symbol)

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
    def today_loss(self, closed: list[Signal], open_sigs: list[Signal] = (), positions: dict | None = None) -> float:
        """Today's loss on bot trades in $ (0 if net positive).

        Realized P&L comes from Bybit's own records (real fills, fees included) and falls back to
        the tracked results if Bybit can't be asked. Open bot positions that are currently losing
        count too, so three trades heading for their stops can't slip past the limit."""
        start = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")) * 1000)  # local midnight
        closed_today = [s for s in closed if s.traded and s.closed_at >= start]
        open_traded = [s for s in open_sigs if s.traded]
        pnl = sum(s.realized_r * r_usd(s, self.cfg) for s in closed_today)
        if self.ex is not None and (closed_today or open_traded):
            syms = {s.symbol for s in closed_today + open_traded}
            try:
                pnl = sum(float(r.get("closedPnl") or 0) for r in self.ex.closed_pnl(start_ms=start)
                          if r.get("symbol") in syms)
            except (BybitError, ValueError, TypeError) as e:
                log.warning("bybit closed P&L unavailable, using tracked results: %s", e)
            if open_traded:
                try:
                    pos = positions if positions is not None else self.ex.positions()
                    pnl += sum(min(0.0, pos[s.symbol].get("upnl", 0.0)) for s in open_traded if s.symbol in pos)
                except BybitError as e:
                    log.warning("bybit positions unavailable for the loss limit: %s", e)
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
