# Autonomous AI Trader (Bybit BTCUSDT perpetual + local Ollama AI)

A trading system for an Apple Silicon Mac. A **local AI model (Ollama)** looks at market data that Python has already
calculated and *proposes* LONG, SHORT or WAIT. A **deterministic risk engine** then checks every proposal and can reject it.
Only a separate **execution layer** talks to Bybit. The AI never sees your API keys and cannot place, change or cancel anything.

> **This software does not promise profit.** Trading leveraged crypto can lose all of your money quickly. Nothing here is
> financial advice. A backtest or a good week on testnet proves nothing about the future. Start in PAPER, then TESTNET,
> and only risk money you can afford to lose.

---------------------------------------------------------------------------------------------------

## 1. Quick start (first time)

Open **Terminal** (Cmd+Space, type *Terminal*) and run:

```
cd ~/Downloads/autonomous_ai_trader        # or wherever you unzipped it
./setup.sh
./start.sh
```

Then open **http://127.0.0.1:8080** in your browser.

* `./setup.sh` **checks everything** (Apple tools, Homebrew, Python, packages, Ollama, the AI model) and, for anything
  missing, **asks you** whether to install it. Answer `y` to each. It tells you the size before downloading the AI model.
  Want to just look first? `./setup.sh --check-only`. Want to accept every offer automatically? `./setup.sh --yes`.
* If macOS says "cannot be opened because it is from an unidentified developer" for a script, run
  `chmod +x setup.sh start.sh stop.sh status.sh scripts/*.sh` and try again.
* The first start is **PAPER mode**: simulated trades with live Bybit prices. No account, no keys, no real or testnet orders.

Daily commands:

| What | Command |
|---|---|
| Start | `./start.sh` |
| Is it running? what mode? | `./status.sh` |
| Stop (open positions keep their exchange-side stop loss) | `./stop.sh` |
| **Emergency: close everything and halt** | `./stop.sh --emergency-close` or the red dashboard button |
| Full self-check | `.venv/bin/python -m bot.diagnostics` |
| Backtest | `.venv/bin/python -m bot.backtest --days 30` |
| Run the unit tests | `.venv/bin/python -m unittest discover -s tests -t . -v` |

## 2. What you need

* Apple Silicon Mac (M1/M2/M3/M4), macOS 12+. Intel Macs work but local AI is slow.
* Internet connection. 5-25 GB free disk space (the AI model is 2.6-19 GB depending on your RAM).
* Everything else is installed for you by `./setup.sh` (with your permission).

## 3. How it decides (architecture)

```
Bybit prices/candles --> deterministic Python features (ATR, structure, FVG, order blocks, liquidity, sweeps)
        |                          |
        |                 setup pre-filter: no setup => WAIT, the AI is not even asked
        |                          v
        |              Ollama local model  --> strict JSON {LONG|SHORT|WAIT, SL, TP, confidence...}
        |                          v   (malformed => retried once => rejected)
        |              RISK ENGINE (final authority, can reject anything, AI cannot override)
        |                          v
        |              EXECUTOR: duplicate checks -> order -> confirm fill -> confirm stop loss ->
        |                        (no stop? close the position, halt) -> trailing stop / partial TP
        +--> dashboard, SQLite database, logs, optional Telegram alerts
```

Source layout: `bot/strategy` (indicators and zones), `bot/ai` (Ollama client, prompt, JSON schema), `bot/risk` (limits,
sizing, risk engine), `bot/exchange` (Bybit REST client, WebSocket, paper exchange), `bot/execution` (executor, reconciliation,
emergency close, duplicate protection), `bot/backtest`, `dashboard/`, `tests/`.

The AI can only read through five whitelisted read-only tools (`get_market_snapshot`, `get_current_position`, `get_open_orders`,
`get_account_summary`, `get_strategy_analysis`; off by default) and can only answer with a decision. There is no tool that can
execute an API call, change a setting, remove a stop, change leverage or withdraw.

## 4. The AI model (chosen from your RAM)

`./setup.sh` detects your chip, unified memory, CPU/GPU cores and macOS version and prints, for example:

```
Detected hardware:  Chip: Apple M3 Pro | Memory: 36 GB | CPU: 12 cores | GPU: 18 cores
Selected AI model: qwen3:30b   Estimated model size: ~19 GB download
Reason for selection: 36 GB unified memory; leaves headroom for macOS, the bot and the dashboard
```

