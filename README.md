# Discord Signal Bot

Reads trade calls posted by Unity Academy analysts in Discord's notification inbox, parses them into structured signals, and executes perpetual futures orders on BloFin — all running locally on a Windows PC with Chrome as the browser automation layer.

## How it works

The bot polls the Discord notification inbox on a configurable interval. Each notification from a whitelisted analyst is passed through a two-stage signal parser: regex patterns for speed, a local Qwen LLM (via LM Studio) as fallback. Parsed signals are classified as NEW, UPDATE, CLOSE, or NONE, then routed to the appropriate BloFin API call. A Flask dashboard at `http://localhost:5050` shows live poll stats, open positions, signal history, and a log tail.

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
| `BLOFIN_BASE_URL` | demo URL | `https://demo-trading-openapi.blofin.com` for demo, `https://openapi.blofin.com` for live |
| `DRY_RUN` | `true` | No real orders while `true`. Change to `false` only when ready for live execution. |
| `ANALYST_WHITELIST` | 10 names | Comma-separated analyst display names exactly as they appear in Discord notifications (e.g. `Soul Alerts,Prestige Alerts`) |
| `RISK_PCT` | `0.01` | Fraction of account balance risked per trade (0.01 = 1%) |
| `MAX_OPEN_POSITIONS` | `3` | Maximum concurrent open positions |
| `POLL_INTERVAL` | `60` | Seconds between Discord inbox polls |
| `LOCAL_LLM_BASE_URL` | `http://127.0.0.1:1234/v1` | LM Studio API endpoint |
| `LOCAL_LLM_MODEL` | `qwen3.5-9b` | Model name as shown in LM Studio |

## Switching to live trading

1. Set `BLOFIN_BASE_URL=https://openapi.blofin.com` in `.env`
2. Set `DRY_RUN=false` in `.env`
3. Re-run `run_bot.bat`

Test thoroughly on demo first. The demo endpoint is the default specifically so an accidental start never touches real funds.

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
size = floor((balance * RISK_PCT) / |entry - sl|  /  lot_size) * lot_size
```

BloFin lot sizes are hardcoded in `risk_manager.LOT_SIZES`. If a symbol is missing from that dict it defaults to `1.0` — safe, but potentially wrong. Add new symbols to `LOT_SIZES` as you encounter them.

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

## File overview

| File | Role |
|------|------|
| `bot.py` | Main poll loop — orchestrates everything |
| `discord_reader.py` | Connects to Chrome via CDP (port 9222), clicks the inbox button, scrapes notification cards with JS |
| `signal_parser.py` | Two-stage parser: regex fast-path, then local Qwen LLM fallback. Returns a `Signal` dataclass. |
| `blofin_client.py` | BloFin SDK wrapper: `get_balance`, `place_order`, `amend_order`, `close_position_api` |
| `risk_manager.py` | Validates signals, calculates position size from `RISK_PCT`, rounds to BloFin lot sizes |
| `position_tracker.py` | SQLite (`bot.db`): tracks open positions and seen message IDs |
| `logger.py` | Rotating log to `bot.log`, appends to `signals_log.csv`, writes to the `signals` table in `bot.db` |
| `dashboard.py` | Flask web dashboard at `http://localhost:5050` |
| `run_bot.bat` | Kills existing Chrome, launches a fresh instance with CDP flags, waits 15s, starts `bot.py` |

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
