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

### `/check-budget` command
Type `/check-budget` to open a modal with two multi-selects — **Location** (NYC/SF) and
**Months** (the 12 months of the budget year). On submit, the bot reads the sheet live
and posts a spending report
(Estimated & Actual vs Monthly Budget per month, plus a multi-month total) as an
ephemeral message visible only to you.

### Weekly rep-assignment rundown
Every **Monday 10:00 America/New_York**, the bot reads this week's (Mon–Sun) **NYC**
events from Notion and:
- If every event has reps → posts a rundown (events grouped by day, each as
  `[Event](invite link) - @rep @rep`) to the channels in `RUNDOWN_CHANNELS`.
- If any event is missing reps → DMs Drew Parten a reminder listing them (with Notion
  links) and adds a `:done:` reaction. When Drew reacts `:done:`, it posts the rundown.

`HOLD`/`[HOLD]` events are skipped. Rep names are mapped to Slack `@`-mentions via the
`REP_MAP_CSV` tab (name → Slack ID); unmapped names post as plain text. `/events-this-week`
shows the current rundown to whoever runs it (ephemeral).

`/my-event` lets a rep see their own upcoming assignments (next 60 days, any city): the
bot maps the caller's Slack ID back to their Notion rep name(s) via `REP_MAP_CSV` and lists
the events they're assigned to. Ephemeral; if the caller isn't in the rep sheet it says so.

### Rep-assignment Q&A and changes (@mention or DM)
A rep can **@mention the bot** in a channel, or **DM it**, in plain language. The bot
classifies each message as a **question**, a **change**, or neither:

- **Question** ("what upcoming events is Lavar Buckmon on?", "who's assigned to the Founder
  Dinner on the 28th?", "how many events do I have next week?") → the bot answers from the
  Notion event data.
- **Change** ("I can't make the Founder Dinner on the 28th, Marc is covering") → Claude picks
  the single matching upcoming event and the reps to add/remove, the bot **updates the Notion
  `Reps`** field, then replies with exactly what changed.
- **Neither** (greetings, chit-chat, or a request it can't tie to a specific event) → the
  bot stays silent. Guardrails: only upcoming events;
a rep to add must already exist in the `Reps` options (no junk options are created);
"me/I" resolves to the sender via `REP_MAP_CSV`; if the event is ambiguous or a name can't
be resolved, the bot asks to clarify instead of writing.

The conversation can continue **in-thread**: once the bot has replied in a thread (or been
@mentioned in one), a rep's follow-up replies there — with no new @mention — are picked up
too, and prior thread messages are given to Claude so "the one on the 24th" / "yes" resolve.

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
  **`reactions:write`** (seed ✅/:done: reactions), **`commands`** (slash commands),
  **`im:write`** + **`im:history`** (DM Drew / accept rep DMs), **`app_mentions:read`**
  (accept @mentions).
- Event subscriptions (bot events): `reaction_added`, `message.channels`,
  **`app_mention`**, **`message.im`**.
- Slash commands created (Features → Slash Commands): **`/check-budget`**,
  **`/events-this-week`**, **`/my-event`**. In Socket Mode no Request URL is needed.
- Bot invited to #community-team, **#ny-vc-squad**, and **#qualifiers-across-department**
  (`/invite @your-bot`).
- Custom emoji **`:done:`** must exist in the workspace.
- **Reinstall the app** after changing scopes, events, or commands.

## Budget sheet config

The bot reads the budget from either backend; whichever is configured wins (service
account first). Each city tab needs a `Monthly Budget` cell and a `Cost Analysis Per
Month` table (Month / Estimated columns) — cells are located by content, not position.

**Option A — published CSV (no credentials, public tab):**
- In the sheet: **File → Share → Publish to web**, pick a tab, format **CSV**, Publish.
- Put the resulting URL in `BUDGET_CSV_NYC` / `BUDGET_CSV_SF` (one per tab).
- Live, but Google caches published output (~up to 5 min lag). The published tabs are
  readable by anyone with the URL.

**Option B — Google service account (private, preferred):**
- Create a service account, enable the Google Sheets API, download its JSON key. Put it
  (full JSON on one line, or base64) in `GOOGLE_SERVICE_ACCOUNT_JSON`.
- **Share the spreadsheet** with the service account's `client_email` (Viewer).
- Tab titles must be exactly `NYC` and `SF`.

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
