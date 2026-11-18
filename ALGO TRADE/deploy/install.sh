#!/usr/bin/env bash
# One-time setup on an Ubuntu/Debian VPS. From the bot folder run:   bash deploy/install.sh
# Safe to run again (e.g. after changing keys): it keeps your .env and trade history.
set -euo pipefail
cd "$(dirname "$0")/.."
BOT_DIR="$(pwd)"
SERVICE=crypto-signals

echo "== 1/5 Python 3.11+ =="
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Installing python3.11 ..."
  sudo apt-get update -y && sudo apt-get install -y python3.11
  PY="$(command -v python3.11)"
fi
echo "using $PY ($("$PY" --version))"

# The bot itself needs no packages. The optional news AI needs `anthropic`, installed into a private
# .venv (Ubuntu refuses pip installs into the system Python). If that fails, keyword rules are used.
echo "== 1b/5 Python packages for the news AI =="
if [ ! -x .venv/bin/python ]; then
  if ! "$PY" -m venv .venv 2>/dev/null; then
    VER="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    sudo apt-get install -y "python$VER-venv" >/dev/null 2>&1 || sudo apt-get install -y python3-venv >/dev/null 2>&1 || true
    rm -rf .venv
    "$PY" -m venv .venv 2>/dev/null || echo "(could not create .venv; news will use keyword rules)"
  fi
fi
if [ -x .venv/bin/python ] && .venv/bin/python -m pip install -q --upgrade -r requirements.txt; then
  PY="$BOT_DIR/.venv/bin/python"
  echo "news AI package installed; using $PY"
else
  echo "(news AI package not installed; news will use keyword rules)"
fi
echo "$PY" > .python

echo "== 2/5 clock sync =="
sudo timedatectl set-ntp true 2>/dev/null || echo "(could not enable NTP; check the clock line in the health check)"

echo "== 3/5 secrets =="
if [ ! -f .env ]; then
  echo "Typing is hidden for tokens/keys. Stored only in $BOT_DIR/.env (readable by you only)."
  read -rsp "Telegram bot token: " TG; echo
  read -rp  "Telegram chat id: " CHAT
  read -rsp "Bybit API key (Enter = no auto-trading): " BK; echo
  BS=""
  if [ -n "$BK" ]; then read -rsp "Bybit API secret: " BS; echo; fi
  read -rsp "Anthropic API key for the news AI (Enter = keyword rules): " AK; echo
  umask 077
  printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\nBYBIT_API_KEY=%s\nBYBIT_API_SECRET=%s\nANTHROPIC_API_KEY=%s\nTZ=Asia/Kolkata\n' \
    "$TG" "$CHAT" "$BK" "$BS" "$AK" > .env
else
  echo ".env already exists, keeping it"
  if ! grep -q '^ANTHROPIC_API_KEY=' .env; then
    read -rsp "Anthropic API key for the news AI (Enter = keyword rules): " AK; echo
    printf 'ANTHROPIC_API_KEY=%s\n' "$AK" >> .env
  fi
fi
chmod 600 .env
mkdir -p logs
chmod +x bot

echo "== 4/5 health check =="
if ! "$PY" signals.py check; then
  echo; echo "Fix the PROBLEMS above (edit .env with: nano .env), then run: bash deploy/install.sh"
  exit 1
fi

echo "== 5/5 background service =="
sed -e "s|__USER__|$(id -un)|g" -e "s|__DIR__|$BOT_DIR|g" -e "s|__PY__|$PY|g" deploy/crypto-signals.service \
  | sudo tee /etc/systemd/system/$SERVICE.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE >/dev/null
sudo systemctl restart $SERVICE
sleep 5
if ! systemctl is-active --quiet $SERVICE; then
  echo "The service did not start. Its last log lines:"
  journalctl -u $SERVICE -n 30 --no-pager --output=cat || true
  exit 1
fi
systemctl --no-pager --lines=0 status $SERVICE | head -5 || true
echo
echo "Done. The bot runs 24/7, restarts after crashes and reboots, and messages you on Telegram."
echo "Useful:  ./bot logs   ./bot status   ./bot restart   ./bot stop   ./bot check"
