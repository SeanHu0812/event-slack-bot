import os, json, base64, logging
from datetime import date, datetime
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from notion_client import Client
from anthropic import Anthropic

try:
    import gspread
    from google.oauth2 import service_account
    _SHEETS_AVAILABLE = True
except ImportError:
    _SHEETS_AVAILABLE = False

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("events-bot")

CHANNEL       = "C08K7K31ML6"    # #community-team
APPROVE_EMOJI = "approved"       # Slack sends the bare name, no colons
CONFIRM_EMOJI = "white_check_mark"   # ✅ used to confirm an over-budget event
APPROVERS     = {"U03MEKGQPFC",  # Justin
                 "U0BER9VC6NA"}  # Sean
DB_ID         = "5acc7ada733042a3ace3433f828455b6"   # 2026 Events & Community Calendar

# Budget Google Sheet ("2026 Events" budget tracker).
SPREADSHEET_ID = "1F_IwoL1yixzOR0BVViII1LgBlYzVehO2s2rnbyfBCOc"
# Only these cities have a budget tab; map city -> worksheet (tab) title.
BUDGET_TABS = {"NYC": "NYC", "SF": "SF"}
WARN_THRESHOLD = 0.90            # 90-99% -> warning, >=100% -> over-budget
# Sentinel string embedded in the confirmation message so we can recognize it later.
CONFIRM_SENTINEL = "confirm and I'll create the Notion page"

VALID_CITIES = {"Atlanta", "Austin", "Boston", "Chicago", "Holiday", "LA/El Segundo",
    "Miami", "Montana", "NYC", "Nashville", "New Mexico", "Phoenix", "SF", "San Diego",
    "Seattle", "Vegas", "DC"}

app    = App(token=os.environ["SLACK_BOT_TOKEN"])
notion = Client(auth=os.environ["NOTION_TOKEN"])
claude = Anthropic()


def rt(text):
    """Notion rich_text property from a plain string."""
    return {"rich_text": [{"text": {"content": text or ""}}]}


