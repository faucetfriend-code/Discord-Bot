# Discord Signal Bot

Reads trade calls posted by Unity Academy analysts in Discord's notification inbox, parses them into structured signals, and executes perpetual futures orders on BloFin — all running locally on a Windows PC with Chrome as the browser automation layer.

## How it works

The bot watches Discord's notification inbox in **real time** (a MutationObserver injected over Chrome DevTools Protocol fires the instant a notification arrives), with a periodic inbox sweep as a safety net. Each message is parsed into a structured `Signal` by a layered parser — regex fast-paths, then a local Qwen LLM (via LM Studio), then a chart-vision read for image-only setups.

It runs **three strategies**, each tracked separately:

- **Analyst follow-trades** — copies the whitelisted Unity analysts' calls (text levels, or read off an attached chart).
- **RSI Extreme** — OracleAlgo overbought/oversold alerts traded mean-reversion.
- **OracleAlgo BTC** — 4H structure signals set a bias; 1H signals enter only when they agree with it.

Every source has its own **adaptive leverage** (starts at 75x, ratchets up on wins / down on losses within a 50–125x band based on its own track record) and **scale-out management** (partial closes at each TP target with the stop ratcheting to break-even then behind each prior TP). Positions are marked to market continuously and all PnL is tracked per strategy.

