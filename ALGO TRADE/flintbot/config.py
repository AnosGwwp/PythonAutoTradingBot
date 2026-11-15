"""Load and validate config.toml into plain dataclasses."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path

SLOT_SEC = 0.4  # nominal Solana slot time


@dataclass
class Config:
    # network / market / loop
    network: str
    mcp_url: str
    pair: str
    tick_sec: float
    status_every_sec: float
    # strategy
    min_half_spread_bps: float
    max_half_spread_bps: float
    vol_mult: float
    vol_horizon_sec: float
    vol_halflife_sec: float
    vol_sample_sec: float
    skew_bps: float
    requote_bps: float
    heartbeat_sec: float
    levels: int
    level_size: Decimal
    size_growth: float
    step_bps: float
    staleness_slots: int
    reinstall_drift_pct: float
    # risk
    max_inventory_base: float
    max_drawdown_quote: float
    max_book_age_sec: float
    jump_bps: float
    jump_cooldown_sec: float
    max_consecutive_errors: int
    # paper
    start_base: Decimal
    start_quote: Decimal
    queue_share: float
    maker_fee_bps: float
    # live
    keypair_path: str
    program_id: str
    data_source: str
    balance_poll_sec: float
    degraded_spread_mult: float
    risk_per_slot_decay: str
    # journal
    journal_dir: str


def load(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    flat = {
        "network": raw["network"]["name"],
        "mcp_url": raw["network"]["mcp_url"],
        "pair": raw["market"]["pair"],
        **raw["loop"],
        **raw["strategy"],
        **raw["risk"],
        **raw["paper"],
        **raw["live"],
        "journal_dir": raw["journal"]["dir"],
    }
    types = {f.name: f.type for f in fields(Config)}
    unknown = set(flat) - set(types)
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    missing = set(types) - set(flat)
    if missing:
        raise ValueError(f"missing config keys: {sorted(missing)}")
    conv = {"float": float, "int": int, "str": str, "Decimal": lambda v: Decimal(str(v))}
    cfg = Config(**{k: conv[types[k]](v) for k, v in flat.items()})
    _validate(cfg)
    return cfg


def _validate(c: Config) -> None:
    errs = []
    if c.min_half_spread_bps <= 0 or c.max_half_spread_bps < c.min_half_spread_bps:
        errs.append("need 0 < min_half_spread_bps <= max_half_spread_bps")
    if not 1 <= c.levels <= 10:
        errs.append("levels must be 1..10")
    if not 1 <= c.staleness_slots <= 255:
        errs.append("staleness_slots must be 1..255 (on-chain byte)")
    # The heartbeat must refresh the fair before the on-chain staleness window closes,
    # otherwise every level silently stops filling.
    if c.heartbeat_sec + c.tick_sec >= c.staleness_slots * SLOT_SEC * 0.8:
        errs.append(
            f"heartbeat_sec+tick_sec must be < 80% of staleness window "
            f"({c.staleness_slots} slots ~ {c.staleness_slots * SLOT_SEC:.1f}s)"
        )
    if c.level_size <= 0 or c.max_inventory_base <= 0:
        errs.append("level_size and max_inventory_base must be > 0")
    if not 0 < c.queue_share <= 1:
        errs.append("queue_share must be in (0, 1]")
    if c.data_source not in ("mcp", "sdk"):
        errs.append("data_source must be 'mcp' or 'sdk'")
    if errs:
        raise ValueError("invalid config:\n  - " + "\n  - ".join(errs))
