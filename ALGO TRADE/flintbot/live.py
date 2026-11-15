"""Live executor: signs and submits OracleOffset quotes through the Flint Python SDK.

Written against the documented SDK surface (docs.flintlabs.dev/sdks/python + /sdks/quoting).
The SDK is distributed from Flint's private index, so it could not be exercised here:
run `python run.py --live --check` first, which connects, authenticates and sends a
no-op canary transaction without quoting anything.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from pathlib import Path

from .config import Config
from .feed import Book, Level, PairInfo
from .strategy import Ladder, Quote, dec

log = logging.getLogger("flintbot.live")


def _import_sdk():
    try:
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        import flint
        from flint.onchain import OffsetSpec, RiskParams
    except ImportError as e:
        raise SystemExit(
            f"Live mode needs the Flint SDK ({e}).\n"
            "Install it with your dashboard token — see README.md, 'Going live'."
        ) from e
    return Keypair, Pubkey, flint, OffsetSpec, RiskParams


class LiveExecutor:
    mode = "live"

    def __init__(self, cfg: Config, pair: PairInfo):
        self.cfg, self.pair = cfg, pair
        self.Keypair, self.Pubkey, self.flint, self.OffsetSpec, self.RiskParams = _import_sdk()
        self.client = self.core = self.market = None
        self.ladder: Ladder | None = None
        self.sent: Quote | None = None
        self.errors = 0                     # failed/unknown submissions since last success
        self._watchers: set[asyncio.Task] = set()
        self._bal = (0.0, 0.0)
        self._bal_ts = 0.0
        self.start_base: float | None = None
        self.start_quote: float | None = None

    # ----- lifecycle -----
    async def connect(self) -> None:
        c, f = self.cfg, self.flint
        if not c.keypair_path or not Path(c.keypair_path).is_file():
            raise SystemExit("live.keypair_path must point to your QUOTER key JSON (never the admin key).")
        kp = self.Keypair.from_json(Path(c.keypair_path).read_text())
        if c.network == "devnet":
            eps = f.Endpoints(public_url=f.DEVNET_PUBLIC_ENDPOINT, auth_url=f.DEVNET_MAKER_ENDPOINT)
            self.client = f.Client(eps, keypair=kp)
        else:
            self.client = f.Client(keypair=kp)

        session = await self.client.authenticate()
        log.info("authenticated as maker_id=%s quoter=%s", session.maker_id, kp.pubkey())

        markets = await self.client.list_markets()
        info = markets.get((self.pair.base_id, self.pair.quote_id))
        if info is None:
            raise SystemExit(f"market {self.pair.label} not in SDK catalog")
        # Never trust a program id from the network: compare against pinned config.
        if str(info.program_id) != c.program_id:
            raise SystemExit(f"program id mismatch: catalog={info.program_id} config={c.program_id}")

        self.core = await self.client.start_quoting_core(program_id=self.Pubkey.from_string(c.program_id))
        await self.client.wait_ready()                     # warm blockhash + slot streams
        if hasattr(self.core, "set_degraded_spread_mult"):
            self.core.set_degraded_spread_mult(c.degraded_spread_mult)
        self.market = self.core.market(self.pair.label)

        b, q = await self.balances(force=True)
        self.start_base, self.start_quote = b, q
        log.info("balances: %s=%s %s=%s", self.pair.base, b, self.pair.quote, q)

    async def canary(self) -> None:
        """Prove the quoter key can submit, without changing any strategy."""
        r = await self.core.send_canary()
        took = await r.landed_within(15)
        log.info("canary landed in %.0f ms", took * 1000 if isinstance(took, (int, float)) else -1)

    async def shutdown(self) -> None:
        try:
            await self.pull()
        finally:
            for t in list(self._watchers):
                t.cancel()
            if self.client is not None:
                await self.client.close()

    # ----- quoting -----
    def _specs(self, ladder: Ladder) -> list:
        return [self.OffsetSpec(price_offset=dec(o), size=str(s), staleness=self.cfg.staleness_slots)
                for o, s in zip(ladder.offsets, ladder.sizes)]

    async def install(self, ladder: Ladder, q: Quote, ts: float, with_risk: bool) -> None:
        """Install/replace the ladder + fair. Emits a params update, which also (re)enables
        the market. Risk is sent only on first install: re-sending resets its accumulator."""
        b = self.core.builder()
        oo = b.oracle_offset(self.market).spread(self._specs(ladder), self._specs(ladder))
        if with_risk:
            oo = oo.risk(self.RiskParams(per_slot_decay_factor=self.cfg.risk_per_slot_decay))
        oo.fair(buy_price=dec(q.bid_fair), sell_price=dec(q.ask_fair))
        receipt = await self.core.commit(b)
        await receipt.landed_within(20.0)       # block: everything after depends on this
        self.ladder, self.sent = ladder, q
        self.errors = 0
        log.info("ladder installed @%.6g (%d levels)", ladder.install_price, len(ladder.offsets))

    async def update(self, q: Quote, ts: float) -> None:
        """Cheap per-tick fair push. Does not block on landing: a newer sequence always
        supersedes an older one, so we watch the receipt in the background instead."""
        b = self.core.builder()
        b.oracle_offset(self.market).fair(buy_price=dec(q.bid_fair), sell_price=dec(q.ask_fair))
        receipt = await self.core.commit(b)
        self.sent = q
        t = asyncio.create_task(self._watch(receipt))
        self._watchers.add(t)
        t.add_done_callback(self._watchers.discard)

    async def _watch(self, receipt) -> None:
        try:
            await receipt.landed_within(10.0)
            self.errors = 0
        except asyncio.CancelledError:
            receipt.close()
            raise
        except Exception as e:                  # OnChainFailure / CommitTimeout / ...
            self.errors += 1
            log.warning("fair update not confirmed: %s: %s", type(e).__name__, e)

    async def pull(self) -> None:
        """Stop quoting without touching the installed strategy (params-only enable=False)."""
        if self.core is None or self.market is None:
            return
        b = self.core.builder()
        b.disable(self.market)
        receipt = await self.core.commit(b)
        await receipt.landed_within(15.0)
        self.sent = None
        log.info("quotes pulled (market disabled)")

    # ----- state -----
    async def balances(self, force: bool = False) -> tuple[float, float]:
        now = time.monotonic()
        if force or now - self._bal_ts >= self.cfg.balance_poll_sec:
            resp = await self.client.maker_balance()
            base = quote = 0.0
            for bal in resp.balances:
                sid = int(bal.spot_id.id)
                if sid == self.pair.base_id:
                    base = float(Decimal(bal.balance.value))
                elif sid == self.pair.quote_id:
                    quote = float(Decimal(bal.balance.value))
            self._bal, self._bal_ts = (base, quote), now
        return self._bal

    def inventory(self) -> float:
        return self._bal[0] - (self.start_base or 0.0)

    def pnl(self, mid: float) -> float:
        b, q = self._bal
        return (b - (self.start_base or 0.0)) * mid + (q - (self.start_quote or 0.0))

    def spread_mult(self) -> float:
        try:
            return max(1.0, float(self.core.spread_mult()))
        except Exception:
            return 1.0

    def own_levels(self) -> tuple[dict[float, float], dict[float, float]]:
        """Approximate our resting levels so the fair isn't computed off our own quotes."""
        if self.sent is None or self.ladder is None:
            return {}, {}
        sizes = [float(s) for s in self.ladder.sizes]
        bids = {} if self.sent.pull_bid else dict(zip(self.ladder.prices(self.sent.bid_fair, "bid"), sizes))
        asks = {} if self.sent.pull_ask else dict(zip(self.ladder.prices(self.sent.ask_fair, "ask"), sizes))
        return bids, asks


class SdkFeed:
    """Lower-latency book snapshots over gRPC using the SDK's public client."""

    def __init__(self, client, pair: PairInfo):
        from flint.gen.flint.spot.v1 import common_pb2
        from flint.gen.flint.spot.v1.market_data import messages_pb2 as md
        self._client, self._md, self._c = client, md, common_pb2
        self._pair = common_pb2.Pair(base_id=pair.base_id, quote_id=pair.quote_id, label=pair.label)

    async def book(self, label: str) -> Book:
        r = await self._client.market_data.GetBook(
            self._md.GetBookRequest(pair=self._pair, level=self._c.FEED_LEVEL_L2))
        lv = lambda xs: tuple(Level(float(x.price.value), float(x.size.value)) for x in xs)
        ts = r.l2.metadata.ts.micros / 1e6 or time.time()
        return Book(lv(r.l2.bids), lv(r.l2.asks), ts, int(r.l2.metadata.slot))
