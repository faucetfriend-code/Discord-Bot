# Analyst Trade-Input App: Proposal

## The opportunity

Give each analyst a near-zero-effort way to log trades, and you get verified,
exact track records for every analyst plus a clean signal feed that followers
can mirror. Low friction is the whole game: the easier it is, the more analysts
actually use it, and adoption is what turns analyst calls into a product.

## How it works (analyst side)

They trade as they already do, on their own account and exchange. They chart the
trade with the TradingView position tool, screenshot it, and either click a
browser extension (desktop) or paste it in the analyst channel (mobile). The app
reads the entry, stop, and targets, shows them back for a one-tap confirm, and
fires. About ten seconds.

## Why it is credible, not hand-wavy

- It builds on the existing bot, not a rewrite. The screenshot-reading pipeline
  is already partly scaffolded.
- The OCR only has to place the order. The real fill from the analyst's exchange
  is the source of truth, so the tracked record is the actual executed trade,
  exact by definition.
- Multi-exchange and per-analyst token differences are handled by one unified
  execution layer (CCXT), not a separate integration per exchange.
- Security is bounded: trade-only API keys, no withdrawal permission, set up
  once.

## What it takes (build order)

1. Unified execution layer (CCXT) so any analyst's exchange works through one path.
2. Per-analyst account registry (their exchange + trade-only keys).
3. Screenshot-to-confirm front ends: Discord bot for mobile, browser extension
   for desktop.

## The ask

A small pilot: wire one or two analysts end to end on their own exchanges, prove
the capture-confirm-fire-track loop, then roll out.