def to_number(v):
    """Coerce a cost value into a plain number of dollars, or None.
    Handles shorthand like '$3k' -> 3000 and '2.5k' -> 2500 as a safety net
    in case the model returns a string instead of a number."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().lower().replace("$", "").replace(",", "")
    try:
        if s.endswith("k"):
            return float(s[:-1]) * 1000
        return float(s)
    except ValueError:
        return None


def parse_proposal(text):
    """Extract event fields from a free-text proposal. Returns a dict with an
    'event' key that is None when the message is not actually a proposal."""
    today = date.today().isoformat()
    prompt = (
        "You are parsing a Slack message that MAY be an event proposal. "
        f"Today's date is {today}. "
        "Return ONLY JSON with keys: event (string or null), date (YYYY-MM-DD or null), "
        "city, partner, cost, invite_link. "
        "For date: if the message gives a month/day with no year, choose the year that "
        "makes the date fall on or after today (i.e. the next upcoming occurrence), since "
        "proposals are for future events. If no date is given at all, use null. "
        "For cost: return a plain NUMBER of US dollars with no symbols or separators, "
        "converting shorthand (e.g. '$3k' -> 3000, '2.5k' -> 2500, '$1,200' -> 1200). "
        "Use null if no cost is given. "
        f"city must be exactly one of {sorted(VALID_CITIES)} or null; proposals often "
        "write things like 'SF Partnered' or 'NYC dinner', so normalize to the matching "
        "option. Use null (not a guess) when a field is absent. "
        "If the message is not an event proposal, set event to null.\n\n"
        f"MESSAGE:\n{text}"
    )
    out = claude.messages.create(
        model="claude-sonnet-5", max_tokens=500,
        system="You output only valid JSON. No prose, no markdown fences.",
        messages=[{"role": "user", "content": prompt}])
    # Skip any non-text blocks (e.g. extended-thinking blocks) and join text.
    raw = "".join(b.text for b in out.content if b.type == "text").strip()
    if raw.startswith("```"):                      # strip code fences if present
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("could not parse model output as JSON: %r", raw)
        return {"event": None}


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

_data_source_id = None


def data_source_id():
    """Resolve (once, cached) the DB's data-source ID. The Notion API (version
    2025-09-03) queries and parents pages by data source, not database."""
    global _data_source_id
    if _data_source_id is None:
        db = notion.databases.retrieve(database_id=DB_ID)
        _data_source_id = db["data_sources"][0]["id"]
        log.info("resolved data source %s for database %s", _data_source_id, DB_ID)
    return _data_source_id


def page_exists(ts):
    """Idempotency check: has this Slack message already been synced to Notion?"""
    r = notion.data_sources.query(data_source_id=data_source_id(), filter={
        "property": "Notes", "rich_text": {"contains": f"slack_ts:{ts}"}})
    return len(r["results"]) > 0


def create_notion_page(f, ts):
    props = {
        "Event": {"title": [{"text": {"content": f["event"]}}]},
        "Date":  {"date": {"start": f["date"]}},
        "Partner":     rt(f.get("partner")),
        # Proposal cost goes to "Estimated Cost" (a number field).
        # Never write to "Actual Cost" — that is filled in manually post-event.
        "Estimated Cost": {"number": to_number(f.get("cost"))},
        "Invite Link": rt(f.get("invite_link")),
        "Notes":       rt(f"slack_ts:{ts}"),
    }
    city = (f.get("city") or "").strip()
    if city in VALID_CITIES:
        props["City"] = {"select": {"name": city}}
    page = notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": data_source_id()},
        properties=props)
    return page["url"]


# ---------------------------------------------------------------------------
# Budget (Google Sheet)
# ---------------------------------------------------------------------------

_sheet = None


def sheet_handle():
    """Open (once, cached) the budget spreadsheet via a service account.
    Returns None if the sheets libs or GOOGLE_SERVICE_ACCOUNT_JSON are absent,
    so budget checks degrade to no-ops rather than breaking the bot."""
    global _sheet
    if _sheet is not None:
        return _sheet
    if not _SHEETS_AVAILABLE:
        return None
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    raw = raw.strip()
    info = json.loads(raw if raw.startswith("{") else base64.b64decode(raw))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    _sheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
    log.info("opened budget spreadsheet %s", SPREADSHEET_ID)
    return _sheet


def _money(s):
    """Parse a spreadsheet money cell like '$95,000.00' or '($2,642)' into a float."""
    s = (s or "").strip().replace("$", "").replace(",", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_budget(tab, month):
    """Read (monthly_budget, estimated_allocated_for_month) from a city tab.
    `month` is formatted like 'Sep 2026'. Locates cells by content so it is
    robust to the exact column the analysis panel sits in."""
    sheet = sheet_handle()
    if sheet is None:
        return None, None
    grid = sheet.worksheet(tab).get_all_values()

    # 1) Monthly Budget: the value to the right of a "Monthly Budget" label cell.
    budget = None
    for row in grid:
        for i, cell in enumerate(row):
            if cell.strip().lower().startswith("monthly budget"):
                for c in row[i + 1:]:
                    v = _money(c)
                    if v is not None:
                        budget = v
                        break
        if budget is not None:
            break

    # 2) Estimated for the month: from the "Cost Analysis Per Month" table.
    estimated = 0.0
    for r_idx, row in enumerate(grid):
        if not any(cell.strip() == "Cost Analysis Per Month" for cell in row):
            continue
        header = grid[r_idx + 1] if r_idx + 1 < len(grid) else []
        month_col = est_col = None
        for ci, cell in enumerate(header):
            t = cell.strip().lower()
            if t == "month":
                month_col = ci
            elif t == "estimated":
                est_col = ci
        if month_col is None or est_col is None:
            continue
        for data in grid[r_idx + 2:]:
            if month_col >= len(data):
                continue
            m = data[month_col].strip()
            if m == month:
                estimated = _money(data[est_col]) or 0.0
                break
            if m.upper() == "TOTAL":
                break
        break
    return budget, estimated


def budget_status(fields):
    """Given parsed proposal fields, return a dict describing budget impact,
    or None when no warning applies (city has no budget, no cost/date, under
    90%, or the sheet is unavailable)."""
    city = (fields.get("city") or "").strip()
    tab = BUDGET_TABS.get(city)
    if not tab:
        return None
    cost = to_number(fields.get("cost"))
    d = fields.get("date")
    if not cost or not d:
        return None
    try:
        month = datetime.strptime(d, "%Y-%m-%d").strftime("%b %Y")
    except ValueError:
        return None
    try:
        budget, estimated = read_budget(tab, month)
    except Exception:
        log.exception("budget read failed for %s %s", tab, month)
        return None
    if not budget:
        return None
    estimated = estimated or 0.0
    projected = estimated + cost
    frac = projected / budget
    if frac >= 1.0:
        band = "over"
    elif frac >= WARN_THRESHOLD:
        band = "warn"
    else:
        return None
    return {"city": city, "month": month, "budget": budget, "estimated": estimated,
            "proposed": cost, "projected": projected, "remaining": budget - projected,
            "pct": round(frac * 100), "band": band}


def pre_approval_text(s):
    """Warning posted when a proposal is first sent in the channel."""
    if s["band"] == "over":
        return (f":rotating_light: *WARNING:* This event will cause you to go over your "
                f"{s['month']} budget; ${s['projected']:,.0f} / ${s['budget']:,.0f}")
    return (f":warning: Warning: You've allocated {s['pct']}% of the budget for "
            f"{s['month']}; ${s['projected']:,.0f} / ${s['budget']:,.0f}")


def post_approval_text(s):
    """Warning posted after an event is approved and the page is created."""
    if s["band"] == "over":
        return (f":rotating_light: *WARNING:* Your budget allocation has gone over the "
                f"{s['month']} budget.")
    return (f":warning: Warning: You've allocated {s['pct']}% of the budget for "
            f"{s['month']}. You have ${s['remaining']:,.0f} left for this month.")


def confirmation_text(s):
    """Message posted (with a ✅) asking an approver to confirm an over-budget event."""
    return (f":rotating_light: *WARNING:* Approving this will put {s['city']} over the "
            f"{s['month']} budget (${s['projected']:,.0f} / ${s['budget']:,.0f}, {s['pct']}%).\n"
            f"React :white_check_mark: to {CONFIRM_SENTINEL} anyway.")


# ---------------------------------------------------------------------------
# Slack handlers
# ---------------------------------------------------------------------------

def create_and_reply(client, fields, ts, status):
    """Create the Notion page for a proposal and post the thread reply(s)."""
    url = create_notion_page(fields, ts)
    client.chat_postMessage(channel=CHANNEL, thread_ts=ts, text=f"Notion page created: {url}")
    log.info("created page for %s -> %s", ts, url)
    if status and status["band"] in ("warn", "over"):
        client.chat_postMessage(channel=CHANNEL, thread_ts=ts, text=post_approval_text(status))


def handle_approval(client, ts):
    """An approver reacted :approved: on the message at `ts`."""
    if page_exists(ts):
        log.info("skip: already synced %s", ts)
        return
    msg = client.conversations_history(
        channel=CHANNEL, latest=ts, inclusive=True, limit=1)["messages"][0]
    text = (msg.get("text") or "").strip()
    if not text:
        log.info("skip: reacted message has no text %s", ts)
        return
    fields = parse_proposal(text)
    if not fields.get("event"):
        log.info("skip: not a proposal %s", ts)
        return
    if not fields.get("date"):
        client.chat_postMessage(
            channel=CHANNEL, thread_ts=ts,
            text="Approved, but I couldn't find a date. Add this one to Notion manually.")
        log.info("no date, asked for manual entry %s", ts)
        return

    status = budget_status(fields)
    if status and status["band"] == "over":
        # Don't create yet — ask for an explicit ✅ confirmation.
        resp = client.chat_postMessage(
            channel=CHANNEL, thread_ts=ts, text=confirmation_text(status))
        try:
            client.reactions_add(channel=CHANNEL, timestamp=resp["ts"], name=CONFIRM_EMOJI)
        except Exception:
            log.warning("could not seed confirm reaction (needs reactions:write scope)")
        log.info("over budget, awaiting confirmation for %s", ts)
        return

    create_and_reply(client, fields, ts, status)


def handle_confirmation(client, confirm_ts):
    """An approver reacted ✅ on a confirmation message at `confirm_ts`.
    Walk back to the original proposal and create the page."""
    thread = client.conversations_replies(channel=CHANNEL, ts=confirm_ts)["messages"]
    if not thread:
        return
    conf = next((m for m in thread if m.get("ts") == confirm_ts), None)
    if not conf or CONFIRM_SENTINEL not in (conf.get("text") or ""):
        return  # not one of our confirmation messages
    proposal_ts = thread[0].get("ts")            # thread parent = original proposal
    if not proposal_ts:
        return
    if page_exists(proposal_ts):
        log.info("skip: already synced %s (confirmed)", proposal_ts)
        return
    text = (thread[0].get("text") or "").strip()
    fields = parse_proposal(text)
    if not fields.get("event") or not fields.get("date"):
        return
    log.info("confirmed over-budget event %s", proposal_ts)
    create_and_reply(client, fields, proposal_ts, budget_status(fields))


@app.event("reaction_added")
def on_reaction(event, client):
    try:
        item = event.get("item", {})
        reaction, user = event.get("reaction"), event.get("user")
        log.info("reaction_added: reaction=%r user=%r channel=%r type=%r",
                 reaction, user, item.get("channel"), item.get("type"))
        if item.get("channel") != CHANNEL or item.get("type") != "message":
            return
        if user not in APPROVERS:
            log.info("skip: user %r not in approvers", user)
            return
        ts = item["ts"]
        if reaction == APPROVE_EMOJI:
            handle_approval(client, ts)
        elif reaction == CONFIRM_EMOJI:
            handle_confirmation(client, ts)
    except Exception:
        log.exception("failed handling reaction on %s", event.get("item", {}).get("ts"))


@app.event("message")
def on_message(event, client):
    """Post a budget heads-up when a proposal is first sent in the channel."""
    try:
        if event.get("channel") != CHANNEL:
            return
        if event.get("subtype") or event.get("bot_id"):
            return                                # edits, joins, bot messages
        if event.get("thread_ts"):
            return                                # only top-level messages
        text = (event.get("text") or "").strip()
        if len(text) < 20:                        # skip chatter
            return
        fields = parse_proposal(text)
        if not fields.get("event") or not fields.get("date"):
            return
        status = budget_status(fields)
        if not status:
            return
        client.chat_postMessage(
            channel=CHANNEL, thread_ts=event["ts"], text=pre_approval_text(status))
        log.info("posted pre-approval %s warning for %s", status["band"], event["ts"])
    except Exception:
        log.exception("failed handling message %s", event.get("ts"))


if __name__ == "__main__":
    log.info("events-bot starting (socket mode)")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
