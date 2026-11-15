# Quick start (Windows)

| Double-click | What it does |
| --- | --- |
| **Crypto Signals** (desktop icon) or `START SIGNALS.bat` | Starts the scanner. Keep the window open. It restarts itself if it crashes. |
| `SHOW STATUS.bat` | Shows open trades and your results so far |
| `AUTOSTART ON.bat` / `AUTOSTART OFF.bat` | Start the scanner automatically (minimized) when you log in to Windows |
| `PACK FOR VPS.bat` | Build `dist\crypto-signals.zip` to run the bot 24/7 on a server. See [VPS_SETUP.md](VPS_SETUP.md). |

Run `python signals.py check` at any time to test market data, Telegram, Bybit keys, the clock
and the news feeds in one go.

Telegram confirms when the scanner starts and when it stops. Starting it twice is harmless:
the second copy refuses to run, so you never get duplicate alerts. Signals arrive only while
the PC is on and awake.

## Phone control (Telegram)

While the scanner runs, the bot shows buttons (Status, Trades, Scan now, Settings, Pause,
Resume, Help) and answers commands. Type `/` in the chat to see the full menu:

| Command | Does |
| --- | --- |
| `/status`, `/trades` | Running state and results; open trades with live P&L |
| `/scan` | Check all pairs immediately |
| `/pause`, `/resume` | Stop or restart new signals. Open trades are still tracked while paused. |
| `/capital 3000`, `/risk 1`, `/lev 10` | Account size, % lost at the stop, exchange leverage |
| `/add SUI`, `/watch DOGE`, `/remove SUI`, `/pairs` | Main pairs (loud), watchlist (silent) |
| `/minscore 70`, `/tf 4h` | Signal strength filter, chart timeframe |
| `/close BTC`, `/closeall` | Close one trade or all bot trades (on Bybit too) and record the result |
| `/balance`, `/autotrade on/off` | Bybit equity and positions; switch order placing on or off |
| `/news`, `/summary` | Upcoming big events, price shocks and headlines; today's results |

The bot obeys only your own chat (`TELEGRAM_CHAT_ID`). Phone changes are saved in
`logs/settings.json` and override `signals.toml`. Delete that file to reset them.

## News guard

Free news feeds reach you after the price has already moved. Professional desks get headlines
from paid terminals, in milliseconds. So the bot uses news **defensively** and never trades a
headline:

- **Economic calendar** (US high-impact releases: CPI, FOMC, jobs). You get a Telegram heads-up
  30 minutes before. No new auto-trades from 15 minutes before until 30 minutes after.
- **Shock guard.** It watches 1-minute candles. If a coin moves more than 8× its normal range and
  at least 1.2% within 5 minutes, you get an alert within about 1–2 minutes, with any related
  headline, and new entries on that coin pause for 30 minutes. Price is the fastest news feed;
  this usually beats the headline itself.
- **Headlines.** CoinDesk, Cointelegraph, The Block and Decrypt. Important ones about your coins
  or macro arrive as silent alerts, at most 6 per hour. Bybit delisting notices for your coins are
  included.

A signal that lands inside a pause is still sent to you, marked "not auto-traded" with the reason.
All thresholds are in `[news]` in `signals.toml`.

**Other safety rules.**
- Signals older than 10 minutes (after downtime) are skipped.
- You get an alert if market data is unreachable for 5 minutes.
- A daily summary arrives at 22:00.
- Bybit positions the bot doesn't manage are flagged.

## Auto-trading on Bybit

With `[trading] enabled = true`, every new signal on a main pair becomes a real order. It is
currently set to `env = "demo"`, Bybit Demo Trading: real prices, play money.

1. **Checks.** The trade is skipped, and the alert says why, if:
   - the pair is on the watchlist
   - 3 bot trades are already open
   - today's losses have reached `max_daily_loss_pct`
   - you already hold that coin
   - price has run past the "don't chase" level
   - the margin is not available
2. **Order.** The bot sets the leverage, sends a market order with the **stop-loss attached**,
   then places 3 reduce-only limit take-profits of ⅓ each.
3. **Management.** The exchange follows the tracker:
   - After TP1 the stop moves to entry; after TP2 it moves to TP1.
   - On a stop, expiry or the last TP, leftover orders are cancelled and any remainder is closed
     at market.
   - If you close a position in the Bybit app, the bot notices and records it.

**Keys** are read from the Windows user variables `BYBIT_API_KEY` / `BYBIT_API_SECRET`. They
are never stored in project files.

**Real money.** Set `env = "mainnet"` **and** `real_money_ok = true`. Use a key with no
withdrawal permission. The bot assumes one-way position mode and switches the account to
isolated margin. Don't trade the same coins by hand on that account: the bot closes whatever
position exists on a coin when its trade ends.