| Unified memory | Model | Download |
|---|---|---|
| 8 GB | qwen3:4b | ~2.6 GB |
| 16 GB | qwen3:14b | ~9.3 GB |
| 24 GB and up | qwen3:30b (mixture-of-experts, fast on Apple Silicon) | ~19 GB |

A model larger than 65% of your memory is never selected. The model runs only on your Mac through Ollama (the local
address `127.0.0.1`); the config refuses any remote AI address. To choose another model: put `OLLAMA_MODEL=qwen3:8b`
in `.env` (then `ollama pull qwen3:8b`). The model is asked at most once per closed 5-minute candle when a live trigger is at a zone, and at most once per 15 minutes per coin for a plan.

### Planning a trade before price arrives

If the 1-hour trend is clear and a fresh zone sits on the pullback side, up to about 6 ATR away, the mapper hands that to the model as a **PLAN** instead of waiting for price to already be at the zone. The model answers with a limit at the zone, how long the order may wait (5 to 240 minutes), and a level where the idea is dead if price trades there first. That level defaults to the target.

The limit rests on the exchange with its stop and target already attached, survives a restart, and is cancelled when the clock runs out or the abandon level is hit. Per trade, the model may also choose fixed, partial or trailing take-profit, and it may use 25% to 100% of the configured risk. It cannot take more risk, widen or remove a stop, or change account limits. The usual stop-distance, reward and daily-loss checks still apply. On the dashboard the plan is dashed lines (entry, stop, target, abandon) and the open-trade tile reads `Plan long, entry X, N min left`.

## 5. The three modes

The banner on the dashboard, in the terminal and in `./status.sh` always shows the mode.

| Mode | Prices | Orders | Needs keys |
|---|---|---|---|
| **PAPER MODE** (default) | live Bybit public data | simulated here (fees + slippage modelled), **never sent to Bybit** | no |
| **TESTNET MODE** | Bybit testnet | real orders on the testnet exchange (fake money) | testnet keys |
| **LIVE MODE** | Bybit live | real orders, real money | live keys |

Mode is set by `mode:` in `config/config.yaml`. `.env` must agree (`BYBIT_TESTNET=true` for TESTNET, `false` for LIVE) or the bot
refuses to start, so testnet and live can never be mixed. TESTNET uses `BYBIT_TESTNET_API_KEY/SECRET`; LIVE uses **only**
`BYBIT_API_KEY/SECRET`.

## 6. Step by step: Testnet

1. Create a testnet account at https://testnet.bybit.com and request test USDT in the Assets section.
2. Account -> API -> **Create new key**, *System-generated*, permission **Contract - Orders & Positions** only. **Do not tick Wallet/Withdrawal or anything else.**
3. Put the key in `.env` (open with `open -e .env`):
   ```
   BYBIT_TESTNET_API_KEY=your-testnet-key
   BYBIT_TESTNET_API_SECRET=your-testnet-secret
   BYBIT_TESTNET=true
   ```
4. In `config/config.yaml` change `mode: PAPER` to `mode: TESTNET`.
5. Check it: `.venv/bin/python -m bot.diagnostics --testnet-orders` - this places one far-from-market order on testnet, checks it, cancels it and reads positions.
6. To let it place testnet orders on its own, set in `.env`: `TRADING_ENABLED=true` and `AUTONOMOUS_MODE=true`. Run `./stop.sh` then `./start.sh`.
   With either switch `false`, testnet mode only monitors.

## 7. Going live (only after weeks of good paper/testnet behaviour)

Live orders require **all** of: `mode: LIVE`, `BYBIT_TESTNET=false`, `TRADING_ENABLED=true`, `AUTONOMOUS_MODE=true`, and live keys. If any is missing nothing is sent;
`./status.sh` prints exactly which one:

```
[BLOCK] TRADING_ENABLED=false (needs true)
```

Recommended live key: a **dedicated** API key, permission *Contract - Orders & Positions* only, **no withdrawal**, restricted to your home IP address
(Bybit -> API -> IP restriction). At startup the bot reads the key's permissions and **refuses to trade** if the key can withdraw.
Start with a small account. The defaults risk 0.5% of equity per trade.

To stop live orders instantly: set `AUTONOMOUS_MODE=false` (or `TRADING_ENABLED=false`) in `.env` and run `./stop.sh && ./start.sh`,
or press **EMERGENCY CLOSE** (it also halts trading until you press Resume).

## 8. Risk controls (all in `config/config.yaml`, none can be changed by the AI)

