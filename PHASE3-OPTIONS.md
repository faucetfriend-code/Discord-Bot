# Phase 3: Live Per-User Execution - Options

How to go from paper portfolios (built in Phase 1/2) to real trades on real
user accounts. Three viable paths, not mutually exclusive.

## Option A: BloFin native copy trading (lead trader)

BloFin has first-class copy trading built into the exchange, with a full API.

How it works: you apply to become a Lead Trader (verified/KYC account,
application takes minutes, approval required). Your engine then places its
orders in the dedicated `copy_trading` account instead of `futures`. Users
follow you ON BLOFIN, fund their own copy-trade wallet, and BloFin mirrors
every order into their accounts automatically.

What BloFin handles for you:
- Per-user execution, sizing and mirroring. Copiers choose Smart Copy,
  Fixed Amount, or Fixed Ratio mode.
- Custody: you never touch user funds or keys.
- Revenue: lead traders earn 10% profit share of copiers' net profit,
  credited every Monday, plus possible fee commissions.
- Scale: up to 200 copiers per lead account.

API surface (already in the `blofin` Python SDK this bot uses):
- `from blofin.rest_copytrading import CopyTradingAPI`
- `BlofinWsCopytradingClient` for the private WS feed
- REST endpoints mirror normal trading: Place Order, Cancel Order,
  Place/Cancel TPSL (by order or contract), Close Position, Set Leverage,
  Set Position Mode, GET Copy Trading Account Balance, positions, history.
- Private WS: `wss://openapi.blofin.com/ws/copytrading/private`

Engine changes needed: small. `blofin_client.py` grows a copy-trading mode
that routes place/close through `CopyTradingAPI`; everything upstream
(parser, risk manager, tracker) is untouched.

Limitations:
- One lead account = ONE public strategy stream. The per-analyst bot
  experience cannot be expressed as separate followable bots unless each
  bot gets its own verified lead-trader account. Whether one person may
  operate multiple lead accounts (e.g. via sub-accounts) needs to be
  confirmed with BloFin support before designing around it.
- BloFin owns the user relationship, the UI, and the rules.
- Demo endpoint compatibility for copy-trading endpoints is unverified;
  test on demo first as usual.

Best for: validating demand fast with the single best-performing stream
(e.g. Sveezy-only, as discussed in the Unity server) while keeping zero
custody risk.

## Option B: self-built fan-out executor (users' own API keys)

Users paste BloFin API keys into their profile; the engine executes every
followed bot's signal on each subscriber's account, sized to their balance
and risk setting. This is the natural continuation of the Phase 1/2 design:
`subscriptions` already maps users to bots.

Required pieces:
1. Key storage: encrypt at rest (Fernet/AES-GCM with a master key kept
   outside the repo and DB; rotate on compromise). Require users to create
   TRADE-ONLY keys with withdrawals disabled and IP-whitelisted to your
   server. Never log keys.
2. Executor: a worker pool that fans one engine signal out to N user
   accounts. Per-user sizing via the existing risk_manager math against
   that user's live balance. Per-account rate-limit budgeting, retries,
   and partial-failure isolation (one user's failed order must not block
   the rest).
3. Reconciliation: the existing `_reconcile_with_exchange` logic, but per
   account, so a user who manually closes a position does not drift.
4. TP/SL management: either exchange-side TPSL orders per account
   (preferred; survives bot downtime) or the current soft management loop
   run per account.
5. Audit: per-user order log (extend `user_ledger` with order ids/fills).

Costs and risks:
- You are operating discretionary trading on other people's accounts.
  Depending on jurisdiction this can require registration/licensing
  (investment adviser / asset manager rules). Get real legal advice
  before onboarding strangers; friends-and-family testing is a different
  risk profile than public launch. (Not legal advice.)
- Security burden is real: a leaked master key = control of every user
  account's trading.
- Ops burden: N accounts x M positions monitored 24/7; the current
  single-process loop needs hardening (supervisor, alerting, failover).

Best for: the full multi-bot product vision, where each analyst bot is
independently followable with per-user risk settings, your own UX, and
your own economics.

## Option C: signal distribution, execution stays with the user

Ship the executor to users instead of their keys to you. The bot already
exposes `/api/v1`; add a signed webhook or WS feed of bot signals, and a
small "follower client" (this same codebase minus Discord reading) that
users run locally with their own keys and their own .env.

- Pros: zero custody, minimal regulatory surface (you publish signals,
  the user executes), keys never leave the user's machine.
- Cons: onboarding friction (users must run software), version drift,
  support burden; "bots" feel less turnkey.

Best for: technical users, or as a bridge while Option A validates demand.

## Suggested sequencing

1. Keep dry-run multi-bot paper trading running (done) to build per-bot
   track records; that data is the product regardless of path.
2. Confirm with BloFin whether multiple lead accounts / sub-account lead
   traders are allowed. If yes, Option A can host 2-3 of the best bots
   cheaply.
3. Prototype Option A on the best single stream once its paper record
   justifies it (preflight.py GO, then small real balance).
4. Revisit Option B only with legal sign-off and real demand; reuse the
   Phase 2 tables (users/subscriptions) as the control plane.

## References

- BloFin API docs: https://docs.blofin.com/index.html (Copy Trading section)
- Python SDK: https://github.com/blofin/blofin-sdk-python
- Lead trader guide: https://support.blofin.com/hc/en-us/articles/13837466296847
- Lead trader user center (profit share, 200-copier cap):
  https://support.blofin.com/hc/en-us/articles/12362092015887
- Copier modes (Smart Copy / Fixed Amount / Fixed Ratio):
  https://blofin.com/en/support/FAQ/Copy-Trading/12361244393231
- Lead trader application: https://blofin.com/en/copy-trade/application
