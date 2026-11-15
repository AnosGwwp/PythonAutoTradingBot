"""Build dist/crypto-signals.zip with ONLY what the VPS needs.

  python deploy/pack_for_vps.py                 code + config
  python deploy/pack_for_vps.py --with-state    + open trades / history / phone settings (first move)
  python deploy/pack_for_vps.py --with-secrets  + .env with your Telegram/Bybit keys (keep the zip private)

Left out on purpose: the Flint market maker, Windows .bat launchers, caches and logs.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOP = "crypto-signals"
FILES = ["signals.py", "signals.toml", "bot", ".env.example", "VPS_SETUP.md",
         "deploy/install.sh", "deploy/crypto-signals.service", "deploy/pack_for_vps.py"]
GLOBS = ["signalbot/*.py", "tests/test_signals.py", "tests/test_remote.py", "tests/test_trader.py",
         "tests/test_news_scan.py", "tests/test_learn.py"]
STATE = ["logs/signals_state.json", "logs/settings.json", "logs/signals.csv", "logs/news_seen.json",
         "logs/calendar.json", "logs/journal.jsonl", "logs/learn.json"]
EXECUTABLE = {"bot", "deploy/install.sh"}


def add(z: zipfile.ZipFile, rel: str, data: bytes) -> None:
    if rel.endswith((".py", ".sh", ".toml", ".md", ".service", ".example", ".env")) or rel == "bot":
        data = data.replace(b"\r\n", b"\n")                 # Linux line endings
    info = zipfile.ZipInfo(f"{TOP}/{rel}", date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if rel in EXECUTABLE else 0o600 if rel == ".env" else 0o644
    info.external_attr = (0o100000 | mode) << 16
    z.writestr(info, data)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-state", action="store_true")
    ap.add_argument("--with-secrets", action="store_true")
    a = ap.parse_args()

    out = ROOT / "dist" / "crypto-signals.zip"
    out.parent.mkdir(exist_ok=True)
    names = list(FILES) + [str(p.relative_to(ROOT)).replace("\\", "/") for g in GLOBS for p in sorted(ROOT.glob(g))]
    with zipfile.ZipFile(out, "w") as z:
        for rel in names:
            add(z, rel, (ROOT / rel).read_bytes())
        if a.with_state:
            for rel in STATE:
                if (ROOT / rel).exists():
                    add(z, rel, (ROOT / rel).read_bytes())
                    names.append(rel)
        if a.with_secrets:
            from signalbot.notify import env
            keys = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "BYBIT_API_KEY", "BYBIT_API_SECRET"]
            body = "".join(f"{k}={env(k)}\n" for k in keys) + "TZ=Asia/Kolkata\n"
            add(z, ".env", body.encode())
            names.append(".env (your keys)")
    print(f"Created {out}  ({out.stat().st_size // 1024} KB, {len(names)} files)")
    for n in names:
        print("  " + n)


if __name__ == "__main__":
    main()
