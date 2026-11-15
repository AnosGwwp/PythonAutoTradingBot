"""Trade signal scanner: entry / stop-loss / take-profit alerts, optional Bybit auto-trading.

  python signals.py scan        run continuously (the bot)
  python signals.py check       test everything: data, Telegram, Bybit keys, clock, news feeds
  python signals.py once        show any setup on the last closed candle (print only)
  python signals.py backtest    test the rules on history
  python signals.py status      open signals + track record
  python signals.py telegram    find your Telegram chat id
  options: --pairs BTCUSDT,ETHUSDT  --tf 1h  --trend 4h  --bars 3000  --config signals.toml
"""
import sys

if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11 or newer is required (this is {sys.version.split()[0]}).")

import argparse
import logging
import logging.handlers
import os
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)                         # so logs/ and signals.toml resolve the same from anywhere

from signalbot import backtest, remote, scanner   # noqa: E402
from signalbot.data import TF_MS                   # noqa: E402
from signalbot.notify import dotenv                # noqa: E402

# timezone for message times / daily summary (TZ=Asia/Kolkata in .env on the VPS)
if hasattr(time, "tzset") and not os.environ.get("TZ") and dotenv().get("TZ"):
    os.environ["TZ"] = dotenv()["TZ"]
    time.tzset()


def load(path: str, a) -> dict:
    cfg = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    remote.apply_overrides(cfg)      # settings changed from the phone (logs/settings.json)
    if a.pairs:
        cfg["pairs"] = [p.strip().upper() for p in a.pairs.split(",") if p.strip()]
    if a.tf:
        cfg["entry_tf"] = a.tf
    if a.trend:
        cfg["trend_tf"] = a.trend
    if a.bars:
        cfg["backtest"]["bars"] = a.bars
    for k in ("entry_tf", "trend_tf"):
        if cfg[k] not in TF_MS:
            sys.exit(f"{k} must be one of {list(TF_MS)}")
    if TF_MS[cfg["trend_tf"]] <= TF_MS[cfg["entry_tf"]]:
        sys.exit("trend_tf must be a higher timeframe than entry_tf")
    if len(cfg["rules"]["tp_r"]) < 1 or cfg["rules"]["sl_atr_min"] > cfg["rules"]["sl_atr_max"]:
        sys.exit("check rules.tp_r and sl_atr_min/max")
    if not cfg["pairs"]:
        sys.exit("add at least one pair to `pairs` in signals.toml")
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="Trade signal scanner")
    ap.add_argument("cmd", choices=["scan", "check", "once", "backtest", "status", "telegram"])
    ap.add_argument("--config", default="signals.toml")
    ap.add_argument("--pairs")
    ap.add_argument("--tf")
    ap.add_argument("--trend")
    ap.add_argument("--bars", type=int)
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    cfg = load(a.config, a)

    if a.cmd == "scan":
        log_dir = Path(cfg["alerts"]["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "scanner.log", maxBytes=2_000_000,
                                                  backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
        logging.getLogger().addHandler(fh)
        sys.exit(scanner.run(cfg))
    if a.cmd == "check":
        from signalbot import doctor
        sys.exit(doctor.run(cfg))
    if a.cmd == "telegram":
        import getpass
        from signalbot.notify import env, telegram_setup
        token = env("TELEGRAM_BOT_TOKEN") or getpass.getpass("Paste bot token from @BotFather (hidden): ").strip()
        telegram_setup(token)
    elif a.cmd == "backtest":
        backtest.report(cfg)
    elif a.cmd == "status":
        scanner.status(cfg)
    elif a.cmd == "once":
        n = scanner.preview(cfg)
        print(f"\n{n} setup(s) across {len(scanner.all_pairs(cfg))} pairs on the last closed {cfg['entry_tf']} candle.")
        if n == 0:
            print("No setup right now. That's normal: the rules wait for trend + pullback/breakout alignment.")


if __name__ == "__main__":
    main()
