# Discord Signal Bot — Final Results Report

**Window:** 2026-05-23 → 2026-08-22 (paper trades) · 2026-06-06 → 2026-08-21 (shadow log)
**Mode the whole time:** `DRY_RUN=true`, `HEDGE_MODE=true`, BloFin demo endpoint. No live order has ever been placed.
**Code state:** commit `9b46cd8` (2026-08-08); bot running continuously since the 2026-08-20 restart.
**Data refreshed for this report:** `analyze_shadow.py` run 2026-08-22 — 597 → 610 rows, 374 → 390 terminal outcomes, 0 regressions.

---

## 1. Executive summary

1. **The paper book is profitable (+$202 / +21.1R on 140 trades, 47% WR), but one human analyst is the whole edge.** Sveezy Alerts is +$160 / +16.1R (n=34). Strip Sveezy out and the remaining 106 trades net +$42 / +5.0R — roughly break-even after the 51 stop-outs.
2. **Both algorithmic feeds lose money in shadow over the full sample.** RSI Extreme −18.5R (n=290, 46% WR), OracleAlgo −28.3R (n=100, 35% WR). In the paper book they look better (RSI +3.5R, Oracle +0.1R) only because the live gates blocked most of the losing rows.
3. **Under the *current* RSI ruleset the feed is marginally positive: +6.0R over 81 gate-passing signals since 2026-07-10 (+0.07R/trade, 54% WR).** That is the honest expectancy of what you are running today. It is positive, thin, and not yet statistically distinguishable from zero.
4. **Two gates are clearly validated, one is clearly wrong, one is neutral.** `sell_regime` blocked −18.5R (n=34) — keep. Oracle `no_bias` blocked −16.0R (n=58) — keep. The confirmation-on-turn entry is net-negative on paired rows (−12.7R, t=−2.56, post-cutoff) — correctly off. `macro_trend` blocked +0.5R (n=35) — it is not hurting, but it is not the source of the regime edge either.
5. **The system is not ready for live money yet — not because of the strategy, but because of operations:** preflight has never been run with a recorded GO, the watchdog has been dead since 08-07 (a 4-day CDP outage went unwatched 08-17 → 08-20), and the post-attribution-fix sample is 22 trades.

---

## 2. What is working well

### Infrastructure (keep as-is)
| Area | Evidence |
|---|---|
| Signal capture | 4,265 signals logged, 1,483 parsed, 452 produced orders. Zero Python tracebacks in 3 months of logs. |
| Shadow measurement | Every gate verdict is logged *including blocked branches*, so each gate's cost/benefit is measurable. 390 terminal what-if outcomes. |
| Outcome durability | Merge-not-clobber analyzer (`analyze_shadow.py`) held 374 → 390 terminal rows with 0 flips on this refresh. `recover_shadow.py` exists as a git-history backstop. |
| Attribution | Author-first + mention-stripped matching (2026-08-08) stopped cross-attribution (Sveezy posts booked to Soul, spam booked to Prestige). |
| Config | Per-source overrides (`<SOURCE>__<KEY>`) and `.env` hot-reload — tuning needs no restart. |
| Exit engine | TP ladder + BE ratchet works: `tp1_final` (+$441, n=21) and `tp2_final` (+$140, n=8) are 100% winners; `trail_stop` n=6 all positive. `time_stop` at 120h is net *positive* (+$103, 55% WR, n=22) — runners left alone make money. |
| LLM fallback | Local qwen3.5-9b vision lane has fired successfully (8 of 14 lifetime attempts, last success 2026-08-21). |