**Leverage.** Each signal shows the position size, the margin at your leverage and an estimated
liquidation price. Position size comes from `risk_pct`, so leverage changes only the margin
you lock up, not the dollars you lose at the stop. If the stop would sit too close to
liquidation, the leverage is lowered for that trade. No single trade uses more than
`max_margin_pct` of capital as margin. Use **isolated** margin on the exchange.

---

# Signal scanner (any exchange)

The signal scanner generates entry / stop-loss / take-profit alerts for the pairs you choose.
You place the trades yourself, on TitanX or any other exchange. It uses free public data from
Binance USDT-M perpetuals, with Bybit as an automatic fallback. It needs no API key and no
installs.

```
python signals.py backtest          # ALWAYS first: how have these rules done on your pairs?
python signals.py once              # scan all pairs now
python signals.py scan              # run continuously, alert on every closed candle
python signals.py status            # open signals + live track record
python signals.py scan --pairs BTCUSDT,SUIUSDT --tf 4h --trend 1d
```

Edit `pairs`, timeframes, risk and account size in [signals.toml](signals.toml).

**Rules.** The scanner only trades in the direction of the higher-timeframe trend, defined as
EMA50 vs EMA200 with a rising or falling slope. There are two setups:
- **Pullback:** price dips into the EMA20 zone and is rejected, with rising (or, for shorts,
  falling) RSI and ADX ≥ 18.
- **Breakout:** price closes beyond the 20-bar range on at least 1.5× average volume, with
  ADX ≥ 20.

Each signal gets a 0–100 score from trend strength, volume, RSI and how well the setups agree.
Signals below `min_score` are not sent.

**Levels.**
- **Stop-loss:** placed just beyond the recent swing high or low, kept between 1.0 and 2.5 ATR
  from entry.
- **Take-profits:** TP1, TP2 and TP3 sit at 1R, 2R and 3R, where R is the distance from entry
  to the stop.
- **Management:** ⅓ of the position closes at each TP. After TP1 the stop moves to breakeven;
  after TP2 it moves to TP1. A signal that hits nothing within 48 bars expires.
- **Position size:** suggested from `account_size × risk_pct ÷ stop distance`, with leverage
  capped at `max_leverage`.

**Tracking.** Every signal is followed on 1-minute candles. The scanner reports each TP, stop
or expiry and logs the result in R. State survives restarts in `logs/signals_state.json`; the
full history is in `logs/signals.csv`.

**Telegram alerts (optional).**
1. Create a bot with @BotFather.
2. Set the environment variables `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Set `telegram = true` in the config.

**Honest backtest results** (Nov 2026, 3000 × 1h candles over 6 pairs, fees included):

| Configuration | Trades | Win % | Avg R | Older half | Newer half |
| --- | --- | --- | --- | --- | --- |
| 1h / 4h trend (default) | 331 | 53 | 0.00 | −0.23 | +0.22 |
| 1h / 4h, min score 70 | 267 | 55 | +0.02 | −0.23 | +0.27 |
| 4h / 1d trend | 201 | 53 | +0.02 | +0.12 | −0.09 |
| 15m / 1h trend | 617 | 51 | −0.07 | −0.11 | −0.03 |

The rules are disciplined, but they have **no proven edge**. Results flip between the two
halves of the data, so performance depends on market conditions. Avoid 15m. Use the backtest
on your own pairs and the live track record (`status`) before risking money. Treat the scanner
as a filter and a tool for risk discipline, not a money printer.

---

# Flint automated market maker

An automated market-making bot for [Flint](https://docs.flintlabs.dev), an on-chain (Solana)
exchange where trading bots act as **makers**. The bot posts bid and ask quotes, takers trade
against them, and the bot earns the spread.

```
python run.py                 # paper trading on live devnet data — no install, no keys
python run.py --minutes 30    # paper trade 30 min, then print a summary
python run.py --pair JitoSOL/PYUSD
python -m unittest discover -s tests
```

Paper mode uses only the Python standard library (3.11+). Market data comes from Flint's public
MCP endpoint `https://devnet.mcp.flint.trade/mcp`, which needs no sign-in.

## How it trades

Each tick (default 1 s):

1. **Read the market.** Fetch the L2 book (plus public fills in paper mode). The two requests
   run concurrently over persistent keep-alive connections.
2. **Fair price.** Use the top-of-book **microprice**, the mid-price weighted by order sizes,
   so it leans toward the side with less depth. In live mode the bot removes its own quotes from
   the book first, so it doesn't follow its own prices.
3. **Spread.** `half_spread = clamp(vol_mult × σ(10s), min, max) × SpreadProtect`.
   - Volatility (σ) is an EWMA (exponentially weighted moving average) of log returns sampled
     every 5 s, which filters out noise from changing order sizes.
   - At startup, σ is pre-loaded from the last 2 h of 1-minute candles.
   - `SpreadProtect` is Flint's own signal to quote wider during risky periods. The SDK reports
     it, and the bot also widens automatically if the SDK has failed over to Solana RPC.
