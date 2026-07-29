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

### Proposal auto-assessment (1–10 score)
When a proposal is posted in #community-team, the bot replies in-thread with a 1–10
assessment across three aspects, so approvers have a read before they `:approved:`:

1. **Business-goal fit** — scored against Tarlon's *Event Partner Screening Framework*
   (baked into the prompt; six factors, ICP/audience weighted highest).
2. **Past feedback** — read directly from the **#events-feedback** channel (the Tally form
   submissions), matched to the proposed partner/format by keyword overlap. The channel is
   the complete source of truth; the synced Notion DB was lossy, so the bot parses the
   submissions itself (skipping test rows). The bot must be a member of #events-feedback.
3. **Past revenue** — queried live from **Snowflake** for revenue tied to the partner/format.
   *Not enabled yet* — the assessment currently posts aspects 1–2 only; the revenue row
   appears automatically once Snowflake is configured (see below).

The message shows an overall score + GO/FLAG/PASS verdict and a one-sentence reason per
aspect. Aspects with no data score `n/a` (never penalized). It's idempotent — one
assessment per proposal thread. Each aspect degrades gracefully: if the feedback DBs aren't
shared with the integration, or Snowflake isn't configured, that aspect is skipped and the
score leans on the others.

**Triggers.** The bot assesses automatically when it detects a new proposal in the channel.
Anyone can also react **👀 (`:eyes:`)** on any message to assess it on demand.

**Self-learning.** Reply in an assessment thread (with or without @Event-Bot) to give the bot
insight — e.g. "score open mixers lower" or "this partner always over-promises." The bot logs
each reply as a *signal* to a Notion memory page. It deliberately **won't change its scoring
off a single comment**: only once the same theme recurs (default 3 times, `LEARN_THRESHOLD`)
does it distill those signals into a standing *guideline*, which is then injected into every
future assessment. It replies in-thread to say whether it's still gathering the pattern or has
adopted a new standard, and reacts 👍 to non-actionable replies. Set `ASSESSMENT_MEMORY_PAGE_ID`
to a blank Notion page shared with the integration; without it, feedback is acknowledged but
not persisted.

**Enabling the revenue aspect (Snowflake).** Uncomment `snowflake-connector-python` in
`requirements.txt`, redeploy, and set these as Replit secrets; without all of them the
revenue aspect stays off:

- `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, and either `SNOWFLAKE_PASSWORD` or
  `SNOWFLAKE_AUTHENTICATOR` (e.g. `SNOWFLAKE_JWT` with a key-pair).
- `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, `SNOWFLAKE_ROLE`.
- `REVENUE_SQL` — a `SELECT` using named binds `%(partner)s`, `%(event_type)s`,
  `%(city)s`, `%(term)s`. Returned rows (first 25) are summarized and handed to the model.
  Example (mirrors the Salesforce Opportunity event tagging):

  ```sql
  SELECT EVENT__C, STAGE_NAME, ORIGINATION_TOTAL_COMMITTED_DEPOSITS__C
  FROM   ANALYTICS.SALESFORCE.OPPORTUNITY
  WHERE  EVENT__C ILIKE '%' || %(term)s || '%'
  ORDER  BY CLOSE_DATE DESC
  LIMIT  25
  ```

The feedback aspect needs the bot invited to **#events-feedback** (`/invite @Event-Bot`);
it already has `channels:history`. Override the channel with `FEEDBACK_CHANNEL_ID` if needed.

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

Before posting, the bot scans the channels for a rundown already sent this week and skips
if found — so a restart/republish (which resets the in-memory schedule flag) can't cause a
duplicate post. The Drew reminder is likewise not re-sent if one already went out this week.
Editing the rundown does **not** re-notify already-tagged reps — only a newly added rep is
pinged — which is why changes edit in place rather than reposting.

`HOLD`/`[HOLD]` events are skipped. Rep names are mapped to Slack `@`-mentions via the
`REP_MAP_CSV` tab (name → Slack ID); unmapped names post as plain text. `/events-this-week`
shows the current rundown to whoever runs it (ephemeral).