### Strategies / rules with positive evidence
| Rule | Status | Evidence (shadow unless noted) |
|---|---|---|
| Follow **Sveezy Alerts** | live | +$160 / +16.1R, n=34, 56% WR, max DD only $21. Post-cutoff +$91 / +9.3R (n=20). |
| `RSI_SELL_REGIME_GATE=true` | live | Blocked 34 rows: 21% WR, −18.5R. Strongest single gate. |
| Oracle `no_bias` gate (1H needs 4H bias) | live | Blocked 58 rows: −16.0R; post-cutoff 32 rows −15.3R. |
| Oracle `counter_bias` gate | live | Blocked 16 rows: −6.3R. |
| `RSI_CONFIRM_ENABLED=false` | live | Paired on identical rows, post-cutoff n=137: immediate +5.5R vs confirm −7.2R, **delta −12.7R, t=−2.56**. Confirm better on 2 rows, worse on 10. All-time delta −7.9R. |
| `RSI_FIRST_ALERT_ONLY=false` | live | Gate would have blocked +6.0R (n=49, 53% WR). Repeat alerts (`repeat_n>=2`) outperform first alerts post-cutoff: +4.0R (n=36) vs +2.0R (n=45). |
| `ORACLE_HTF_FALLBACK=false` | live | `htf_bear` rows 18% WR / −10.7R (n=17); `htf_bull` −2.3R (n=4). The computed-bias lane is the worst Oracle bucket. |
| `RUNNER_MAX_AGE_HOURS=120` (code default) | live | `time_stop` closes are +$103 net — the horizon is not cutting winners short. |

---

## 3. Working strategies — and how to improve them