| Setting | Default | Meaning |
|---|---|---|
| risk_per_trade_percent | 0.5 | max loss at the stop (fees and slippage included) as % of equity |
| max_daily_loss_percent | 2.0 | stop trading for the day |
| max_account_drawdown_percent | 10 | stop trading until you review it |
| max_daily_trades / max_open_positions | 10 / 1 | |
| min_risk_reward | 2.0 | real R:R computed by Python, not the AI's claim |
| max_leverage | 5 | position sizing is capped by it |
| max_consecutive_losses | 4 | stop after a losing streak (0 = off) |
| min/max_stop_atr_mult | 0.8 / 4.0 | blocks absurdly tight or wide stops |
| ai.min_confidence | 70 | below = rejected; 70-79 only with trend alignment + trigger + extra R:R; 80+ passes if all else passes |

Also enforced: stop loss and take profit must exist and be on the right side, spread limit, abnormal-volatility filter, stale-data
check, tick/step/min-quantity rounding, sufficient margin, API/WebSocket/Ollama health, and "state uncertain => no new trades".
Trailing stop: ATR x 0.9, starts at +1R, **only ever tightens**. Position size is calculated from risk % and stop distance, never "x% of the account".
If the stop loss cannot be confirmed on the exchange after entry, the position is closed immediately, an alarm is raised and trading is halted.

## 9. Dashboard

http://127.0.0.1:8080 (local to your Mac). Sections: System, Controls, Market, AI, Position, Risk, recent events and trades. Times in UK time.
Buttons: Pause, Resume, Emergency close, Test AI, Refresh connection. In TESTNET/LIVE mode, Resume and Emergency close require an explicit confirmation.
Web pages from other sites cannot press these buttons (custom header + local Host/Origin check).

## 10. Autonomous mode, restart behaviour, auto-start

* "Autonomous" means `AUTONOMOUS_MODE=true` (plus `TRADING_ENABLED=true`): approved trades are placed without asking you.
* Restart / crash / Mac reboot: on startup the bot reads **Bybit** (positions, open orders), compares with its database, repairs the database, restores
  today's counters, **never assumes there is no position**, resumes managing an existing position (stop, trailing), cancels unknown opening orders, and never
  opens a trade just because it was interrupted. An unexpected position is reported, blocks new trades, and gets a protective ATR stop (or is closed if that is impossible).
* An emergency halt survives restarts and needs an explicit **Resume**.
* Automatic start after a Mac restart (optional, off by default): `scripts/install_autostart.sh` (remove: `scripts/uninstall_autostart.sh`).
  LIVE trading will **not** auto-start unless `launchd.allow_live_autostart: true` in config.yaml.
* Your Mac must stay awake and online. `start.sh` uses `caffeinate`; closing the lid or losing internet stops monitoring. Your protection then is the stop loss that sits on Bybit's servers.

## 11. Logs and data

`logs/app.log`, `trading.log`, `errors.log`, `ai.log`, `risk.log` (auto-rotated; API keys and secrets are redacted). Database: `data/trader.db` (SQLite; every AI decision, rejection reason, order, execution, trade with entry/SL/TP/size/risk/R-multiple/exit reason).
Console output: `logs/console.log`.

## 12. Backtesting

`.venv/bin/python -m bot.backtest --days 30` downloads public BTCUSDT 5-minute history and runs the real strategy, risk and sizing code.
It reports trades, win rate, profit factor, max drawdown, average/total R, expectancy and losing streak, plus a list of assumptions. **The AI is not used in the backtest**
(a fixed rule replaces it), candles hide the real intrabar order (stops assumed first), and there is no funding or latency. Treat results as a sanity check, not a forecast.
`--synthetic 4000` runs on invented data for a demo only.

## 13. Telegram alerts (optional)

Create a bot with @BotFather, get your chat id, then in `.env`: `TELEGRAM_ENABLED=true`, `TELEGRAM_BOT_TOKEN=...`, `TELEGRAM_CHAT_ID=...`. Alerts: start/stop, trade opened/closed, SL/TP hit,
daily-loss limit, emergency stop, API/AI failure, unexpected position, system errors. Credentials are never sent.

## 14. Troubleshooting

| Symptom | Fix |
|---|---|
| `./setup.sh` says pip failed | check internet, re-run `./setup.sh` |
| `Ollama: server not reachable` | open the Ollama app, or run `ollama serve` in another Terminal tab |
| `AI model ... not downloaded` | `ollama pull <model>` (name shown by `./status.sh`) |
| Dashboard won't open | `./status.sh`; another program may use port 8080 - change `dashboard.port` |
| Bybit errors / region blocked | check VPN/internet; some regions cannot reach Bybit |
| `API key has WITHDRAWAL permission` | delete the key, create a new one without it |
| Bot says HALTED | read the reason on the dashboard; fix it; press Resume |
| `mode LIVE requires BYBIT_TESTNET=false` | .env and config.yaml disagree - fix one |
| Everything looks stuck | `.venv/bin/python -m bot.diagnostics` shows the failing part with a Fix line |

