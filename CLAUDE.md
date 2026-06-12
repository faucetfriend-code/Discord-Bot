# Discord Signal Bot

Reads trade calls from Unity Academy Discord notification inbox and executes on BloFin perpetual futures.

## Quick Start

```bat
run_bot.bat
```

This launches Chrome with CDP port 9222 then starts `bot.py`. Discord must be open in the Chrome tab before the bot starts.

## Manual Start

```powershell
# 1. Launch Edge with CDP (if not already running)
Start-Process "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" -ArgumentList "--remote-debugging-port=9222 --profile-directory=Default"

# 2. Navigate to Discord in Chrome manually, then:
python bot.py
```

## Config (.env)

| Var | Default | Notes |
|-----|---------|-------|
| `DRY_RUN` | `true` | Set `false` to actually execute orders |
| `BLOFIN_BASE_URL` | demo URL | Change to `https://openapi.blofin.com` for live |
| `POLL_INTERVAL` | `60` | Seconds between inbox polls |
| `RISK_PCT` | `0.01` | 1% of balance risked per trade |
| `MAX_OPEN_POSITIONS` | `3` | Max concurrent positions |
| `ANALYST_WHITELIST` | (10 names) | Comma-separated "X Alerts" names |
| `ANTHROPIC_API_KEY` | empty | Set for Claude fallback signal parser |

## File Overview

| File | Role |
|------|------|
| `bot.py` | Main loop — orchestrates everything |
| `discord_reader.py` | agent-browser CDP → inbox notification cards |
| `signal_parser.py` | Regex + Claude API → Signal dataclass |
| `blofin_client.py` | BloFin SDK wrapper (balance, place_order) |
| `risk_manager.py` | Validation, position sizing, lot-size rounding |
| `position_tracker.py` | SQLite: open positions + seen message IDs |
| `logger.py` | Rotating log to `bot.log` + `signals_log.csv` + `bot.db` |
| `accounts.py` | Virtual bots + users: each signal source is a "bot" with its own paper balance; users register at `/bots`, follow bots, paper portfolios mirror followed bots (R-multiple scaling) |

## Tuning Signal Parser

When new signal formats appear in dry-run logs, add regex patterns at the top of `signal_parser.py`. The patterns are tried in order; Claude API is the last fallback.

## Switching to Live Trading

**First run the preflight** (read-only — places no orders):

```bat
python preflight.py
```

It verifies your LIVE keys authenticate, shows the live balance, and lists which
recent signal symbols are tradeable on live vs demo. Only flip to live once it
reads `VERDICT: GO`.

Then:

1. Change `BLOFIN_BASE_URL` in `.env` to `https://openapi.blofin.com`
2. Set `DRY_RUN=false`
3. Restart bot

The bot auto-selects LIVE keys on the live endpoint (the `Demo-*` keys are only
used on the demo endpoint). The startup health check confirms auth + balance.

## Position Sizing & Contract Specs

Order `size` on BloFin is in **contracts**, not coins. Contract specs
(`contractValue`, `lotSize`, `minSize`) are fetched live from BloFin's
instruments endpoint via `blofin_client.get_contract_specs()` and cached once
per run. `risk_manager.calculate_size()`:

1. risk budget = balance × `RISK_PCT`
2. coins wanted = risk budget ÷ |entry − SL|
3. contracts = coins wanted ÷ `contractValue`, floored to `lotSize`
4. rejects (returns None) if below the exchange `minSize`, or if the symbol
   isn't listed on BloFin (e.g. some altcoins exist on demo but not live, or
   vice-versa)

No hardcoded lot table — the exchange is the source of truth, so newly-listed
symbols size correctly with no code change. The instrument cache is loaded once
at first use; restart the bot to pick up brand-new listings.
