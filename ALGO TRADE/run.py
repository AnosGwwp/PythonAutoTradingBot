"""Flint automated market maker.

  python run.py                     paper trading on live devnet data (default, safe)
  python run.py --minutes 30        paper trade for 30 minutes, then print a summary
  python run.py --live --check      live: connect, authenticate, send a no-op canary
  python run.py --live              live quoting (requires Flint maker onboarding)
"""
import argparse
import asyncio
import logging
import sys

from flintbot import config
from flintbot.engine import run


def main() -> None:
    ap = argparse.ArgumentParser(description="Flint automated market-making bot")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--live", action="store_true", help="submit real quotes via the Flint SDK")
    ap.add_argument("--check", action="store_true", help="with --live: connectivity check only")
    ap.add_argument("--minutes", type=float, help="stop after N minutes")
    ap.add_argument("--pair", help="override market.pair, e.g. JitoSOL/PYUSD")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    try:
        cfg = config.load(a.config)
    except (ValueError, FileNotFoundError) as e:
        sys.exit(str(e))
    if a.pair:
        cfg.pair = a.pair
    if a.live and cfg.network == "mainnet":
        if input("LIVE on MAINNET with real funds. Type YES to continue: ") != "YES":
            sys.exit("aborted")

    try:
        asyncio.run(run(cfg, live=a.live, check_only=a.check,
                        duration_sec=a.minutes * 60 if a.minutes else None))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