Orders execute on BloFin perpetual futures. A Flask dashboard at `http://localhost:5050` shows a per-source scoreboard, open positions with live PnL and scale-out progress, performance, and a log tail. **The default configuration trades on the BloFin demo account** (real orders, play money) so every code path — fills, amends, closes, reconciliation — is exercised exactly as it will be live; flipping to real money changes only `BLOFIN_BASE_URL`. See [Execution modes](#execution-modes).

## Prerequisites

- Windows PC
- Google Chrome installed at `C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`
- Python 3.11+
- [LM Studio](https://lmstudio.ai/) running locally with a Qwen model loaded
  - Default model: `qwen3.5-9b` at `http://127.0.0.1:1234/v1`
  - The bot still works without LM Studio — signals that fail all regex patterns will just be skipped
- BloFin account with API key (start with a demo account)
- Unity Academy Discord membership

## Setup

**1. Clone the repo and install dependencies**

```powershell
git clone <repo-url>
cd "Discord Bot"
pip install -r requirements.txt
```

**2. Create your `.env` file**

```powershell
cp .env.example .env
```

Open `.env` and fill in your BloFin API credentials and analyst whitelist (see [Configuration](#configuration) below).

**3. Load a model in LM Studio**

Open LM Studio, download a Qwen model (qwen3.5-9b recommended), and start the local server. The default URL (`http://127.0.0.1:1234/v1`) requires no changes in `.env`.

**4. Run the bot**

```bat
run_bot.bat
```

**5. First run only: log in to Discord**

A new Chrome window opens with a fresh profile (`C:\chrome-cdp-profile`). Log in to Discord in that window. Your session is saved — you will not need to do this again.

**6. Open the dashboard**

Navigate to `http://localhost:5050` in any browser.

## Configuration

All configuration lives in `.env`. Copy `.env.example` as your starting point.

| Variable | Default | Description |
|----------|---------|-------------|
| `BloFinAPI` | — | BloFin API key (required) |
| `Blofin_secret_key` | — | BloFin secret key (required) |
| `Passphrase` | — | BloFin API passphrase (required) |
| `BLOFIN_BASE_URL` | demo URL | `https://demo-trading-openapi.blofin.com` for DEMO, `https://openapi.blofin.com` for LIVE. **This is the only switch between demo and live.** |
| `PAPER_MODE` | `false` | `true` = PAPER: never call the exchange for orders; fills are simulated in-process (`DRYRUN-` order ids). |
| `DRY_RUN` | *(derived)* | **Deprecated.** Now derived: `mode != LIVE`. A value in `.env` is ignored and logged once at startup if it disagrees. |
| `BLOFIN_BROKER_ID` | *(unset)* | brokerId on LIVE order requests (Broker/MCP keys need one; `none` for Transaction keys). Never sent on demo. |
| `ATTACH_EXCHANGE_TP` | `false` | Also attach the signal TP to the exchange entry order (the bot runs its own TP ladder; SL is always attached). |
| `FORCE_IPV4` | `true` | Pin outbound HTTPS to IPv4 so IP-whitelisted keys are seen from the whitelisted address. |
| `LIVE_EPOCH_START` | *(blank)* | ISO-8601 moment the bot went live; the dashboard's "Live" view starts here, everything earlier is the Paper/Demo archive. |
| `ANALYST_WHITELIST` | 10 names | Comma-separated analyst display names exactly as they appear in Discord notifications (e.g. `Soul Alerts,Prestige Alerts`) |
| `RISK_PCT` | `0.01` | Fraction of account balance risked per trade (0.01 = 1%) |
| `MAX_OPEN_POSITIONS` | `3` | Maximum concurrent open positions |
| `POLL_INTERVAL` | `60` | Seconds between Discord inbox polls |
| `LOCAL_LLM_BASE_URL` | `http://127.0.0.1:1234/v1` | LM Studio API endpoint |
| `LOCAL_LLM_MODEL` | `qwen3.5-9b` | Model name as shown in LM Studio |

## Execution modes

The bot runs in exactly one of three modes, resolved at startup by `exec_mode.py` and printed as the first banner line (`EXEC MODE: ...`):

| Mode | Selected by | What happens |
|------|-------------|--------------|
| **PAPER** | `PAPER_MODE=true` | No exchange orders at all. Positions are simulated in-process (order id `DRYRUN-<symbol>-<ts>`); a limit entry rests in the local pending queue until price trades through it. Balance/prices are still read from the endpoint in `BLOFIN_BASE_URL`. |
| **DEMO** | `BLOFIN_BASE_URL` = demo endpoint (default) | **Real orders on the BloFin demo account** with the `Demo-*` keys, no brokerId. The full live code path: `set_leverage` -> `place_order` (real exchange orderId) -> amend/reduce/close on the exchange -> periodic reconciliation against exchange positions. The accounting (analyst ladder, virtual bots, R-ledger) settles from the same price path as live. |
| **LIVE** | `BLOFIN_BASE_URL=https://openapi.blofin.com` | Same code as DEMO on the live endpoint with the live keys and a brokerId. Real money. |

`DRY_RUN` is no longer a switch: it is derived (`dry_run = mode != LIVE`) and only tags records - `trades.dry_run`, the dashboard badge (PAPER/DEMO/LIVE) and the "Paper/Demo archive" view. PAPER and DEMO trades are both `dry_run=1`; only LIVE trades are `dry_run=0`. If `.env` still has a `DRY_RUN` that disagrees with the derived value the bot logs one warning and ignores it.

The mode is pinned for the life of the process. Editing `BLOFIN_BASE_URL` or `PAPER_MODE` in `.env` while the bot runs logs a "needs restart" warning and changes nothing - the BloFin client, keys and price stream were built for the startup endpoint.

Where DEMO still differs from LIVE: the demo account's fills, fees and funding are what BloFin's demo matching engine reports (thin books on some altcoins), a smaller instrument list, and no brokerId on orders. Run `python preflight.py --live` before going live to see which recent symbols exist on the live endpoint.

## Switching to live trading

1. Run `python preflight.py --live` (read-only) and wait for `VERDICT: GO`
2. Set `BLOFIN_BASE_URL=https://openapi.blofin.com` in `.env` (leave `PAPER_MODE` unset/false)
3. Re-run `run_bot.bat`

That is the only change. Test thoroughly on DEMO first - the demo endpoint is the default specifically so an accidental start never touches real funds.

## Signal routing

Every notification from a whitelisted analyst is classified into one of four types:

| Type | What happens |
|------|-------------|
| **NEW** | Validates the signal, calculates position size, and places an order on BloFin. If the symbol already has an open position, the signal is automatically re-routed as UPDATE instead of opening a duplicate. |
| **UPDATE** | Amends the stop-loss and/or take-profit on the existing open order via the BloFin amend API. |
| **CLOSE** | Market-closes the existing open position via the BloFin close API. |
| **NONE** | Logged and ignored. |

### Signal parser stages

1. **Update keyword regex** — detects phrases like "moving SL", "break even", "trailing stop", "partial TP hit". Extracts new SL/TP values if present.
2. **Close keyword regex** — detects "close", "closed", "exit", "exiting", "out of". Skips messages that also contain an entry price (those are new signals with a direction, not exits).
3. **New entry regex** — three patterns covering common formats:
   - `LONG BTC | Entry: 45000 | SL: 44000 | TP: 47000`
   - `BUY BTC-USDT @ 45000, SL 44000, TP 47000`
   - `⚡ ASTER/USDT LONG  Entry 0.67  SL 0.63  TP 0.70`
4. **LLM fallback** — sends the message to the local Qwen model via LM Studio. Returns a structured JSON classification with all signal fields.

When new signal formats appear that none of the regex patterns catch, add a new pattern to the `_NEW_PATTERNS` list at the top of `signal_parser.py`. Patterns are tried in order.

### Position sizing

Size is calculated as:

```
coins     = (balance * RISK_PCT) / |entry - sl|
contracts = floor(coins / contractValue / lotSize) * lotSize   # rejected if < minSize
```

Order size is in **contracts**, not coins. Contract specs (`contractValue`,
`lotSize`, `minSize`) are fetched live from BloFin's instruments endpoint and
cached per run — no hardcoded lot table. Symbols not listed on BloFin are
skipped automatically.

Signals are also rejected if:

- SL is on the wrong side of entry
- Risk/reward ratio is below 1.0 or above 20.0 (likely a bad parse)
- `MAX_OPEN_POSITIONS` is already reached
- The symbol already has an open position (automatically re-routed to UPDATE instead)

## Dashboard

The Flask dashboard at `http://localhost:5050` auto-refreshes every 15 seconds. It shows:

- Poll counter, last poll time, and how many notifications were found
- Chrome and Discord tab connection status
- All currently open positions (symbol, side, entry, SL, TP, size, order ID)
- The last 40 parsed signals with outcomes
- A live tail of `bot.log` (last 80 lines)

The `/api/status` endpoint returns the same data as JSON if you want to build additional tooling on top.

### Sharing the dashboard remotely (Cloudflare tunnel)

To let someone else (e.g. the Discord server owner) view your live dashboard,
expose it with a Cloudflare tunnel. The dashboard is **read-only** — it never
trades — but it does show your balance, positions and PnL, so **password-protect
it first**:

1. Set a password in `.env`: `DASHBOARD_PASSWORD=something_strong` (username is `DASHBOARD_USER`, default `viewer`). Restart the bot.
2. Install cloudflared if needed: `winget install --id Cloudflare.cloudflared`
3. Run `tunnel.bat` (with the bot already running). It prints a `https://<random>.trycloudflare.com` URL — share that and the username/password.

The quick-tunnel URL changes each run. For a **permanent URL with email-gated
access**, create a named tunnel under your own Cloudflare account and put
[Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)
in front of it (free for small teams) — then only specific emails can open it,
and you can skip the password.

## Module map (for contributors)

Each module has a header docstring stating its purpose, public interface, and
whether it's reusable standalone. Start there. The "Lift it out?" column flags
parts that drop cleanly into another project.

| File | Role | Lift it out? |
|------|------|--------------|
| `bot.py` | The orchestrator. Event loop, signal routing, the three strategy state machines, scale-out management, startup health check + reconciliation. | No — this is the glue |
| `signal_parser.py` | Turns a message dict into a `Signal`. Regex fast-paths → local LLM → chart vision. Owns the RSI/OracleAlgo/analyst format detection. | Partly (regex + `Signal` reusable) |
| `risk_manager.py` | Pure functions: validate a signal, size a position (contracts, not coins), pick a TP, assign chart levels by geometry. No I/O. | **Yes** — pure, no deps |
| `blofin_client.py` | Every BloFin exchange call, in one place. Balance, price, contract specs, leverage, all order types. Handles demo/live keys + endpoint. | Mostly (needs the `blofin` SDK) |
| `position_tracker.py` | All SQLite persistence: positions, adaptive leverage/PnL stats, seen-message dedupe, strategy state. | **Yes** — stdlib `sqlite3` only |
| `price_stream.py` | Live last-price cache from BloFin's public WebSocket, with a stale→None contract for REST fallback. | **Yes** — only needs `websocket-client` |
| `discord_reader.py` | Reads Discord's notification inbox over Chrome CDP (port 9222): one-shot poll + a real-time MutationObserver listener. | Partly (Discord/Chrome specific) |
| `dashboard.py` | Read-only Flask cockpit at `localhost:5050`. Reads `bot.db` + `bot.log`; never writes or trades. | Partly (schema-specific) |
| `logger.py` | Shared logger (local-timezone), plus signal logging to `bot.log`, `signals_log.csv`, and the `signals` table. | **Yes** |
| `preflight.py` | Standalone read-only go-live check: verifies live keys, balance, and which recent symbols are tradeable on live. Run `python preflight.py`. | n/a (a tool) |
| `run_bot.bat` | Kills existing Chrome, launches a fresh instance with CDP flags, then starts `bot.py`. | n/a |

### How the modules connect (data flow)

```
Discord inbox ──CDP──> discord_reader ──msg dict──> signal_parser ──Signal──> bot.py
                                                                                 │
                              ┌──────────────────────────────────────────────────┤
                              ▼                         ▼                         ▼
                        risk_manager            blofin_client            position_tracker
                       (size / validate)        (place / price)          (persist + PnL)
                              │                         ▲                         │
                              └─────────► bot.py ◄───────┘                         │
                                          │  price_stream feeds blofin_client      │
                                          ▼                                        ▼
                                     dashboard ◄────────── reads bot.db ───────────┘
```

## Runtime files (not in the repo)

These are generated at runtime and listed in `.gitignore`:

| File | Contents |
|------|----------|
| `.env` | Your credentials and configuration |
| `bot.db` | SQLite: open positions, seen message IDs, signal history |
| `bot.log` | Rotating log file |
| `signals_log.csv` | Flat CSV of every parsed signal |
| `C:\chrome-cdp-profile\` | Chrome user profile used by the bot (persists your Discord login) |

## Troubleshooting

### Chrome does not bind port 9222

**Symptom:** Bot prints `Cannot reach Chrome on port 9222 after 30s` and exits.

**Cause:** Chrome was already running before `run_bot.bat` was launched. Chrome only exposes the CDP port when it starts with the `--remote-debugging-port` flag — a running instance cannot be patched after the fact.

**Fix:** Always use `run_bot.bat` to start the bot. The bat file kills all existing Chrome processes first, then launches a fresh instance with the correct flags. Never open Chrome manually and then try to run the bot on top of it.

If the port still does not appear after running the bat:

```powershell
# Confirm Chrome started with the flag
netstat -ano | findstr :9222
```

If nothing comes back, check that Chrome is installed at `C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`. If your Chrome is in a different location, update the path in `run_bot.bat`.

### Discord inbox selector returns no notifications

**Symptom:** Every poll logs `No notifications found` even when there are unread notifications visible in the Discord window. The dashboard log tail shows a `WARNING` followed by a large HTML dump.

**Cause:** Discord periodically rotates its obfuscated CSS class names. The JS selectors in `discord_reader.py` use class-name substrings and `aria-label` attributes that occasionally change with Discord updates.

**Fix:** The HTML dump in the log is the raw `outerHTML` of whatever inbox panel the bot found (or the nearest candidate it could locate). Paste that HTML into the Discord Bot group chat — someone can identify the new class names and update the selectors in `_parse_notifications_js()`. The `aria-label="Inbox"` selector for the inbox button itself is more stable; it is usually the inner notification card selectors that break.
