# Proposed `.env` changes for launch (2026-08-22)

Nothing here has been applied. `.env` hot-reloads into the running bot, so apply these
deliberately, one group at a time, and watch the next main-loop iteration in `bot.log`.

## Group A — pin defaults that are currently implicit (safe, no behaviour change)

```
RUNNER_MAX_AGE_HOURS=120
LOG_MAX_MB=10
LOG_BACKUP_COUNT=4
STALE_SWEEP_ALERT_N=3
SHADOW_ANALYZE_HOURS=6
```

`SHADOW_ANALYZE_HOURS=6` is the one real change in this group: it runs the outcome
backfill four times a day instead of once, so shadow rows reach a terminal outcome
before Gate.io's ~34-day candle window closes (29% of rows are currently lost to this).

## Group B — exposure caps (required before live; HEDGE_MODE bypasses MAX_OPEN_POSITIONS)

```
MAX_OPEN_PER_SOURCE=3
SVEEZY_ALERTS__MAX_OPEN=4
RSI_EXTREME__MAX_OPEN=6      # currently 10
SOUL_ALERTS__MAX_OPEN=3
```

## Group C — source set for the first live month (report-only recommendations, your call)

```
ORACLEALGO__ENABLED=false        # -28.3R / n=100 shadow, never in profit; still shadow-logged
SVEEZY_ALERTS__RISK_PCT=0.015    # +16.1R n=34, max DD $21
SOUL_ALERTS__RISK_PCT=0.005      # max DD $78; re-evaluate at 30 post-attribution-fix trades
CALEB_ALERTS__ENABLED=false      # n=6, -$18; re-enable once 20 paper trades exist
```

Leave `RSI_CONFIRM_ENABLED=false`, `RSI_FIRST_ALERT_ONLY=false`, `ORACLE_HTF_FALLBACK=false`,
`RSI_TP_PCTS=0.05,0.10` exactly as they are - all four are validated by the final report.

## Group D — first 30 live trades only

```
RISK_PCT=0.005                   # step to 0.01 after 30 clean live fills
LEVERAGE_MAX=50                  # paper ladder has Sveezy at 115x; cap until live fills are verified
```

## Group E — the live flip itself (last, after preflight reads GO)

```
BLOFIN_BASE_URL=https://openapi.blofin.com
DRY_RUN=false
```

Requires: new BloFin account keys in `.env` (old live keys fail with 152408), BloFin
long/short position mode enabled by hand in the exchange UI (HEDGE_MODE needs it),
watchdog running, and `python preflight.py` printing `VERDICT: GO`.

## Keys that exist in `.env` but not `.env.example` (add to the example for hygiene)

`BOT_START_BALANCE`, `ENGINE_BASELINE_BALANCE`, `RSI_EXTREME__MAX_OPEN`, `USER_INVITE_CODE`,
`USER_START_BALANCE`.