**Any** rep-assignment change — a reply in the rundown thread, an @mention, or a DM —
updates Notion and then **edits the weekly rundown message in place in both channels** so
the single message always shows the current assignments. It never posts a new rundown; a
reply in the rundown thread posts nothing else, while an @mention/DM also gets a plain-text
confirmation. Works even after a restart (it re-finds the rundown message by content).

### Google Calendar sync
When the Monday rundown posts, the bot **clones each listed event** from Sean's personal
calendar (`sean.hu@rho.co`) to the shared **New York Event Calendar**, matching by date +
title, and **adds the assigned reps as guests** (emails from the rep sheet's email column).
The initial clone sends an invite; the event is stamped with its Notion page id so it can
be found later. When reps change in Notion (any @mention/DM/reply), the bot **updates the
cloned event's guest list with no email** (`sendUpdates=none`) — so reps aren't spammed.
It locates each event on the New York calendar by our Notion-page stamp *or* a same-day
title match, so it never duplicates. A copy that's already there but not created by the bot
(added manually / by Luma) is **adopted**: stamped and its guest list brought in line with
Notion, while any non-rep guests on it are preserved. Best-effort throughout (a calendar
failure never blocks the Slack/Notion flows). Requires the OAuth env vars below; skipped
without them.

`/gcal-sync` **actively reconciles** the New York calendar to this week's rundown and reports
what it did: clones missing events (guests + initial invite), aligns guest lists on events
already there — including adopting pre-existing copies — to the current Notion reps (silently,
no email), preserving any non-rep guests. Actions read like "Cloned X with 3 guests",
"Updated guests on Y (+Marc, -Joe)", "Adopted Z (already on the calendar) — guests set to 2".
The same reconcile runs with the Monday rundown; an already-aligned calendar reports "no
changes needed".

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

The conversation can continue **in-thread**: any reply in a thread where Event-Bot has
posted is read — with no new @mention needed (recognized even after a restart by reading
the thread) — and prior thread messages are given to Claude so "the one on the 24th" / "yes"
resolve.

If a rep says they **can't make an event but names no replacement**, the bot removes them and
asks *"Who will be taking <name>'s place?"*, listing the **available reps** (plain names, no
tags). The approver can just reply in the thread with a name to fill the slot.

Availability is checked against reps' **real Google Calendars** (free/busy) at the event's
time when a time is known — reps with a conflict are dropped; anyone whose free/busy is hidden
is assumed free. If no event time is available, it falls back to "not already booked that day"
from Notion. `/reps-availability` lists, for each of this week's events (with its time), which
active reps are free — the same check, on demand.

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
  (accept @mentions), **`groups:history`** (read/edit rundowns in the **private**
  #qualifiers-across-department channel — without it, edits there silently fail).
- Event subscriptions (bot events): `reaction_added`, `message.channels`,
  **`message.groups`** (replies in the private channel), **`app_mention`**, **`message.im`**.
- The bot must be a **member** of every channel it posts to (public *and* private).
- Slash commands created (Features → Slash Commands): **`/check-budget`**,
  **`/events-this-week`**, **`/my-event`**, **`/gcal-sync`**, **`/reps-availability`**.
  In Socket Mode no Request URL is needed.
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

## Google config (OAuth as Sean — Calendar + Drive)

Calendar sync needs the bot to act as a real user (service accounts can't invite guests);
the lead-list lookup needs Drive read access.
1. Enable the **Google Calendar API** and **Google Drive API**; use an OAuth
   **Client ID/Secret** (the one IT issued).
2. Run `get_google_token.py` **locally** once (signs in as `sean.hu@rho.co`, approves
   **calendar + drive.readonly**) → prints a **refresh token**. (If you minted a
   calendar-only token earlier, re-run to add Drive — lead lists are skipped until you do.)
3. Set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`.
4. Add an **email column** to the rep-map tab (rep name → Slack ID → email) for guests.
5. Sean must have edit access to the New York Event Calendar and read access to the
   **Enrichment OUTPUT** Drive folder (he's the one being acted as).

### Lead lists on new assignment
When a rep is **newly added** to an event (any change path), the bot searches the
Enrichment OUTPUT Drive folder for a matching lead-list file (by core event name, host
qualifiers stripped) and DMs the rep: *"Hi @Rep, here's the [Lead List](link) for <event>"*.
If no confident match is found, it skips silently.

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
