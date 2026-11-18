# Run the bot 24/7 on a VPS

The bot is pure Python (3.11+) with **no packages to install**. It needs about 50 MB of RAM, so the
smallest VPS (1 vCPU / 512 MB–1 GB, Ubuntu 22.04 or 24.04) is enough.

## Choosing the VPS location

**Bybit and Binance block some countries' IPs, including the US. Don't pick a US or UK region.**
Bybit's servers are in Singapore, so a nearby region is fastest: Singapore, Tokyo, Mumbai or
Hong Kong. Frankfurt also works fine. Whatever region you pick, use one where *you* are allowed to
use Bybit. Step 3 below tests access from the VPS before anything runs.

## Option A: install from GitHub

```
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/<you>/PythonAutoTradingBot.git
cd "PythonAutoTradingBot/ALGO TRADE"
bash deploy/install.sh
```

Then stop the bot on your PC (step 3 below). To update later: push from the PC, then on the VPS
run `./bot update` (it pulls, runs the tests and restarts).

Your keys, open trades and phone settings are not in git (`.env` and `logs/` are ignored). The
installer asks for the keys. To carry over open trades and phone settings, copy them from the PC
before installing:

```
scp logs\signals_state.json logs\settings.json root@1.2.3.4:~/           (on the PC)
mkdir -p "logs" && mv ~/signals_state.json ~/settings.json logs/          (on the VPS, in ALGO TRADE)
```

## Smarter news with Claude (optional)

1. Create an API key at console.anthropic.com. Haiku 5.5 reading the headlines costs roughly
   $1 a month.
2. On the VPS, add it to `.env` with `nano .env`, on a new line: `ANTHROPIC_API_KEY=...`
3. Run `bash deploy/install.sh` again. It installs the `anthropic` package into `.venv`, points
   the service at it, and runs the health check. The "News AI" line should say `OK`.

Without a key the bot still works and reads headlines with keyword rules.

## Option B: install from a zip

## 1. Build the package (on your PC)

```
python deploy\pack_for_vps.py                 code + config
python deploy\pack_for_vps.py --with-state    + open trades, history and phone settings (first move)
python deploy\pack_for_vps.py --with-secrets  + your keys in .env (don't share the zip; delete it after uploading)
```

This creates `dist\crypto-signals.zip`. It contains only what the VPS needs: no Flint bot, no logs.

## 2. Upload and unzip

In PowerShell on your PC (replace `root@1.2.3.4` with your VPS login):

```
scp "dist\crypto-signals.zip" root@1.2.3.4:~
ssh root@1.2.3.4
```

On the VPS:

```
sudo apt-get update && sudo apt-get install -y unzip
unzip crypto-signals.zip
cd crypto-signals
bash deploy/install.sh
```

The installer does the rest:
1. finds or installs Python 3.11+
2. turns on clock sync
3. asks for your keys if you didn't include them, storing them in `.env` (readable only by you)
4. runs the **health check**
5. installs a background service that starts on boot and restarts after crashes

You get a Telegram message from the VPS when it's running.

## 3. Stop the bot on your PC (important)

**Only one copy may run at a time.** Two copies could open double trades. On the PC, close the
bot window (or press Ctrl+C in it).

If both run anyway, the bot warns you on Telegram: "Two copies of this bot are running".

## Daily use (on the VPS)

```
./bot logs       live log (Ctrl+C to leave)
./bot status     service state + open trades + track record
./bot restart    after editing signals.toml
./bot stop       stop it
./bot check      health check (data, Telegram, Bybit, clock, news)
```

Everything else works from your phone as before.

## Updating to a new version

From GitHub: push from the PC, then run `./bot update` on the VPS.

From a zip: on the PC, run `python deploy\pack_for_vps.py`. Upload the zip, then on the VPS:

```
scp "dist\crypto-signals.zip" root@1.2.3.4:~     (on the PC)
cd ~/crypto-signals && ./bot update               (on the VPS)
```

`update` keeps your `.env`, trade history and phone settings. It runs the tests, then restarts.

## Security checklist

- Use a Bybit key **without withdrawal permission**.
- For real money, restrict the key to the VPS IP. `./bot check` shows that IP.
- Log in with SSH keys instead of a password if you can.
- The service runs as your user and can only write inside the bot folder.
