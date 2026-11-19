# Quick start

The bot runs 24/7 on a VPS as a systemctl service. See [VPS_SETUP.md](VPS_SETUP.md).

| Command | What it does |
| --- | --- |
| `python signals.py scan` | Runs the scanner in the foreground (Ctrl+C to stop). On the VPS the service does this. |
| `python signals.py status` | Shows open trades and your results so far |
| `python signals.py check` | Tests market data, Telegram, Bybit keys, the clock and the news feeds |

Telegram confirms when the scanner starts and when it stops. Starting it twice on one machine is
harmless: the second copy refuses to run, so you never get duplicate alerts. Don't run it on your
PC while the VPS is running it: the bot warns you on Telegram if two machines run it.

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
| `/review`, `/apply 1`, `/rules`, `/drop 1` | Learn from the trade history; approve or remove "don't auto-trade" rules |

The bot obeys only your own chat (`TELEGRAM_CHAT_ID`). Phone changes are saved in
`logs/settings.json` and override `signals.toml`. Delete that file to reset them.

## Stops

After TP1 the stop moves to entry, so the trade can't lose any more. After TP2 it moves to TP1.
Three things make sure this really happens on Bybit:
- **Confirmation.** Every stop move is read back from Bybit, and the alert says "✅ Bybit confirms
  the stop at ...".
- **Early moves.** While trades are open, the bot checks every 15 s. As soon as Bybit shows a
  take-profit fill, the stop is moved, without waiting for the next candle to close.
- **Stop audit.** On each check, a stop that is missing or looser than it should be is put back,
  and you're told. A stop you moved closer yourself is left alone.

`/trades` shows each stop's state (not moved yet / at entry / profit locked) and whether Bybit
agrees.

## Learning from its own trades

Every closed trade, auto-traded or alert-only, is added to `logs/journal.jsonl`, which is never
trimmed. Each entry records the indicator values on the signal candle (ADX, RSI, volume, trend
strength, distance from the EMA, stop width, time of day), how far price moved for and against
the trade, and the result.

The journal also records each trade's timeline (TP hits and stop moves, with times), the fill
compared with the signal price, and what Bybit really booked for it, fetched shortly after the
close. Trades that closed before the journal existed are added from the state file. `/history`
lists the latest trades in plain words.

`/review` is also sent every Sunday at `summary_hour`. It contains:
- **Your trades, explained one by one**: hit every target, gave back a profit, failed straight
  away, traded against the news, filled worse than the signal.
- **What stands out**: plain takeaways from your live trades, and how they compare with the
  history for the same weeks.
- **What-if tests**: the same history replayed with other settings (strength filter, earlier
  breakeven, other targets, one setup only). A change is suggested only if it beats the current
  rules in both the older and the newer half of the history.
- **Losing patterns**, such as "breakouts on SOL", to stop auto-trading.
- **How complete the data is.**

`/apply N` approves suggestion N, whether it's a setting change or a pattern to skip.

The bot gets about one signal a day, far too few to judge on live trades alone, so the evidence
comes from a rolling backtest of the same rules, with your live trades shown alongside.

**It only suggests.** A suggestion needs at least `min_trades` trades, a clearly negative average,
and losses in both the older and the newer half of the history. Your live trades must not
contradict it. Anything weaker is mostly bad luck, and acting on it would make the bot worse.

Send `/apply 1` to approve suggestion 1. From then on the bot stops auto-trading that kind of
setup. You still get the alert, and it is still tracked and journaled, so the review can tell you
when the condition recovers and suggest `/drop`. `/rules` lists your rules. The bot never raises
risk, leverage or size by itself. Rules are saved in `logs/settings.json`.

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
- **Headlines, read like a trader would.** CoinDesk, Cointelegraph, The Block, Decrypt and
  Bybit's delisting notices are checked every 3 minutes. Each new headline is assessed for:
  - which of your coins it concerns, or whether it moves the whole market
  - whether it's good or bad news
  - how serious it is: noise, minor, notable or major
  - whether it repeats a story already seen on another site (counted once, with the number of sites)

  Three readers, all optional except the first:
  - **Word list, free and always on** ([signalbot/lexicon.py](signalbot/lexicon.py)): over 500
    crypto phrases, each with a direction and strength. It understands:
    - negation: "denies hack" and "no evidence of exploit" are reassuring
    - guesses and opinion: "could", "price prediction" and analytics-firm commentary are minor
    - aftermath: sentencing and trials are minor
    - amounts: a $300K exploit isn't major, a $150M one is
    - recoveries: "funds returned" and "withdrawals resume" are good news
    - names: Saylor, MicroStrategy and IBIT mean BTC; Vitalik and Lido mean ETH; Jupiter and
      pump.fun mean SOL

    Every rules verdict names the words it found.
  - **CryptoPanic, paid plan, off by default.** To use it, set `[news] cryptopanic = true` and
    put the key in `CRYPTOPANIC_TOKEN`. It adds more news sources, plus how traders vote on each
    story.
    - Votes are re-checked as they come in.
    - 5+ votes can set a story's direction.
    - 5+ "important" votes can raise it to notable, but never to major. Closing trades still needs
      the content itself to say major.
    - Alerts show the votes and link back to CryptoPanic, as its terms require.
  - **Claude Haiku 5.5, paid** (`ANTHROPIC_API_KEY`, roughly $1 a month): reads meaning best.

  `/news` and `./bot check` show which readers are active.

