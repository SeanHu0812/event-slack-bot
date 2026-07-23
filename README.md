# Community Events Slack Bot

A Slack bot for **#community-team**. When Justin adds his `:approved:` reaction to an
event proposal message, the bot parses the proposal, creates a page in the Notion events
calendar, and replies in that message's thread with `Notion page created: <link>`.

No DMs, no other output.

## How it works

1. Listens for `reaction_added` over Slack **Socket Mode** (no public URL needed).
2. Only acts when the reaction is `:approved:`, the reactor is Justin, and it's in
   #community-team.
3. Dedups against Notion (`Notes` contains `slack_ts:<ts>`) before creating anything —
   safe against the event firing twice and against process restarts.
4. Parses the free-text proposal into clean JSON with one Anthropic call.
5. Creates the Notion page (title + date + city/partner/cost/invite link + dedup marker).

### Budget warnings (NYC & SF only)
The bot cross-checks proposal cost against the monthly budget in a Google Sheet
(`Cost Analysis Per Month` table on the NYC and SF tabs; `Monthly Budget` cap per tab).
`projected = that month's Estimated + this event's cost`, compared to the Monthly Budget:

- **When a proposal is posted** in the channel, if `projected` is ≥90% of budget the bot
  posts a heads-up in-thread (a bigger warning at ≥100%).
- **When approved**, if it stays under 100% the page is created and (at 90–99%) a
  "you have $X left" note is posted.
- **When approving would push the month to ≥100%**, the bot does **not** create the page.
  It posts a confirmation with a ✅; only when an approver clicks the ✅ is the page created.

Only NYC and SF have budgets — other cities are created normally with no budget check.
If the Google credentials aren't configured, budget checks are skipped entirely.

### Behavior on edge cases
- **Reaction fires twice** → dedup check finds the existing page, does nothing.
- **Non-proposal** (a link, a photo, no event name) → parse returns no event, bot stays silent.
- **No / TBD date** → replies in-thread asking for manual entry, creates no page.
- **City not in the valid list** → omits the `City` property instead of inventing an option.
- **Notion / Slack / Sheets API error** → logged; the process does not crash and the websocket stays up.

## Setup

1. `python -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in the four tokens:
   - `SLACK_BOT_TOKEN` — the `xoxb-` bot token
   - `SLACK_APP_TOKEN` — the `xapp-` app-level token (Socket Mode, `connections:write`)
   - `NOTION_TOKEN` — the `ntn_` integration secret (integration must be shared with the DB)
   - `ANTHROPIC_API_KEY` — the `sk-ant-` key
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — *(optional)* service-account key JSON for budget
     checks; omit to disable budget warnings
4. Run: `python app.py`

## Slack app config

- Socket Mode enabled → `xapp-` app-level token with `connections:write`.
- Bot scopes: `reactions:read`, `channels:history`, `chat:write`, `users:read`,
  **`reactions:write`** (to seed the ✅ on over-budget confirmations).
- Event subscriptions (bot events): `reaction_added`, **`message.channels`**
  (to see proposals when they're posted).
- Bot invited to #community-team (`/invite @your-bot`).
- **Reinstall the app** after changing scopes or event subscriptions.

## Budget sheet config (Google service account)

- Create a Google Cloud service account, enable the Google Sheets API, and download its
  JSON key. Put the key (full JSON on one line, or its base64) in `GOOGLE_SERVICE_ACCOUNT_JSON`.
- **Share the budget spreadsheet** with the service account's `client_email` (Viewer).
- Tab titles must be `NYC` and `SF`; each needs a `Monthly Budget` cell and a
  `Cost Analysis Per Month` table (Month / Estimated columns).

## Notion config (already done)

- Internal integration created; `ntn_` secret in `NOTION_TOKEN`.
- 2026 Events & Community Calendar shared with the integration (⋯ → Connections).

The page is created with **only** these properties and no page body:

| Property | Type | Notes |
|---|---|---|
| `Event` | title | event name |
| `Date` | date | `start = YYYY-MM-DD` |
| `City` | select | must match a valid option or is omitted |
| `Partner` | rich_text | |
| `Estimated Cost` | number | proposal cost, converted to a number (e.g. "$3k" → 3000) |
| `Invite Link` | rich_text | plain text, not a url-type property |
| `Notes` | rich_text | holds `slack_ts:<ts>` — the dedup marker |

The bot **never writes to `Actual Cost`** — that number field is filled in manually
after an event happens. Proposal cost goes only to `Estimated Cost`.

Valid `City` options: Atlanta, Austin, Boston, Chicago, Holiday, LA/El Segundo, Miami,
Montana, NYC, Nashville, New Mexico, Phoenix, SF, San Diego, Seattle, Vegas, DC.

## Test before trusting it

1. Run the bot locally.
2. In #community-team, post a fake proposal, then react `:approved:` **as Justin's account**
   (or temporarily set `APPROVER_ID` to your own user ID for testing).
3. Confirm one Notion page appears with correct fields and an empty body, and the
   thread reply posts.
4. React again → confirm no duplicate page.
5. Reset `APPROVER_ID` to Justin before going live.