4. **Inventory skew.** The reservation price (the price the bot quotes around) moves against
   the position: when the bot is long it quotes lower so it sells more. At the inventory cap, the
   side that would add to the position is pulled.
5. **Publish only when needed.** This uses Flint's OracleOffset model:
   - The bot installs the **ladder** (levels, sizes, offsets) once, which costs more.
   - After that, each tick sends only two numbers, a buy fair and a sell fair. Volatility
     widening and skew are both encoded in those two numbers.
   - A fair is sent only when it moves more than `requote_bps`, or when `heartbeat_sec` has
     passed, so the on-chain fair stays inside its `staleness_slots` window.
   - In a 2-minute devnet test this meant **34 transactions instead of 120**. Fewer
     transactions means lower fees.

## Risk controls

| Control | Action |
| --- | --- |
| Drawdown from peak PnL ≥ `max_drawdown_quote` | **Kill**: pull quotes and exit |
| Feed or submission errors × `max_consecutive_errors` | **Kill** |
| Book older than `max_book_age_sec`, or one-sided book | Pause (pull quotes) |
| Mid jumps > `jump_bps` in one tick | Pause for `jump_cooldown_sec` |
| Inventory at `max_inventory_base` | Stop adding to the position |
| Fair not refreshed (bot crash, network loss) | On-chain `staleness` stops your levels filling automatically (~10 s) |
| On-chain `RiskParams` | Flint backs off your quotes after heavy fill volume |
| Ctrl+C or kill | Market disabled on-chain before exit |

PnL is measured **against holding the starting portfolio**, so it shows trading edge rather
than price drift.

## Paper fill model

The paper model fills the bot's simulated quotes against **real taker fills** on Flint:

- A taker sell at price `p` fills the bot's bids priced `≥ p`. A taker buy fills asks priced
  `≤ p`.
- The bot captures `queue_share` (default 50%) of that taker's size. Flint splits fills among
  makers pro-rata, so a full share would be too optimistic.
- Fills are capped by balances and the inventory cap.
- Only fills that happen *after* a quote was placed count, so there is no look-ahead.

This model is conservative but not perfect. A taker might have traded differently if the
bot's quotes had been in the book. Devnet taker flow is light, so expect few paper fills.

All events (fills, installs, pauses, status, summary) are written to `logs/*.jsonl`.

## Going live

Live trading needs Flint maker onboarding. Only the Flint team can create a maker account.

1. **Get a `maker_id`.** Contact the Flint team: https://devnet.dash.flint.trade/contact.
   You will register a cold **admin wallet**.
2. **Set up on the dashboard.** Go to https://devnet.dash.flint.trade, page **Onchain**.
   Sign each step with the admin wallet:
   - Add a **quoter (hot) key** as a quoting authority. Generate it with
     `solana-keygen new -o quoter.json`. The quoter key also pays transaction fees, so fund it
     with a little SOL.
   - Enable the market you will quote, e.g. SOL/main.
   - Set a `soft_max_balance` on **both** sides. If you skip this, buy quotes never fill. This
     cap is also your hard on-chain inventory limit.
   - Deposit inventory.
3. **Install the SDK.** Get the package token from the dashboard **Developer** tab and follow
   [requirements-live.txt](requirements-live.txt).
4. **Configure** `[live]` in `config.toml`:
   - Set `keypair_path` to the quoter key. **Never use the admin key here.**
   - Check that `program_id` matches what the dashboard shows.
5. **Run a connectivity check.** It authenticates, verifies the program id and sends a canary
   transaction that changes nothing:
   ```
   python run.py --live --check
   ```
6. **Go live** with a small `level_size` and a tight `max_drawdown_quote`:
   ```
   python run.py --live
   ```

The live executor ([flintbot/live.py](flintbot/live.py)) follows Flint's documented Python SDK.
The SDK sits behind a private package index, so this code has **not been run against the real
SDK yet**. Always run `--check` first.

### Tuning tips

- Start with `requote_bps` at about ¼ of `min_half_spread_bps`. Raise it to save fees, lower it
  to stay closer to the market.
- Keep `heartbeat_sec + tick_sec` well below `staleness_slots × 0.4 s`. The config loader
  enforces this.
- If paper results show many fills on one side followed by price moving against you (adverse
  selection), raise `vol_mult` or `min_half_spread_bps`.

## Layout

```
run.py                 CLI entry point
config.toml            all parameters
flintbot/feed.py       MCP market data (stdlib, keep-alive)
flintbot/strategy.py   fair price, volatility, skew, ladder, requote rule
flintbot/risk.py       kill switches and pause logic
flintbot/paper.py      paper executor (fill simulator)
flintbot/live.py       live executor (Flint SDK: QuotingCore, OracleOffset)
flintbot/engine.py     main loop and journal
tests/test_core.py     offline unit tests
```

> Trading carries risk of loss. Run paper mode and devnet first. Mainnet requires an explicit
> confirmation prompt.
