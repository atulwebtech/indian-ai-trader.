# Indian AI Trader

A **paper-mode** assistant for **NIFTY 50** and **BANK NIFTY** (NSE, 09:15-15:30 IST). It shows a
full-day map of the market: bullish / bearish / sideways scenarios, numeric target zones, support and
resistance, possible reversal zones, and what would cancel each view. It can send selected alerts to
Telegram and show everything on a local web page.

> **Read this first**
> - It **never places orders**. Order methods are blocked in code and checked by a test.
> - **No target is guaranteed.** Targets are estimates from past data.
> - Our own 2024-2026 backtests found **no proven trading edge** in the bias or the BUY/SELL signals
>   (see "What the tests showed"). Use this as a market map and a learning tool, not as a money machine.
> - Until the Groww API is connected, every message says **PAPER MODE** and the data is delayed.

*Hinglish summary:* Yeh tool asli trade kabhi nahi karta. Yeh sirf din ka nakshaa (levels, targets, scenarios) dikhata hai.
Backtest mein BUY/SELL signals se koi pakka fayda nahi mila, isliye isse asli paise ke liye mat use kijiye.

---

## 1. First-time setup

**Linux / Mac** (open the VS Code terminal, one line at a time):
```
cd /var/www/html/trading/indian-ai-trader
./setup.sh
```
**Windows:** double-click `setup.bat` (needs Python 3.10+ from https://www.python.org/downloads/ and Git from https://git-scm.com/downloads/win).
The `.bat` files are written but have not been run on a Windows machine by the author.

`setup` creates a private Python environment in `.venv`, installs the libraries and creates `.env` from `.env.example`.

## 2. Everyday commands

Run them as `./run.sh <command>` (Windows: `run.bat <command>`).

| Command | What it does |
|---|---|
| `forecast` | Forecast for NIFTY and BANKNIFTY using delayed real candles (add `--data mock` for fake test data) |
| `signal` | BUY / SELL / WAIT / NO TRADE check on the latest candle |
| `telegram-test` | Sends one test message to your Telegram |
| `telegram` | Runs the Telegram bot. Keep the window open; stop with Ctrl+C. **Easier: `./bot.sh`** (same thing, plus it stops the computer from sleeping) |
| `dashboard` | Local page at http://127.0.0.1:8050 (only your computer can open it) |
| `paper-report` | Results of simulated signals collected so far |
| `backtest` | Long historical validation (about 15 minutes) -> `reports/` |
| `calendar` | Downloads the official NSE holiday list |
| `groww-check` | Read-only Groww connection test (only after you buy API access) |

## 3. Telegram

1. In Telegram open **@BotFather** (https://t.me/BotFather), send `/newbot`, follow the questions, copy the token.
2. Open the bot you created and press **Start** (this lets it message you).
3. Put the token in `.env` as `TELEGRAM_BOT_TOKEN=...` and your chat id as `TELEGRAM_CHAT_ID=...`. **Never paste these anywhere else.**
4. `./run.sh telegram-test` must show "Test message sent". Then `./run.sh telegram`.

Messages are in **Hinglish** by default (`TELEGRAM_LANG=hi` in `.env`); send `/lang en` in Telegram for English, `/lang hi` to switch back (the choice is remembered).
A BUY signal is shown as a **CALL (CE)** idea and a SELL signal as a **PUT (PE)** idea; the strike, expiry and premium are never chosen by the system.
Bot commands: `/start /help /status /forecast /nifty /banknifty /signals /report /news /context /explain` (`/explain hi` for Hinglish).
It answers **only your chat id**; other chats are ignored.
Automatic messages (only while the bot is running and the computer is on): 09:00 pre-market forecast, about 09:35
opening-range update, during the day only confirmed signals / reversals / invalidations / signal status changes,
15:20 quick summary, about 15:50 the full END-OF-DAY REPORT (see below). Nothing is sent on NSE holidays, and alerts pause with a "DATA STALE" notice if data is old.

## 4. What is in each folder

| Folder | Purpose |
|---|---|
| `config` | Settings (secrets only from `.env`) |
| `market_data` | Candle adapters (mock, Yahoo historical, Groww), validation, cache, NSE calendar |
| `groww_integration` | Groww login, instrument master, history, live index feed (read-only) |
| `indicators` | EMA, RSI, MACD, ADX, ATR, pivots, opening range, support/resistance, price action |
| `news` | Free RSS headlines, headline classifier, global market context |
| `forecasting` | Daily forecast engine, target ladder, base rates, plain-language explanations |
| `reversal_detection` | Potential / confirmed reversal, false breakout, continuation |
| `strategies` | Signal engine (BUY / SELL / WAIT / NO TRADE) |
| `risk_management` | Signal gate: window, cooldown, duplicates, daily caps and loss limit |
| `backtesting` | Out-of-sample forecast test, signal test, costs, metrics |
| `paper_trading` | Ledger of simulated signals and results |
| `telegram_bot` | Client, messages, scheduler, commands |
| `dashboard` | Local web page |
| `reports`, `logs`, `data` | Output (not committed to Git) |
| `tests` | Automated tests (`.venv/bin/python -m pytest`) |

## 5. How a forecast is made (no AI service, no cost)

1. Candles are validated (duplicates, order, bad prices, missing candles; nothing is ever invented or filled).
2. Indicators and key levels are measured (previous-day high/low, pivots, opening range, swing support/resistance, ATR).
3. Target zones are the nearest measured levels inside the expected range; if too few exist, a clearly labelled "ATR projection" fills in.
4. Each target shows how often past days moved that far from the open ("past days moved this far"). This is a history count, **not** today's probability.
5. Bias is a simple vote of 4-6 checks; each scenario has a trigger and an invalidation level.

The quality score on signals is a **checklist score, not a probability of winning**.

## 6. What the tests showed (Oct 2026 run, details in `reports/backtest_*.md`)

* **Targets:** about 690 trading days each for NIFTY and BANKNIFTY. The share of days that reached a target was within roughly 10 points
  of the stated history rate (for example bull T1: stated 54-59%, reached 63-65%). Useful as a range map.
* **Bias:** BULLISH days closed above the open only 43-48% of the time (normal days: 47-50%). Trading the bias open-to-close,
  after an assumed 0.04% cost, lost about 0.09% per trade (statistically negative). This held in every volatility/trend regime.
* **Signals (15m, about 50 sessions, 34 trades):** net result negative after costs; too few trades to prove anything.
* The cost figure is an **assumption**; check your own broker charges and taxes.
* Index spot has no volume, and Yahoo data is delayed and unofficial. 5-minute history is only about 60 days on Yahoo.

## 6a. End-of-day report and accuracy diary

About 15:50 (once the last candle is published; `/report` any time) the bot sends, for NIFTY and BANKNIFTY:
open/high/low/close; whether the pre-market direction call was right; which target zones were reached (next to what history said);
the opening-range outcome; the day's paper signals and their result; the **3 biggest moves of the day found in hindsight** and whether a signal caught each;
and a running **"How accurate has the tool been?"** section (direction hit rate with its margin of error, level hit rates vs history, signal results in R).
Totals are saved in `data/scorecard.json`; only finished days are counted. Under 20 days the report says the numbers are a diary, not a verdict.
Live signals are limited to **3 per day across both indices** (plus 45-minute cooldown). The tool cannot know in advance which signal will turn out best;
the hindsight list shows what was possible, not what was knowable.

## 6b. News and market context (free, no API key)

The bot also reads public RSS feeds (Economic Times, Livemint, Business Standard, Google News) and Yahoo daily data for
India VIX, S&P 500, Nasdaq, Nikkei, Hang Seng, Brent crude and USD/INR.

* `/news` - latest market-level headlines. `/context` - global markets, VIX, crude, rupee and headlines.
* 09:05 each trading day: a MARKET CONTEXT message.
* 08:52-15:32, every 5 minutes: a **NEWS ALERT** is sent only for a fresh, market-wide *event* (war, attack, sanctions, tariffs,
  rate decisions, default...) or an extreme fall reported for an Indian index. It must be India-specific or confirmed by a second headline.
  Routine "Sensex rallies" reports never alert. Limits: 1 alert per 20 minutes, 6 per day, same story not repeated for 3 hours.
* An alert shows the headlines with links, how NIFTY/BANKNIFTY have actually moved today (is price confirming the news?), and the forecast's
  levels for that direction. **It never creates a BUY/SELL.** Use the normal signal alerts and your own judgement.
* Headlines are read with plain keywords, so mistakes happen. There is no free history of past headlines, so this part
  **could not be backtested**. Context numbers do not change the forecast.
* More feeds: add `NEWS_EXTRA_FEEDS=url1,url2` to `.env`. Paid or key-based news APIs are not connected.

## 7. Connecting Groww later (read-only)

The Groww Trading API is not purchased yet. When you have it:
1. Put `GROWW_API_KEY` and `GROWW_API_SECRET` in `.env` (or `GROWW_TOTP_SECRET`, which needs `pip install pyotp`).
   With the secret method Groww requires approving the key daily on its API Keys page.
2. Run `./run.sh groww-check`. It logs in, loads the instrument master and checks NIFTY and BANKNIFTY (`NSE-NIFTY`, `NSE-BANKNIFTY`, type IDX).
3. Set `DATA_MODE=LIVE` in `.env`. Messages will then read "LIVE DATA - PAPER TRADING ONLY".

Only documented SDK methods are used (verified against the installed `growwapi` package). The live index feed's payload format and
reconnection behaviour are **not documented by Groww**, so that part is built defensively and has only been tested with fake data;
test it with a real account before relying on it. Groww's candle history goes back further than Yahoo's, which makes a proper
6-12 month 5-minute backtest possible.

## 8. Optional AI explanations

`/explain` and `forecasting/explain.py` build explanations from the engine's own numbers; they cost nothing. An external model (for example the
Claude API) is **not** connected. It would need a paid API key and charges per use; ask before adding it.

## 9. Safety

* Secrets live only in `.env` (ignored by Git). Logs and Telegram error messages never contain the token.
* All Groww calls go through a wrapper that allows read-only methods only.
* The dashboard listens on 127.0.0.1 only and has no buttons or forms.
* Signals obey: entry window 09:30-14:45, 45-minute cooldown, max 3 per day per index, no duplicates, stop for the day after -3R or 2 losses in a row.
* NSE holidays come from NSE's own list (`./run.sh calendar`). If the list does not cover the current year the dashboard says UNVERIFIED.

## 11. Option-buying signal engine (paper only, live Groww data)

`./run.sh option-signal --live` evaluates NIFTY and BANKNIFTY once on **live Groww data** and prints one decision per index:
**BUY CALL / BUY PUT / WAIT FOR CONFIRMATION / NO TRADE** (EXIT / INVALIDATED comes from the signal lifecycle, `strategies/option_engine.advance_signal`).
It needs the explicit `--live` flag and never changes `DATA_MODE` in `.env`. Without live data (stale, disconnected, market closed) it prints DATA UNAVAILABLE and gives no signal.

* Data: one REST history load (1-minute index candles + futures volume, about 20 sessions), WebSocket ticks for freshness, `get_option_chain`, `get_quote` (real bid/ask, previous OI) per evaluated contract. 3/5/15/30m candles are built from the 1m candles, using closed candles only.
* Levels: previous-day high/low/close, opening range, swings, CPR, pivots R1-R3/S1-S3, repeated support/resistance zones (20 sessions), option-chain OI walls.
* Regime first (trending / breakout / breakdown / reversal / sideways / choppy), then only the setups allowed for that regime. Choppy = NO TRADE.
* Underlying levels and option **premium** levels are separate. Premium stop/targets are **estimates** from delta and gamma (theta drag on targets); IV change is not modelled.
* Position size = allowed risk / (premium risk x lot size). The lot size comes from the instrument master. No sensible size = NO TRADE.
* Change in OI is shown only from `get_quote` (exchange previous OI) or from our own snapshots (`data/oi_snapshots`); otherwise UNAVAILABLE. Option chain has no bid/ask, so the spread comes from the quote.
* Every decision and its reasons is appended to `data/option_decisions.jsonl`. Limits and thresholds are the `OPT_*` variables in `.env.example`.
### 11a. Continuous monitor, Telegram alerts, paper ledger

`./run.sh option-monitor --live` (needs the explicit `--live`; paper signals only, never orders) polls every ~2 seconds:
* data health (LIVE / STALE / DISCONNECTED) with an alert when the feed is lost and when it is back;
* one evaluation per new closed 1-minute candle and instrument (no duplicate processing); one open position per instrument;
* open paper signals are tracked on the option **premium** (quote): ENTRY TRIGGERED, TARGET 1 (stop moves to entry), TARGET 2/3, STOP LOSS, INVALIDATED, EXPIRED;
* after a stop loss that instrument is blocked for 45 minutes (no automatic re-entry);
* Telegram messages (setup detected, signal, entry, targets, stop, invalidation, data lost/restored) with the signal ID. Every alert has a persistent key, so restarts and repeated evaluations never send the same message twice;
* `data/option_ledger.json` (signals, outcomes, net P&L after costs) and `data/option_state.json` (dashboard feed). A signal counts as a trade only if its entry triggered; expired/invalidated-before-entry signals are stored but never counted.
Costs (`paper_trading/costs.py`) are assumptions: brokerage 20/order, STT 0.1% on sell, exchange 0.03503%, SEBI, stamp, GST 18%, 0.3% slippage per leg. Check your broker's contract note.

### 11b. Option dashboard

`./run.sh dashboard` then open http://127.0.0.1:8050/options (this computer only, read-only, no order controls). It reads `data/option_state.json`,
so the monitor must be running; otherwise it says MONITOR NOT RUNNING, and old data is marked MONITOR STOPPED. Panels: live prices and sparkline, direction and
timeframe alignment (1h/30m/15m/5m/3m/1m), key levels, final decision with **WHY THIS TRADE?** / **WHY NO TRADE?**, CALL vs PUT comparison, previous day vs today,
last 5 sessions, indicators, option chain with ATM highlight, paper P&L, win rate, profit factor, drawdown, risk limits, signal history.
If the feed drops, the last live analysis stays on screen marked "OLD ANALYSIS".

### 11c. Backtest (modelled premiums)

`./run.sh option-backtest --months 6 [--iv 15]` replays the engine over Groww 1-minute index history (as far back as Groww returns) and writes `reports/option_backtest_<date>.md`
with costs, walk-forward split, rolling blocks, results by setup/regime/instrument and a score-threshold sensitivity table.
**Option premiums are modelled** (Black-Scholes, fixed IV, 3 days to expiry, 0.6% spread) because Groww has no history for expired options; OI, option volume, PCR and liquidity are not testable.
The report ends with an evidence verdict that never says "profitable" without enough trades both in-sample and out-of-sample.
Thresholds: `OPT_MIN_OI_LOTS`, `OPT_MAX_THETA_PCT` etc. in `.env.example` (liquidity is measured in lots; theta filters use the real Groww values).
**First backtest result (6 Nov 2026, 135 sessions, NIFTY+BANKNIFTY, modelled premiums, costs included):** 198 trades, win rate 15.7%, profit factor 0.29,
negative in-sample, out-of-sample, in every block and at every score threshold (score >= 80 was worse, not better). A **no-edge random-walk control run gave almost the same
numbers** (win rate 17.1%, profit factor 0.26, 32 of 35 trades stopped out), so (a) the signals show no detectable edge over noise and (b) these execution assumptions
(tight stops, spread, slippage, costs, theta, stop-before-target on 1-minute bars) are harsh enough to lose money without an edge. Verdict: **NOT PROVEN, do not trade real money from these signals.**
Next steps would be wider stops / fewer trades tested with walk-forward, but beware of fitting the past.
* **Still not done:** 1-minute candles are polled over REST each minute (verified fresh) rather than built from ticks; market breadth is not available from Groww; there is **no evidence yet** that the signals are profitable. The score is a checklist rating, not a probability.

## 10. Troubleshooting

| Problem | What to do |
|---|---|
| No Telegram message | Press Start in the bot chat, check token / chat id in `.env`, run `./run.sh telegram-test` |
| Bot does not answer | Is `./run.sh telegram` still running? Look at `logs/app.log` |
| "DATA STALE" | The free data feed is late; wait for the next candle |
| "UNVERIFIED" calendar | Run `./run.sh calendar` |
| Anything else | `.venv/bin/python -m pytest` and read the failing test name |

Design ideas (opening range, R-multiples, risk gate, Monte-Carlo replay) were inspired by the MIT-licensed
`Jakeschincariol/day-trader-skill`, kept untouched in `../reference/`. No US-specific code is reused.
