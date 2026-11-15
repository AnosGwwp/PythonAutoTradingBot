"""Risk gate evaluated every tick. Returns OK, PAUSE (pull quotes, keep running) or KILL."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .feed import Book

OK, PAUSE, KILL = "ok", "pause", "kill"


@dataclass
class Verdict:
    action: str
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.peak_pnl = 0.0
        self.pause_until = 0.0
        self.errors = 0
        self._last_mid: float | None = None

    def record_error(self) -> Verdict:
        self.errors += 1
        if self.errors >= self.cfg.max_consecutive_errors:
            return Verdict(KILL, f"{self.errors} consecutive errors")
        return Verdict(PAUSE, f"error #{self.errors}")

    def record_success(self) -> None:
        self.errors = 0

    def check(self, book: Book, mid: float | None, pnl: float, now: float) -> Verdict:
        c = self.cfg
        # 1. Hard stop: drawdown from peak PnL.
        self.peak_pnl = max(self.peak_pnl, pnl)
        dd = self.peak_pnl - pnl
        if dd >= c.max_drawdown_quote:
            return Verdict(KILL, f"drawdown {dd:.4f} >= {c.max_drawdown_quote}")
        # 2. Data quality.
        age = now - book.ts
        if age > c.max_book_age_sec:
            return Verdict(PAUSE, f"book stale ({age:.1f}s)")
        if mid is None:
            return Verdict(PAUSE, "one-sided/empty book")
        # 3. Jump guard: a sudden move usually means informed flow; step aside briefly.
        prev, self._last_mid = self._last_mid, mid
        if prev is not None and abs(mid / prev - 1) * 1e4 > c.jump_bps:
            self.pause_until = now + c.jump_cooldown_sec
            return Verdict(PAUSE, f"jump {abs(mid / prev - 1) * 1e4:.0f}bps, cooling down")
        if now < self.pause_until:
            return Verdict(PAUSE, f"cooldown {self.pause_until - now:.0f}s")
        return Verdict(OK)