## 15. What has and has not been verified

Verified by the automated test-suite (`python -m unittest discover -s tests -t .`, ~290 tests): FVG/order-block/liquidity/structure/ATR calculations, position sizing and rounding,
risk engine and limits, AI JSON validation, duplicate-order protection, stop-loss protection and unprotected-position handling, trailing stop (never loosens), partial take-profit,
emergency close, restart reconciliation, engine behaviour end to end on the paper exchange, Bybit request signing/parsing/retry (against a simulated server), Ollama client against
a real local HTTP server, dashboard security, backtester accounting, installer scripts.

**Needs your Mac and your accounts** (cannot be done without them, run `.venv/bin/python -m bot.diagnostics`): a real Ollama model answering, live Bybit connectivity,
real WebSocket data (pybit), Testnet authentication/orders (`--testnet-orders`, needs your testnet key), real fills. `RUN_INTEGRATION=1 .venv/bin/python -m unittest tests.test_integration -v` runs these.
Until you have run them, treat Testnet and Live behaviour as unproven. Paper P/L is an approximation (5-second price sampling, modelled fills).

## October 2026 development patch

- Added a market-wide public ticker scan for linear USDT pairs, with minimum 24-hour turnover and maximum spread filters. Dashboard shows the ranked shortlist, funding, turnover, spread, and scan status.
- The shortlist is a liquidity/spread pre-screen only. It does **not** automatically switch the execution symbol or claim that a pair is a trade opportunity. Before autonomous symbol switching is enabled, position and open-order reconciliation must cover the whole account and reliably distinguish bot-owned orders from manual/other-bot activity.
- Risk-limit alert deduplication now keys on the risk-limit class, so a changing drawdown percentage does not create a new alert on each loop.
- Shipped config is set to PAPER, 1x maximum leverage, one position maximum, 2% daily loss limit, 5% account drawdown limit, Telegram disabled, and live autostart disabled. Review these values before any testnet or live use.
- The original archive omitted `.env.example` and `.gitignore`; safe templates have been added. Keep actual API keys in `.env`, never in the archive.
- Automated validation: 292 tests passed, 6 skipped. Tests do not validate live exchange behaviour or profitability.

### Remaining limitations

The scanner evaluates every eligible pair by default, which can increase public API requests and scan duration. News sentiment is a transparent keyword heuristic, not a validated predictive score. Token-specific DEX pool data is a market/on-chain proxy, not full chain-wide transaction analytics or contract-risk auditing. Symbol-only DEX matches can be ambiguous, so the dashboard flags that limitation. Validate all behaviour in PAPER/TESTNET before considering LIVE.

## Autonomous scanner and unmanaged-trade protection (2026-10-21 patch)

- The market scanner now evaluates a bounded shortlist of liquid USDT perpetuals with the configured multi-timeframe technical setup, not only turnover/spread ranking.
- When the active symbol has no position or open orders and the bot is not halted, it can switch to the highest-ranked eligible technical setup. AI decision validation and the existing risk engine still gate every entry.
- Any candidate with an existing exchange position or open order is excluded and left untouched.
- Reconciliation no longer cancels an unknown opening order. It flags it as unmanaged and blocks new entries instead.
- The engine only manages positions that match an open trade recorded in its database. Unknown/manual/other-bot positions are read-only.
- News and token DEX-market context are now available through `bot/news_intel.py`; data refresh is asynchronous and never independently authorises a trade.
- Validate in PAPER mode first. Do not reset a drawdown halt until you have checked the account and open orders directly at the exchange.

## Telegram commands and market intelligence

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in your local `.env` file to enable the command poller. Keep the chat ID private. The bot accepts commands only from that exact chat:

- `/status` - current mode, pair, risk state and scanner status
- `/pause` - block new entries while monitoring continues
- `/resume` - attempt to resume after checking exchange state
- `/reset` - explicitly rebase daily/account risk limits at verified current equity and clear the persisted risk halt
- `/help` - command list

`/reset` never closes, cancels, or modifies positions/orders. It changes the risk baseline, so use it only after checking the account. A risk breach will halt new entries and send one alert per limit class until reset.

