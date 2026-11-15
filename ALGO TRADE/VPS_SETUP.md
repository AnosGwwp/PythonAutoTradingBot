# Run the bot 24/7 on a VPS

The bot is pure Python (3.11+) with **no packages to install**. It needs about 50 MB of RAM, so the
smallest VPS (1 vCPU / 512 MB–1 GB, Ubuntu 22.04 or 24.04) is enough.

## Choosing the VPS location

**Bybit and Binance block some countries' IPs, including the US. Don't pick a US or UK region.**
Bybit's servers are in Singapore, so a nearby region is fastest: Singapore, Tokyo, Mumbai or
Hong Kong. Frankfurt also works fine. Whatever region you pick, use one where *you* are allowed to
use Bybit. Step 3 below tests access from the VPS before anything runs.

## 1. Build the package (on your PC)

Double-click **`PACK FOR VPS.bat`**. It asks two questions:
- **Bring open trades + history?** Answer *y* the first time you move the bot, so the VPS keeps
  managing trades already open on Bybit.
- **Include your keys?** *y* saves retyping them. The zip then contains secrets: don't share it,
  and delete it after uploading.

This creates `dist\crypto-signals.zip`. It contains only what the VPS needs: no Flint bot, no .bat
files, no logs.

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

**Only one copy may run at a time.** Two copies could open double trades. On the PC:
- close the bot window, then
- double-click `AUTOSTART OFF.bat`.

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

On the PC, run `PACK FOR VPS.bat` and answer **n** to both questions. Upload the zip, then on the VPS:

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