How news affects trading. The bot never opens a trade because of news.
- **Major news** (hack, delisting, ban, ETF decision, surprise Fed move): no new auto-trades
  *against* it on that coin for 4 hours, for example no new SOL longs after a Solana exploit.
  Trades in the direction of the news are still allowed. Major market-wide news applies to
  every coin. These alerts arrive with sound.
- **Notable news** arrives as a silent alert, at most 6 per hour. Notable news doesn't block
  anything.
- **News against an open trade:**
  - **Major news closes the trade.** Market-wide major news closes trades only when the AI read
    it; keyword rules alone never close every trade at once.
  - **Notable news moves the stop to entry** if the trade is in profit, and otherwise sends a loud
    warning.
  - Each story acts once per trade. Alert-only trades get the same advice to carry out on your
    exchange. Switch off with `exit_on_major` / `protect_on_notable` in `signals.toml`.
- **Every signal shows the coin's news mood** over the last 12 hours, and says when the news
  leans against the trade. The mood is recorded with each trade, so `/review` can show whether
  trading against the news loses money.

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
   - 3 bot trades are already open, or 2 in the same direction (`max_same_direction`: BTC, ETH
     and SOL usually move together, so a third short is mostly the same bet again)
   - today's losses have reached `max_daily_loss_pct`. This counts closed trades as Bybit booked
     them (real fills and fees) plus open bot trades that are currently losing.
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
liquidation price. Position size comes from `risk_pct` of your capital (or of your Bybit equity,
if that is lower after losses), so leverage changes only the margin
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
  A TP counts only once price trades *through* it (a touch may not fill a limit order). If a candle
  hits a TP and then closes back past the stop it just moved, that stop counts as hit.
- **Position size:** suggested from `account_size × risk_pct ÷ stop distance`, with leverage
  capped at `max_leverage`.

**Tracking.** Every signal is followed on 1-minute candles. The scanner reports each TP, stop
or expiry and logs the result in R. State survives restarts in `logs/signals_state.json`; the
full history is in `logs/signals.csv`.

**Telegram alerts (optional).**
1. Create a bot with @BotFather.
2. Set the environment variables `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Set `telegram = true` in the config.

### Strategy research (Nov 2026)

**Test setup:**
- **Data:** one year of 1h candles on 9 coins: BTC, ETH and SOL, plus BNB, XRP, DOGE, ADA, AVAX
  and LINK as extra out-of-sample checks.
- **Fees:** Bybit's real fees.
- **Acceptance rule:** a change was kept only if it improved both the first 60% of the year and the
  last 40%, and helped on most coins.

| Min strength 60 | Trades | Avg R | First 60% | Last 40% | Coins positive | Worst drawdown |
| --- | --- | --- | --- | --- | --- | --- |
| Old rules, market entry | 1,395 | −0.033 | −0.018 | −0.056 | 4/9 | 105R |
| + 4 filters | 1,008 | −0.015 | −0.007 | −0.029 | 5/9 | 63R |
| **+ limit entry (now the default)** | 988 | **+0.013** | +0.024 | −0.008 | 5/9 | **52R** |
| …BTC, ETH, SOL only | 353 | **+0.029** | +0.027 | **+0.034** | 2/3 | 22R |

**What changed and why:**
- **Fees decided everything.** Before fees the rules make +0.05R per trade. A 0.1% round trip
  costs about 0.075R, because the median stop is only 1.3% away.
- **Limit entries** (`trading.limit_entry`) use the maker fee and enter at the signal price,
  which is worth about +0.03R per trade. 98% of them filled within one candle.
- **The 4 filters** (`[rules]`, all on):
  - skip trades whose real stop is wider than the cap
  - require a clear 4h trend (EMA50 at least 1% from EMA200)
  - altcoins only trade in Bitcoin's direction
  - skip volume spikes over 2.5×

  Together they halve the losses and the drawdown.

**Rejected,** because they made things worse in at least one half: 4h chart, shorter or longer
time stops, earlier breakeven, tighter or wider targets, ADX rising, session filters, min
strength 70+.

**Honest verdict:** the edge is thin. It is about breakeven-to-small-positive, and it is positive
on your 3 coins in both halves. Most of the improvement is cutting costs and bad trades. Keep
min strength at **60**: with the new rules, 70 is worse. Run `python signals.py backtest` and
`/review` regularly, and judge the bot on its live record before using real money.

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

Before going live, also confirm with Flint that `OffsetSpec.price_offset` is **cumulative** (each
level's distance from the previous level), as this bot assumes. If it is the distance from the
fair price instead, the deeper levels would sit *inside* level 0.

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