The dashboard's News and network intelligence section reads four public crypto RSS feeds, calculates an explicitly unvalidated keyword-based headline sentiment score, and displays Bitcoin hash-rate/mempool metrics. During the asynchronous refresh it also queries DexScreener for the active token symbol and reports the best-liquidity exact-symbol DEX pool, liquidity, volume, recent buys/sells and price change where available. This is a token-specific DEX-market/on-chain proxy, not complete chain-wide analytics or contract verification; ticker symbols can be ambiguous. Missing feeds or token data never count as trade confirmation. Feed and DEX refresh runs asynchronously so slow public sources do not block the trading loop.

Start and test in PAPER mode first. Live trading still requires a separate review of exchange permissions, API limits, order behaviour and risk settings. No automated strategy can guarantee profitable trades.


## Autonomous pair-selection safety fix (21 October 2026)

When the market-wide scanner is supported, only the scanner-selected symbol may open a new position. If no candidate has a valid technical setup, the engine blocks new entries rather than trading the configured fallback symbol (for example XMRUSDT). The dashboard banner displays `WAITING — no eligible pair` in that state. Existing positions and orders are checked and left untouched; a busy active pair prevents switching and blocks new entries.

This change does not make live trading risk-free. Test in PAPER mode first and keep live execution paused until verified against your own exchange setup.

## Audit update (22 October 2026)

The scanner ranks the full Bybit linear-USDT ticker list by liquidity and spread, then technically evaluates every pair passing those filters by default (`scanner.max_candidates_to_evaluate: 0`). Set a positive value in `config/config.yaml` to deliberately cap technical evaluations if API rate limits or scan duration become a problem. Full evaluation uses multiple candle requests per eligible pair and can take longer or encounter exchange throttling on a large universe; monitor the scan status and do not use LIVE mode until tested. The scanner uses account-wide position/order snapshots when supported to avoid making private account queries for every candidate; if those snapshots fail, it blocks new entries rather than assuming the account is clear.

The dashboard AI panel now displays the model's entry type/price, stop loss, take profit, risk/reward, market regime and invalidation conditions. Risk-alert deduplication keys persist across restarts, and `/resume` refuses to clear a halt while a risk limit remains active; use `/reset` only after reviewing current account equity and risk. Scanner selection can move away from an unmanaged/manual/other-bot position or order without modifying it, while it stays on a position/order recorded as owned by this bot.

External intelligence now reads four public crypto RSS feeds, adds an unvalidated headline keyword score, and queries DexScreener for token-symbol-matched DEX pool liquidity, volume, price change and recent buy/sell counts. It still does **not** provide a validated news-impact model, a broad macroeconomic calendar, full chain-wide transaction analytics, or verified token-contract identity. These inputs are context only and never bypass technical/risk checks. No live Bybit account has been connected for this audit. Keep LIVE trading disabled until you have tested the updated version in PAPER mode and verified its state on your own machine.

## Multi-agent advisory review (NEXUS update)

The engine can run six lightweight specialist reviewers concurrently before the main Ollama decision:

- **BRAM / Scanner** checks the selected candidate symbol and price context.
- **ILSA / News & data** reports available external-intelligence status and heuristic headline sentiment.
- **HOLT / Technical review** summarises the deterministic setup and liquidity-sweep alignment.
- **KETT / Risk pre-check** flags missing price/ATR data, abnormal volatility and trend misalignment.
- **RIGO / Market regime** compares the 1H, 15M and 5M trend labels.
- **TESS / Execution check** checks pre-flight data completeness; it does not place orders.

These are parallel deterministic specialist tasks, not six separate language models. They reuse the current snapshot and local intelligence data, so they do not require extra Ollama downloads or API keys. Their report is passed to the existing Ollama decision as additional context and displayed in the dashboard. All findings are observable summaries and flags, not hidden model chain-of-thought. The risk pre-check is advisory only: the existing deterministic RiskEngine remains the final gate and can reject any AI signal. Agent output cannot place orders or change risk settings.

The dashboard now uses a FOMO × GROK-inspired command floor: six clickable agent workstations, an agent inspector, market display, account telemetry and activity log. Candle charts render only if candle history is present in the bot state; otherwise the panel says history is unavailable rather than drawing fabricated candles.

Settings in `config/config.yaml`:

```yaml
multi_agent:
  enabled: true
  max_workers: 6
```

Start in PAPER mode and compare paper-trading results before considering TESTNET or LIVE. The automated tests validate the software logic; they do not establish live Bybit performance or profitability.
