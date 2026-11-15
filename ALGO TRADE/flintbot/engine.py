"""Main trading loop: read market -> risk gate -> compute quote -> publish only if needed."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from .config import Config
from .feed import FeedError, McpFeed
from .paper import PaperExecutor
from .risk import KILL, PAUSE, RiskManager
from .strategy import Strategy, microprice, needs_requote

log = logging.getLogger("flintbot")


class Journal:
    """Append-only JSONL event log (fills, installs, pauses, status, summary)."""

    def __init__(self, directory: str, mode: str, pair: str):
        Path(directory).mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = Path(directory) / f"{mode}-{pair.replace('/', '_')}-{stamp}.jsonl"
        self._f = self.path.open("a", encoding="utf-8")

    def write(self, ev: str, **kw) -> None:
        self._f.write(json.dumps({"t": round(time.time(), 3), "ev": ev, **kw}) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


async def run(cfg: Config, live: bool = False, check_only: bool = False,
              duration_sec: float | None = None) -> None:
    feed = McpFeed(cfg.mcp_url)
    pair = await feed.pair_info(cfg.pair)
    log.info("market %s (base id %d, quote id %d, lot %s, taker fee %.2f bps)",
             pair.label, pair.base_id, pair.quote_id, pair.lot_size, pair.fee_hbps / 100)

    if live:
        from .live import LiveExecutor, SdkFeed
        ex = LiveExecutor(cfg, pair)
        await ex.connect()
        if check_only:
            try:
                await ex.canary()
                log.info("live check passed: auth, catalog, program id and submission all OK")
            finally:
                await ex.client.close()
            return
        book_src = SdkFeed(ex.client, pair) if cfg.data_source == "sdk" else feed
    else:
        ex = PaperExecutor(cfg)
        book_src = feed

    journal = Journal(cfg.journal_dir, ex.mode, pair.label)
    strat = Strategy(cfg, lot_size=pair.lot_size)
    try:
        n = strat.vol.seed(await feed.candle_closes(pair.label), bar_sec=60.0)
        log.info("volatility seeded from %d one-minute returns: %.1f bps per %.0fs",
                 n, strat.vol.sigma(cfg.vol_horizon_sec) * 1e4, cfg.vol_horizon_sec)
    except FeedError as e:
        log.warning("could not seed volatility (%s); starting from min spread", e)
    risk = RiskManager(cfg)
    journal.write("start", mode=ex.mode, pair=pair.label, network=cfg.network)
    log.info("mode=%s  journal=%s  (Ctrl+C to stop)", ex.mode.upper(), journal.path)

    quoting = False
    need_install = True
    first_install = True
    sent = None
    sent_ts = 0.0
    n_updates = n_installs = 0
    fills_cursor: float | None = None
    last_mid: float | None = None
    last_pause_reason = ""
    last_status = 0.0
    t_start = time.monotonic()
    kill_reason = ""

    try:
        while True:
            t0 = time.monotonic()
            if duration_sec and t0 - t_start >= duration_sec:
                break
            now = time.time()

            # ---- 1. market data (book + public fills fetched concurrently) ----
            try:
                if ex.mode == "paper":
                    if fills_cursor is None:
                        book = await book_src.book(pair.label)
                        fills_cursor, fills = book.ts, []
                    else:
                        book, fills = await asyncio.gather(
                            book_src.book(pair.label),
                            feed.fills_since(pair.label, fills_cursor))
                else:
                    book, fills = await book_src.book(pair.label), []
                    await ex.balances()
                risk.record_success()
            except (FeedError, OSError, asyncio.TimeoutError) as e:
                v = risk.record_error()
                log.warning("data error: %s", e)
                if v.action == KILL:
                    kill_reason = v.reason
                    break
                await asyncio.sleep(min(2.0 * risk.errors, 10.0))
                continue

            # ---- 2. simulated fills (paper) ----
            if fills:
                fills_cursor = max(fills_cursor, fills[-1].ts)
                for e in ex.on_fills(fills):
                    journal.write(**e)
                    log.info("FILL %-4s %s @ %.6g", e["side"], f"{e['size']:.6g}", e["price"])

            # ---- 3. fair value + risk gate ----
            own_b, own_a = ex.own_levels()
            mid = microprice(book, own_b, own_a)
            if mid is not None:
                last_mid = mid
            pnl = ex.pnl(last_mid) if last_mid else 0.0
            v = risk.check(book, mid, pnl, now)
            if live and ex.errors >= cfg.max_consecutive_errors:
                v.action, v.reason = KILL, f"{ex.errors} unconfirmed submissions in a row"

            if v.action == KILL:
                kill_reason = v.reason
                break
            if v.action == PAUSE:
                if quoting:
                    await ex.pull()
                    quoting = False
                    need_install = live      # live: re-enable requires a params update
                if v.reason != last_pause_reason:
                    log.info("PAUSE: %s", v.reason)
                    journal.write("pause", reason=v.reason)
                    last_pause_reason = v.reason
                await _sleep_rest(t0, cfg.tick_sec)
                continue
            last_pause_reason = ""

            # ---- 4. quote: install ladder rarely, push fairs only when needed ----
            if need_install or strat.ladder_stale(mid):
                ladder = strat.build_ladder(mid)
                q = strat.compute(mid, now, ex.inventory(), ex.spread_mult())
                await ex.install(ladder, q, book.ts, with_risk=first_install)
                first_install = need_install = False
                n_installs += 1
                journal.write("install", price=mid, offsets=list(ladder.offsets),
                              sizes=[str(s) for s in ladder.sizes])
                sent, sent_ts, quoting = q, now, True
            else:
                q = strat.compute(mid, now, ex.inventory(), ex.spread_mult())
                if not quoting or needs_requote(q, sent, now - sent_ts, cfg):
                    await ex.update(q, book.ts)
                    sent, sent_ts, quoting = q, now, True
                    n_updates += 1

            # ---- 5. status ----
            if now - last_status >= cfg.status_every_sec:
                last_status = now
                b, qb = await ex.balances()
                bb, ba = book.best_bid, book.best_ask
                log.info("mid %.6g | book %s/%s | quote %.6g/%.6g | half %.1fbps vol %.1fbps "
                         "| inv %+.4f | %s %.6g %s %.6g | pnl %+.5f | pushes %d installs %d",
                         mid, f"{bb.price:.6g}" if bb else "-", f"{ba.price:.6g}" if ba else "-",
                         sent.bid_fair - strat.ladder.offsets[0], sent.ask_fair + strat.ladder.offsets[0],
                         q.half_spread_bps, q.sigma_bps, ex.inventory(), pair.base, b, pair.quote, qb,
                         pnl, n_updates, n_installs)
                journal.write("status", mid=mid, pnl=pnl, inv=ex.inventory(), half_bps=q.half_spread_bps,
                              vol_bps=q.sigma_bps, base=b, quote=qb, pushes=n_updates)

            await _sleep_rest(t0, cfg.tick_sec)
    finally:
        if kill_reason:
            log.error("KILL SWITCH: %s", kill_reason)
            journal.write("kill", reason=kill_reason)
        try:
            await ex.shutdown()
        except Exception as e:  # make sure the summary still prints
            log.error("shutdown error (check the dashboard that quoting stopped!): %s", e)
        summary = {"pnl": ex.pnl(last_mid) if last_mid else 0.0, "inventory": ex.inventory(),
                   "fair_pushes": n_updates, "installs": n_installs,
                   "runtime_sec": round(time.monotonic() - t_start)}
        if ex.mode == "paper":
            summary.update(fills=ex.n_fills, volume_quote=round(ex.volume_quote, 6),
                           fees=round(ex.fees, 6), base=ex.base, quote=ex.quote)
        journal.write("summary", **summary)
        journal.close()
        log.info("SUMMARY %s", json.dumps(summary))


async def _sleep_rest(t0: float, tick: float) -> None:
    await asyncio.sleep(max(0.0, tick - (time.monotonic() - t0)))