### 3.1 Sveezy Alerts (analyst follow) — the profit center
- **Now:** +16.1R, n=34, lev 115x, balance $1,171 from $1,000. 21 of 34 closes are `manual_close` (Sveezy's own close calls), and those are net positive — the analyst manages exits competently.
- **Improvements**
  1. **Concentrate risk here.** `SVEEZY_ALERTS__RISK_PCT` is the lever. 1% → 1.5% would have produced roughly +$240 on the same trades with a max DD near $31. Do not exceed 2% until n ≥ 60.
  2. **Protect it from attribution drift.** The 2026-08-08 fix is only 6 Sveezy trades old. Keep the `_is_whitelisted` / `_resolve_analyst` single-implementation rule (`bot.py:371`, `bot.py:1494`).
  3. **Stop-loss discipline check:** only 5 of 34 Sveezy trades hit `sl`, but those 5 are the bulk of its losses. Worth a per-trade look at whether the parsed SL matched the posted SL (vision parser, 50% deviation guard).

### 3.2 RSI Extreme — marginally positive under current gates
- **Now (current ruleset, post 2026-07-10, immediate entry):** +6.0R over 81 gate-passing rows, 54% WR, +0.07R/trade. Full-sample ungated: −18.5R.
- **Why it is thin:** the edge is entirely regime-dependent. BTC 4H non-trending: +22.5R (n=164, 55% WR). BTC 4H trending: −41.0R (n=126, 33% WR). Post-cutoff the same split holds (+5.5R vs −18.0R).
- **Improvements**
  1. **Narrow `macro_trend` to `trending_strong` only, both sides.** The current gate blocks `trending_moderate` too, and the moderate bucket is ~flat post-cutoff (bear_moderate +1.0R n=24, bull_moderate +0.5R n=13) while `trending_strong` is the real loser (bull_strong −13.5R n=29, bear_strong −6.0R n=6). Blocking moderate costs volume for no protection. One-line change in the gate at `bot.py:~1799` if the threshold is not config-exposed.
  2. **Re-arm the chase filter.** `RSI_MAX_CHASE_PCT=1.00` means it never fires (code default 0.12). `f_chase` is logged on every row, so the right threshold can be swept offline before enabling — do the sweep, don't guess.
  3. **Do NOT adopt the alt exit (TP1 at 3.75%) on current-ruleset evidence.** All-time it looks good (+9.4R, WR 46 → 54%) but post-cutoff it is −0.1R (n=155, t=−0.02): it wins on a handful of large saves and loses on 71 of the 81 rows where the two differ. Revisit at n ≥ 250 post-cutoff.
  4. **Consider a long-only tilt in trending regimes.** `sell_regime` already blocks sells in strong trends; check `side` × regime before acting (not computed here).
  5. **Per-symbol ADX is useless as a filter:** 268 of 290 rows are `trending_strong`. BTC 4H regime is the only discriminating regime field.

---

## 4. Bad strategies — and how to fix them

### 4.1 OracleAlgo — net loser, structurally
- **Shadow:** −28.3R over 100 terminal rows, 35% WR, peak cumulative R never above −0.7R — it has never been in profit. Max DD 34R.
- **Paper:** +0.1R on 18 trades only because gates blocked 84 of 121 signals. Post-cutoff paper is +4.5R on 5 trades — too small to mean anything.
- **Where the losses are:** `stale-bear` bias rows −14.3R (n=29, 28% WR); post-cutoff −13.7R on 17 rows at 12% WR. `BOS` entries −11.0R (n=27) vs `MSS` −5.7R (n=37); post-cutoff BOS −8.7R (20% WR) vs MSS −0.3R (46% WR). Confluence=2 rows are better (−0.19R avg vs −0.30R) but n=14.
- **Fix options, in order of evidence**
  1. **Disable it for go-live** (`ORACLEALGO__ENABLED=false`). Shadow logging continues, so nothing measurable is lost. *Recommended.*
  2. If you keep it: `ORACLE_ENTRY_TYPES=MSS` (drops BOS, the worst type) **and** re-enable a real bias TTL (`ORACLE_BIAS_TTL_HOURS` is 99999 — every `stale-*` row is a bias older than you would ever trust by hand). On the sample this removes roughly −20R of the −28R.
  3. `ORACLE_CONFLUENCE_N=2` is directionally right but n=14 — a shadow experiment, not a live change.
  4. The wider 2.0% alt SL does not help (−2.5R all-time, +1.5R post, neither significant).

### 4.2 Soul Alerts — high volume, no edge, high drawdown
- n=50, −$7.8, 36% WR, **max DD $78** — the biggest drawdown contributor on the book. 24 of 50 closes are stop-outs.
- **Caveat:** 43 of those 50 trades predate the attribution fix; Soul was whitelist index 0 and absorbed mentions from other analysts. Post-fix: 7 trades, +$34, 43% WR.
- **Fix:** `SOUL_ALERTS__RISK_PCT=0.005` (half size) until 30 post-fix trades exist, then re-evaluate. Do not disable — the post-fix sample is positive and Soul is the highest-volume source, so it is also the fastest route to a clean read.

### 4.3 Caleb Alerts — small and negative
- n=6, −$17.7, 5 of 6 closes are `manual_close`. Too small to judge; the close calls are losing. Leave at default size, revisit at n=20.

### 4.4 Confirmation-on-turn entry — keep off
- Mechanically it enters 0.5% worse against a 5% fixed stop (~10% of an R given up per trade) and the re-entry rows lose −12.7R paired. The *skip* side (price never reverted → trade never taken) avoided −18.0R post-cutoff (n=18, 0% WR), but which rows those are is unknowable in advance — the filter value is not separable from the entry-delay cost. Only revisit if redesigned as "immediate entry, cancel if no revert", which is a different rule.

### 4.5 Zero-trade whitelist entries — CORRECTED 2026-08-22
- **Prestige Alerts was never subscribed, not silent.** The bot reads the Discord *mentions inbox*, so an analyst's signal cards only arrive if the account holds that analyst's `@… Alerts` role. Zero `@Prestige Alerts` pings were ever captured across 4,270 messages. Prestige's calls are standard text embeds (Entry / Stop Loss / Take Profits), the same format Soul and Sveezy use, so they parse with no code change now that the role is held (added 2026-08-22). **The earlier "remove Prestige from the whitelist" recommendation is withdrawn.**
- **Sherlock Alerts was a parser bug, fixed 2026-08-22 (uncommitted, needs bot restart).** His pings arrive (trailing `@Sherlock Alerts`) and his entries are fully structured — `$ONDSUSDT LONG Entry: MARKET PRICE ($9.26) Stoploss: 4H CLOSE BELOW $8.61 … TARGET: $12.94` — but the soft-stop stripper in `signal_parser.py` only knew `close under|below <bare number>`, so `CLOSE ABOVE` and `$`-prefixed levels leaked through to the close detector and **every one of his 31 structured entries since May was classified as a CLOSE** (`close_no_position`). Fix: widened `_CLOSE_UNDER_SL` and added a dedicated `Entry: MARKET|LIMIT PRICE` pattern with TP1+TARGET ladder and `soft_stop=True`. Corpus diff vs `HEAD`: 37 flips, all Sherlock (31 CLOSE→NEW, 2 dropped→NEW, 4 commentary CLOSE→none), zero flips on any other analyst; 7 new tests pass. Harness saved as `tools/parser_corpus_diff.py`. Yesterday's two Sherlock calls were XAGUSD (silver — not a BloFin perp) and a ZBT short whose entry message never reached the inbox.
- **Badillusion** arrives under the role spelling `@BadlIllusion Alerts` (7 pings) — working, but the name differs from the whitelist string; watch it.
- **Ajmal / Wilsauce / Grasady** last pinged 06-24 / 07-07 / 05-31. Quiet, not broken.
- **Nurse-Neil** arrives as `@Neilarora Alerts` (137 pings, handled by the matcher). +$20 on 4 trades — n too small.

---

## 5. What needs improvement (measurement and operations)

| # | Gap | Why it matters | Fix |
|---|---|---|---|
| 1 | **Preflight never verifiably run** | `preflight.py` prints to stdout only; zero log mentions. | Run it, save output to `reports/preflight-<date>.txt`. Go-live blocker. |
| 2 | **Watchdog dead since 2026-08-07** | The 08-17 → 08-20 Chrome/CDP tab-loss storm (329 errors, silent signal loss) went unwatched. | Start `watchdog.py` as a scheduled task; add a watchdog-alive check to preflight. Go-live blocker. |
| 3 | **CDP tab loss is the dominant failure mode** | 164 "lost Discord tab" + 164 "stale selectors" alerts in 4 days. | Auto-reacquire the tab / relaunch Chrome after N consecutive failures; `STALE_SWEEP_ALERT_N` is unset. |
| 4 | **Current-ruleset sample is small** | 81 gate-pass RSI rows, 22 post-attribution-fix paper trades. | Keep running dry; the bar for "certified" is ~150 gate-pass rows with sumR > 0 at t > 1.5. |
| 5 | **Shadow `no_data` decay** | 174 of 610 rows (29%) are permanently unmeasurable: Gate.io keeps ~34 days of 5m candles and the analyzer runs daily. | `SHADOW_ANALYZE_HOURS=6` so rows are terminal before they age out; optionally snapshot candles at signal time. |
| 6 | **No per-analyst shadow log** | `_shadow_strategy` is only called from the RSI/Oracle handlers, so analyst follow-trades have no counterfactuals. | Log analyst entries to the shadow CSV with `source=<analyst>`; the analyzer handles arbitrary sources if SL/TP are on the row. |
| 7 | **`analyst_stats` out of sync with `trades`** | Soul 20/35 vs 18/32, RSI 15/14 vs 13/10, Grasady 0/1 vs 0 trades. The leverage ladder is driven by `analyst_stats`. | Rebuild `analyst_stats` by replaying `trades` (same path-dependent replay as `migrate_neil_merge.py`). |
| 8 | **3 trades where `won` disagrees with `net_pnl` sign** | Leverage ladder can step the wrong way. | Derive `won` from net PnL everywhere (`time_stop` already does via `won=None`). |
| 9 | **Mixed timestamp zones in `signals`** | 341 UTC vs 3,924 at −05:00; `TZ_OFFSET_HOURS` shifts logs 1h. | Normalise to UTC on write; fix before any time-of-day analysis. |
| 10 | **`HEDGE_MODE=true` bypasses `MAX_OPEN_POSITIONS`** | Only `RSI_EXTREME__MAX_OPEN=10` bounds anything. Live, this is your exposure cap. | Set `MAX_OPEN_PER_SOURCE` (currently unset) for every live source. |
| 11 | **8 keys in `.env.example` but absent from `.env`** | `RUNNER_MAX_AGE_HOURS`, `LOG_MAX_MB`, `LOG_BACKUP_COUNT`, `MAX_OPEN_PER_SOURCE`, `STALE_SWEEP_ALERT_N`, `ORACLE_ALT_SL_PCT`, `RSI_ALT_TP_PCTS`, `ORACLE_API_TOKEN`. Behaviour depends on code defaults. | Pin them explicitly; add the 5 `.env`-only keys to `.env.example`. |
| 12 | **Shadow CSVs are uncommitted** | Git history is the only recovery path (it saved 149 rows in August). | Commit `strategy_shadow.csv` + `shadow_outcomes.csv` after every analyzer run. |
| 13 | **Testing apparatus undocumented** | README/CLAUDE.md never mention `analyze_shadow.py`, `weekly_report.py`, the shadow columns, or R definitions. | Add a "Measurement" section to README pointing at §8.1 of this report. |
| 14 | **LM Studio is a silent dependency** | Startup probe failed on 08-20 yet parses later succeeded — the lane no-ops when LM Studio is down. 57% lifetime parse success. | Re-probe periodically and alert; consider `ANTHROPIC_API_KEY` cloud fallback for live. |
| 15 | **Sherlock entries misclassified as CLOSE** (fixed 2026-08-22, uncommitted) | 31 structured entries since May silently dropped; Sherlock's real record is unknown. | Restart the bot to load the parser fix; commit `signal_parser.py` + `tools/parser_corpus_diff.py` + `tests/`; re-run the corpus harness on every future parser change (§4.5). |

---

## 6. What is needed to go live ("make it perfect")

Readiness criteria as set on 2026-07-11, with status today:

| # | Criterion | Status |
|---|---|---|
| 1 | Bot running post-`412f1ad` code | ✅ running `9b46cd8`, clean since 08-20 restart (0 errors) |
| 2 | ≥ 1 week clean paper data under current gates | ⚠️ 6 weeks of shadow data, but only 22 paper trades since the attribution fix and a 4-day capture outage inside the window |
| 3 | Oracle regime gate verified or Oracle off | ❌ gate still `false`, unverified (n=33). Resolution: turn OracleAlgo off (§4.1) |
| 4 | qwen3.5 fallback seen working in live log | ✅ last success 2026-08-21 09:48 |

**Launch-prep status, 2026-08-22 afternoon (all uncommitted, bot not yet restarted):**

| Blocker | Status |
|---|---|
| Preflight never recorded | ✅ Run twice; `reports/preflight-baseline-2026-08-22.txt` and `reports/preflight-20260822-1525.txt`. `preflight.py` now tees to `reports/`, checks watchdog freshness, config hygiene, hedge-mode caps, shadow-data lag, parser drift. Current verdict **NO-GO on 2 FAILs**: live keys (`152401 Access key does not exist` — needs the new BloFin account) and hedge-mode exposure uncapped (needs `MAX_OPEN_PER_SOURCE`, see `reports/env-launch-proposal.md`). |
| Watchdog dead | ✅ `watchdog.py --once` passes 7/7; `run_watchdog.bat` added; `schtasks` registration command in the preflight agent notes (user runs it). Not yet running as a service. |
| CDP tab loss unrecovered | ✅ `cdp_recovery.py` state machine wired into the main loop: re-attach after `CDP_RECOVER_AFTER_N` bad ticks, reopen the tab via CDP, escalate to "MANUAL RESTART NEEDED" after `CDP_RECOVER_MAX_ATTEMPTS`; alerts fire once per transition. Not exercised against live Chrome. |
| LM Studio silent dependency | ✅ `llm_probe.py` periodic re-probe (`LLM_PROBE_INTERVAL_MIN`), WARN + alert on transitions. |
| Vision failures invisible | ✅ Both failure paths promoted to WARNING with status/reply excerpt; brace-balanced JSON extractor; `LOG_LEVEL` key. |
| `won` ≠ PnL sign | ✅ `won` now derived from net PnL after fees in every settle path (`position_tracker.derive_won`); the 3 historic rows (ids 63, 69, 103) are explained in the agent notes. |
| `analyst_stats` desync | ⚠️ `tools/rebuild_analyst_stats.py` written and dry-run: 3 rows drift (Soul, RSI Extreme, Grasady); with `--won-from-pnl` RSI leverage goes 100→85. **Not applied** — your call. |
| Sherlock entries → CLOSE | ✅ fixed (§4.5). |
| `.env` gaps / exposure caps | 📄 Proposed in `reports/env-launch-proposal.md`, grouped A–E. Not applied. |

Additional gates before flipping `DRY_RUN`:

5. **Run `preflight.py` and archive a `GO`** (new BloFin account with known passphrase — the old live keys fail with 152408).
6. **Start the watchdog** and confirm it alerts (`python watchdog.py --test-alert`).
7. **Enable BloFin long/short position mode manually** — `HEDGE_MODE=true` requires it and it is not automated.
8. **Decide the live source set.** Recommended: Sveezy + RSI Extreme + Soul (half size). Everything else `__ENABLED=false` (still shadow-logged).
9. **Set exposure caps**: `MAX_OPEN_PER_SOURCE` for each live source; `LEVERAGE_MAX` sanity — Sveezy sits at 115x on paper; cap live at something survivable on a fat-finger fill.
10. **Rehearse the kill switch** (`<SOURCE>__ENABLED=false` hot-reload, and a full stop) once on demo.
11. **Start small**: `RISK_PCT=0.005` for the first 30 live trades, then step to 1%.
12. **Weekly sweep** (`weekly_report.py --days 7` + the §7 table) for the first month, with §5 #4 as the promotion criterion.

---

## 7. Recommended config changes (proposed — nothing applied)

| Key | Current | Recommended | Evidence |
|---|---|---|---|
| `ORACLEALGO__ENABLED` | (unset = on) | `false` | −28.3R / n=100 shadow; never in profit |
| `SVEEZY_ALERTS__RISK_PCT` | 0.01 | `0.015` | +16.1R n=34, max DD $21 |
| `SOUL_ALERTS__RISK_PCT` | 0.01 | `0.005` | max DD $78, 36% WR; 43/50 trades pre-attribution-fix |
| Discord role (not `.env`) | Prestige role missing | hold `@Prestige Alerts` | 0 pings ever captured; added 2026-08-22 |
| (code) `signal_parser.py` | Sherlock entries → CLOSE | restart bot to load the 2026-08-22 fix | 31 entries misclassified; corpus diff clean |
| `RSI_MACRO_TREND_GATE` | true (moderate+strong) | keep, narrow to `trending_strong` | moderate bucket ~flat post-cutoff; strong −19.5R |
| `RSI_MAX_CHASE_PCT` | 1.00 (inert) | sweep `f_chase` offline, then set | filter currently does nothing |
| `SHADOW_ANALYZE_HOURS` | 24 | `6` | 29% of rows lost to candle retention |
| `MAX_OPEN_PER_SOURCE` | unset | set per live source | `HEDGE_MODE` bypasses the global cap |
| `RUNNER_MAX_AGE_HOURS` | unset (120) | `120` explicit | time_stop is +$103; pin it |
| `STALE_SWEEP_ALERT_N` | unset | set | CDP outage alert governance |
| `RSI_CONFIRM_ENABLED` | false | **keep false** | paired −12.7R, t=−2.56 |
| `RSI_FIRST_ALERT_ONLY` | false | **keep false** | gate would block +6.0R |
| `ORACLE_HTF_FALLBACK` | false | **keep false** | htf_* buckets −13R / n=21 |
| `RSI_TP_PCTS` | 0.05,0.10 | **keep** | alt ladder −0.1R post-cutoff |
| `ORACLE_BIAS_TTL_HOURS` | 99999 | only if Oracle stays on: ≤ 24 | stale-bear −14.3R |

---

## 8. Appendix

### 8.1 Methodology
- **Shadow R** (`r_mult`): blended per-rung return ÷ the strategy's fixed SL% (RSI 5%, Oracle 1.5%). `loss_sl` is exactly −1.0. Intra-candle SL/TP ambiguity resolves as the stop (conservative). Source: Gate.io 5m candles, 72h horizon.
- **Paper R** (`bot_ledger`): per-trade structural R from the parsed SL. **Shadow R and paper R are never summed together in this report.**
- **Terminal** = `loss_sl`, `tp_then_be`, `win_full`. `open` (46) and `no_data` (174) rows are excluded from all R sums.
- **Cutoff 2026-07-10** = current gate set live (`412f1ad`). **Attribution fix 2026-08-08** = author-first matching.
- **Paired tests** compare `r_mult` vs `confirm_r` / `alt_r` on the *same row*. Bucket comparisons across `decision` values are confounded by time (June = first-alert gate on, July = confirm on, August = current) and are reported only as gate cost/benefit, never as strategy A vs B.
- **"Current ruleset immediate entry"** = post-cutoff RSI rows with `decision ∈ {enter, pending_confirm}` — both passed every live gate; `pending_confirm` rows would have been immediate entries under today's `RSI_CONFIRM_ENABLED=false`.

### 8.2 Shadow coverage
| month | source | n | terminal | open | no_data |
|---|---|---|---|---|---|
| 2026-06 | rsi_extreme | 116 | 87 | 7 | 22 |
| 2026-06 | oraclealgo | 47 | 46 | 1 | 0 |
| 2026-07 | rsi_extreme | 269 | 134 | 16 | 119 |
| 2026-07 | oraclealgo | 49 | 39 | 10 | 0 |
| 2026-08 | rsi_extreme | 104 | 69 | 2 | 33 |
| 2026-08 | oraclealgo | 25 | 15 | 10 | 0 |

### 8.3 Shadow: source × decision (terminal rows)
| period | source | decision | n | WR% | sumR | avgR |
|---|---|---|---|---|---|---|
| ALL | rsi_extreme | enter | 85 | 38.8 | −19.5 | −0.229 |
| ALL | rsi_extreme | pending_confirm | 83 | 57.8 | +14.0 | +0.169 |
| ALL | rsi_extreme | repeat_alert | 49 | 53.1 | +6.0 | +0.122 |
| ALL | rsi_extreme | macro_trend | 35 | 48.6 | +0.5 | +0.014 |
| ALL | rsi_extreme | sell_regime | 34 | 20.6 | −18.5 | −0.544 |
| ALL | rsi_extreme | over_chased | 4 | 50.0 | −1.0 | −0.250 |
| ALL | oraclealgo | no_bias | 58 | 37.9 | −16.0 | −0.276 |
| ALL | oraclealgo | enter | 16 | 37.5 | −2.0 | −0.125 |
| ALL | oraclealgo | counter_bias | 16 | 31.2 | −6.3 | −0.396 |
| ALL | oraclealgo | already_open | 10 | 20.0 | −4.0 | −0.400 |
| post | rsi_extreme | enter | 9 | 22.2 | −5.0 | −0.556 |
| post | rsi_extreme | pending_confirm | 72 | 58.3 | +11.0 | +0.153 |
| post | rsi_extreme | macro_trend | 35 | 48.6 | +0.5 | +0.014 |
| post | rsi_extreme | sell_regime | 34 | 20.6 | −18.5 | −0.544 |
| post | oraclealgo | no_bias | 32 | 31.2 | −15.3 | −0.479 |
| post | oraclealgo | enter | 3 | 66.7 | +3.0 | +1.000 |

### 8.4 Paired counterfactuals
| test | period | n | base sumR | alt sumR | delta | better / worse | t |
|---|---|---|---|---|---|---|---|
| Confirm-on-turn (RSI, fired) | ALL | 258 | +11.0 | +3.1 | −7.9 | 9 / 15 | −1.11 |
| Confirm-on-turn (RSI, fired) | post | 137 | +5.5 | −7.2 | **−12.7** | 2 / 10 | **−2.56** |
| Confirm skipped rows (never fired) | post | 18 | −18.0 | — | — | 0% WR | — |
| Alt exit, RSI (TP1 3.75%) | ALL | 290 | −18.5 | −9.1 | +9.4 | 24 / 133 | +1.25 |
| Alt exit, RSI | post | 155 | −12.5 | −12.6 | −0.1 | 10 / 71 | −0.02 |
| Alt exit, Oracle (SL 2.0%) | ALL | 100 | −28.3 | −30.9 | −2.5 | 7 / 35 | −0.72 |
| Alt exit, Oracle | post | 41 | −12.3 | −10.9 | +1.5 | 6 / 14 | +0.50 |

### 8.5 RSI by BTC 4H regime (terminal rows)
| period | bucket | n | WR% | sumR |
|---|---|---|---|---|
| ALL | non-trending (pooled) | 164 | 55.5 | +22.5 |
| ALL | trending (pooled) | 126 | 33.3 | −41.0 |
| ALL | bull_ranging_calm | 22 | 72.7 | +12.0 |
| ALL | bear_trending_strong | 36 | 25.0 | −19.5 |
| ALL | bull_trending_strong | 37 | 24.3 | −16.5 |
| post | non-trending (pooled) | 83 | 54.2 | +5.5 |
| post | trending (pooled) | 72 | 36.1 | −18.0 |
| post | bull_trending_strong | 29 | 24.1 | −13.5 |
| post | bear_trending_moderate | 24 | 50.0 | +1.0 |
| post | bull_trending_moderate | 13 | 53.8 | +0.5 |

### 8.6 Paper book per source (`bot.db`, all `dry_run=1`)
| source | n | W/L | net $ | R | lev | balance | max DD $ | post-fix n / $ |
|---|---|---|---|---|---|---|---|---|
| Sveezy Alerts | 34 | 19/15 | +159.81 | +16.14 | 115 | 1171.1 | 20.7 | 6 / +7.2 |
| RSI Extreme | 23 | 13/10 | +34.42 | +3.53 | 100 | 1034.5 | 33.2 | 1 / +12.4 |
| Nurse-Neil Alerts | 4 | 2/2 | +19.59 | +2.01 | 75 | 1020.0 | — | 1 / +10.8 |
| Ajmal Alerts | 3 | 2/1 | +14.49 | +1.45 | 85 | 1014.3 | — | 0 |
| Wilsauce Alerts | 1 | 1/0 | +1.66 | +0.18 | 85 | 1001.8 | — | 1 / +1.7 |
| Sherlock Alerts | 1 | 0/1 | −0.85 | −0.09 | 65 | 999.1 | — | 1 / −0.9 |
| OracleAlgo | 18 | 9/9 | −1.33 | +0.10 | 100 | 999.7 | 49.2 | 5 / +42.4 |
| Soul Alerts | 50 | 18/32 | −7.81 | −0.44 | 60 | 991.0 | 77.7 | 7 / +34.2 |
| Caleb Alerts | 6 | 2/4 | −17.66 | −1.80 | 55 | 982.1 | — | 0 |
| **Total** | **140** | **66/74** | **+202.32** | **+21.08** | | | **111.0** | **22 / +107.7** |

### 8.7 Close-reason economics
| reason | n | sum $ | avg $ | WR% |
|---|---|---|---|---|
| sl | 51 | −547.82 | −10.74 | 0 |
| tp1_final | 21 | +441.46 | +21.02 | 100 |
| tp2_final | 8 | +139.52 | +17.44 | 100 |
| time_stop | 22 | +102.75 | +4.67 | 54.5 |
| trail_stop | 6 | +34.66 | +5.78 | 100 |
| manual_close | 32 | +31.75 | +0.99 | 59.4 |

### 8.8 Data provenance
- `strategy_shadow.csv` 610 rows, `shadow_outcomes.csv` 610 rows (refreshed 2026-08-22, uncommitted), `bot.db` trades=140 / ledger=140 / signals=4,265, `signals_log.csv` 4,265 rows.
- Computation: `results_stats.py` / `results.json` in the session scratchpad (reproducible from the files above; read-only against `bot.db`).
- Supersedes: 2026-07-02 sweep, 2026-07-10 sweep, 2026-08-07 re-analysis (post outcome-decay recovery).
