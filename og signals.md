Unity Signal Bot
A walkthrough for analysts and admins. Each section shows the exact buttons to press and what to fill in.
Open Dashboard →
›
Getting started
›
Posting a signal
›
Preview options
›
Managing trades
›
Closing trades
›
DCAs
›
Past trades
›
Your settings
›
Server setup
›
Recaps
›
Exports
›
Managing admins
Getting started
Everyone
Unity Signal Bot is a trade-signal bot for Discord. Analysts post structured trades, and the bot tracks them live, posts follow-ups on TP/SL hits, and edits the original signal in place so it's always current.

The only command you really need
Everything you do as an analyst happens inside /signal. Run it in any channel and a private dashboard pops up that only you can see.

/signal dashboard
Need the analyst role. If /signal says you need a role, ask an admin to run /setup analyst_role:@role and give you that role.
Posting a new signal
Analyst
Six clicks and a short form. The signal is previewed privately before it's posted so you can fix mistakes.

Run /signal.
Click New Trade.
Pick the trade type — Leverage, Spot, or Stock.
Pick direction — Long / Buy or Short / Sell.
A modal opens. Fill in the 4 fields (see below).
A preview card shows. Click Send Signal to post it.
The details modal
Signal details (popup)
New Signal
Coin / Ticker
BTC, ETH, SOL, AAPL…
Entry Price
95000, 95k, or "cmp" for current
Stop Loss
92k (required)
Take Profits
100k, 105k, 110k (comma separated)
Abbreviations work everywhere: 78k, 1.5m, 0.0042.
CMP — type cmp in the entry field and the bot pulls live price from Bybit (Binance as fallback).
Stop loss is required. Take profits are comma-separated — add as many as you want.
Price sanity check — if your entry is >20% off market, the bot warns you in the preview.
Preview options
Analyst
After the modal, you land on the preview card. Everything here is optional except Send or Cancel.

Preview card
BTC/USDT — LONG (Leverage)
Signal by AnalystName
Entry $95,000 · SL $92,000 (-3.16%) · TPs 100k / 105k / 110k
Edit Details — re-opens the modal so you can fix entry / SL / TPs / entry type (market vs limit).
Notes — adds an analysis paragraph shown in the public signal embed.
SL: Hard / Soft toggle — soft SL means "only invalidated on candle close on X timeframe" and opens a timeframe picker.
Add DCA — structured entries with allocation % (see the DCAs section below).
Attach Chart — click it, then drop an image in the channel. The bot picks it up, deletes your message, and attaches it to the final signal.
Cancel — throws away the draft.
Send Signal — posts publicly as you (via webhook — uses your name + avatar), pings your ping roles, and attaches the chart if uploaded.
Managing active trades
Analyst
Once a signal is live, open /signal → My Trades to update it.

Run /signal → My Trades.
Pick the trade from the dropdown.
You land on the Management Panel. Pick an action.
Management panel
What each button does
TP Hit — modal asks which TP number and what % to trim. Posts a public "TP1 hit" follow-up and edits the original embed.
SL → BE — instant, no modal. Moves stop loss to entry price and posts a follow-up.
Move SL — modal for a custom SL price. cmp works here too (though the hint isn't shown).
Close Trade — the universal close. See the next section.
DCA Filled — mark a structured DCA level as filled at a price. Bot recalculates the weighted average entry.
Auto-close mode. Turn it on in Settings and the price monitor will auto-execute SL/TP hits instead of just pinging you. Off by default.
Closing a trade
Analyst
One button for every kind of close — full, partial, stopped, cut early, breakeven, invalidation.

In the management panel, click Close Trade.
Fill the modal and submit.
Close trade (popup)
Close Trade
Close Price
98000, 98k, or "cmp"
Close Type
profit / stopped / cut / be / invalidation / partial
% to Close
100 for full, 50 for partial, etc.
Notes (optional)
Why you closed it
Full close — % to close = 100. Trade moves to Past Trades.
Partial close — set type to partial and enter the %. Trade stays active with a smaller remaining size.
Close type determines the follow-up wording and P&L category (wins need profit, losses stopped or cut).
cmp works in close price — pulls the current market price.
Adding DCAs
Analyst
DCAs (dollar-cost-average entries) let you lay multiple entry levels. Up to 4 entries, allocations must sum to 100%.

On a fresh signal (before you post)
In the preview card, click Add DCA.
Fill up to 4 rows. Each row needs a price and an allocation %.
The allocations must sum to exactly 100.
Submit. Entry 1 becomes the headline entry price; the bot computes the weighted average.
DCA entries (popup)
Structured Entries
Entry 1 — price & allocation %
95000, 40
Entry 2 — price & allocation %
93000, 30
Entry 3 — price & allocation %
91000, 20
Entry 4 — price & allocation %
89000, 10
On a trade that's already posted
You can't add a new DCA level after posting, but when an existing DCA level fills you mark it:

/signal → My Trades → pick the trade.
Click DCA Filled.
Enter which level filled and at what price. Average entry recomputes.
Past trades
Analyst
View and fix closed trades. Useful if you typoed a close price or want to change the close type / notes.

/signal → Past Trades.
Pick the trade from the dropdown.
Click Edit and adjust the close price, type, or notes.
Submit. The original follow-up message gets updated.
Your settings
Analyst
Personal preferences for your signals. Stored per analyst per server.

Run /signal → Settings.
Pick a preset color swatch or Custom for a hex code.
Toggle Auto-Close on if you want the monitor to auto-execute SL/TP hits.
Ping roles
Ping roles are not analyst-configurable. A server admin sets them per analyst in the admin dashboard, alongside your default signal channel.

Server setup
Server admin
Run once when adding the bot. Requires Discord Administrator.

Run /setup analyst_role:@YourAnalystRole.
Anyone with that role can now use /signal.
Webhook permission — give the bot Manage Webhooks so it can post signals under the analyst's name and avatar. Without it, signals still post but as the bot user.
Recaps
Admin
Public performance summary for a time period. Posted as a visible message, not ephemeral.

Run /recap period:today (or yesterday / week / month / custom).
For custom, add start_date:YYYY-MM-DD and end_date:YYYY-MM-DD.
Optionally filter with analyst:@user.
The recap includes wins, losses, breakeven count, win rate, average P&L, best/worst trade, and per-analyst breakdown.

Exports
Admin · Analyst
Get your data out as CSV or JSON. Admins see all server data; analysts get their own trades only.

Run /export format:csv (or json).
Add filters as options: scope, status, period, analyst, start_date, end_date.
The bot sends you a file attachment privately.
scope:trades — just trade rows. scope:events — every action logged. scope:both — both files.
status:closed — only closed trades. active — only open. all — everything.
period:month — shortcuts for common ranges. custom + dates for anything else.
Managing admins
Bot owner
Bot owners (set via BOT_OWNER_IDS in the bot's env) can grant server admin status. Server admins aren't the same as Discord admins — they can run /recap and full-scope /export.

/admin add user:@user — adds someone as a server admin for the current server.
/admin remove user:@user — removes them.
/admin list — lists all server admins.