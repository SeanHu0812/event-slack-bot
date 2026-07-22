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

### Behavior on edge cases
- **Reaction fires twice** → dedup check finds the existing page, does nothing.
- **Non-proposal** (a link, a photo, no event name) → parse returns no event, bot stays silent.
- **No / TBD date** → replies in-thread asking for manual entry, creates no page.
- **City not in the valid list** → omits the `City` property instead of inventing an option.
- **Notion or Slack API error** → logged; the process does not crash and the websocket stays up.

## Setup

1. `python -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in the four tokens:
   - `SLACK_BOT_TOKEN` — the `xoxb-` bot token
   - `SLACK_APP_TOKEN` — the `xapp-` app-level token (Socket Mode, `connections:write`)
   - `NOTION_TOKEN` — the `ntn_` integration secret (integration must be shared with the DB)
   - `ANTHROPIC_API_KEY` — the `sk-ant-` key
4. Run: `python app.py`

## Slack app config (already done)

- Socket Mode enabled → `xapp-` app-level token with `connections:write`.
- Bot scopes: `reactions:read`, `channels:history`, `chat:write`, `users:read`.
- Event subscription: bot event `reaction_added`.
- Bot invited to #community-team (`/invite @your-bot`).

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
| `Cost` | rich_text | kept as written, e.g. "$3k" |
| `Invite Link` | rich_text | plain text, not a url-type property |
| `Notes` | rich_text | holds `slack_ts:<ts>` — the dedup marker |

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
