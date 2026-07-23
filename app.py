import os, json, base64, csv, io, logging, threading
import urllib.request
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
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
BUDGET_YEAR = 2026               # months offered by /check-budget
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


def _fetch_csv_grid(url):
    """Fetch a published-to-web CSV URL and return it as a list of rows."""
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = resp.read().decode("utf-8", "replace")
    return list(csv.reader(io.StringIO(data)))


def load_grid(tab):
    """Return a city tab as a 2D list of strings, from whichever backend is
    configured: a service account (preferred, if creds are present) or a
    published-to-web CSV URL in BUDGET_CSV_<TAB>. None if neither is set."""
    sheet = sheet_handle()
    if sheet is not None:
        return sheet.worksheet(tab).get_all_values()
    url = os.environ.get(f"BUDGET_CSV_{tab.upper()}")
    if url:
        return _fetch_csv_grid(url)
    return None


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


def budget_table(tab):
    """Return (monthly_budget, {month: {'estimated': float, 'actual': float}})
    for a city tab. Locates cells by content, so it is robust to the exact
    column the analysis panel sits in."""
    grid = load_grid(tab)
    if not grid:
        return None, {}

    # Monthly Budget: the value to the right of a "Monthly Budget" label cell.
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

    # The "Cost Analysis Per Month" table: month -> estimated / actual.
    months = {}
    for r_idx, row in enumerate(grid):
        if not any(cell.strip() == "Cost Analysis Per Month" for cell in row):
            continue
        header = grid[r_idx + 1] if r_idx + 1 < len(grid) else []
        col = {}
        for ci, cell in enumerate(header):
            t = cell.strip().lower()
            if t in ("month", "estimated", "actual"):
                col[t] = ci
        if "month" not in col or "estimated" not in col:
            continue
        for data in grid[r_idx + 2:]:
            if col["month"] >= len(data):
                continue
            m = data[col["month"]].strip()
            if not m:
                continue
            if m.upper() == "TOTAL":
                break
            est_ci, act_ci = col["estimated"], col.get("actual")
            est = _money(data[est_ci]) if est_ci < len(data) else None
            act = _money(data[act_ci]) if act_ci is not None and act_ci < len(data) else None
            months[m] = {"estimated": est or 0.0, "actual": act or 0.0}
        break
    return budget, months


def read_budget(tab, month):
    """(monthly_budget, estimated_for_month) — used by the live budget checks."""
    budget, months = budget_table(tab)
    if not budget:
        return None, None
    return budget, months.get(month, {}).get("estimated", 0.0)


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
# /check-budget slash command
# ---------------------------------------------------------------------------

def _month_key(m):
    try:
        return datetime.strptime(m, "%b %Y")
    except ValueError:
        return datetime.max


def build_budget_report(locations, months):
    """Assemble a spending report for the selected cities and months."""
    if not locations or not months:
        return "Please pick at least one location and one month."
    months = sorted(months, key=_month_key)
    lines = [":bar_chart: *Budget report*"]
    for city in locations:
        lines.append(f"\n*{city}*")
        tab = BUDGET_TABS.get(city)
        if not tab:
            lines.append("• no budget tracked for this location")
            continue
        budget, table = budget_table(tab)
        if not budget:
            lines.append("• couldn't read the budget sheet")
            continue
        tot_est = tot_act = 0.0
        for m in months:
            row = table.get(m, {})
            est, act = row.get("estimated", 0.0), row.get("actual", 0.0)
            tot_est += est
            tot_act += act
            lines.append(
                f"• {m} — Est ${est:,.0f} / ${budget:,.0f} ({est / budget * 100:.0f}%)"
                f" · Act ${act:,.0f} ({act / budget * 100:.0f}%)")
        if len(months) > 1:
            cap = budget * len(months)
            lines.append(
                f"  _Total ({len(months)} mo)_ — Est ${tot_est:,.0f} / ${cap:,.0f}"
                f" ({tot_est / cap * 100:.0f}%) · Act ${tot_act:,.0f}")
    return "\n".join(lines)


def _options(values):
    return [{"text": {"type": "plain_text", "text": v}, "value": v} for v in values]


def month_options():
    """Month choices for the modal. Network-free on purpose: a slash command's
    trigger_id expires in ~3s, so views.open must not wait on a sheet fetch.
    The report itself reads live sheet data at submission time."""
    labels = [datetime(BUDGET_YEAR, i, 1).strftime("%b %Y") for i in range(1, 13)]
    return _options(labels)


def check_budget_modal(channel_id):
    return {
        "type": "modal",
        "callback_id": "check_budget",
        "private_metadata": channel_id or "",
        "title": {"type": "plain_text", "text": "Check Budget"},
        "submit": {"type": "plain_text", "text": "Run report"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input", "block_id": "loc",
             "label": {"type": "plain_text", "text": "Location"},
             "element": {"type": "multi_static_select", "action_id": "v",
                         "placeholder": {"type": "plain_text", "text": "Select location(s)"},
                         "options": _options(list(BUDGET_TABS))}},
            {"type": "input", "block_id": "months",
             "label": {"type": "plain_text", "text": "Months"},
             "element": {"type": "multi_static_select", "action_id": "v",
                         "placeholder": {"type": "plain_text", "text": "Select month(s)"},
                         "options": month_options()}},
        ],
    }


# ---------------------------------------------------------------------------
# Slack handlers
# ---------------------------------------------------------------------------

def _bg(fn, *args):
    """Run slow work off the Socket Mode dispatch path, so a long-running handler
    never delays the next command/interaction. A blocked dispatch is what causes
    Slack's 'expired_trigger_id' (the 3s trigger window lapses before views.open)."""
    def run():
        try:
            fn(*args)
        except Exception:
            log.exception("background task %s failed", getattr(fn, "__name__", fn))
    threading.Thread(target=run, daemon=True).start()


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
    item = event.get("item", {})
    reaction, user = event.get("reaction"), event.get("user")
    log.info("reaction_added: reaction=%r user=%r channel=%r type=%r",
             reaction, user, item.get("channel"), item.get("type"))
    if item.get("channel") != CHANNEL or item.get("type") != "message":
        return
    if user not in APPROVERS:
        return
    ts = item["ts"]
    if reaction == APPROVE_EMOJI:
        _bg(handle_approval, client, ts)          # slow work off the dispatch path
    elif reaction == CONFIRM_EMOJI:
        _bg(handle_confirmation, client, ts)


@app.event("message")
def on_message(event, client):
    """Post a budget heads-up when a proposal is first sent in the channel."""
    if event.get("channel") != CHANNEL:
        return
    if event.get("subtype") or event.get("bot_id") or event.get("thread_ts"):
        return                                    # edits, joins, bot msgs, thread replies
    text = (event.get("text") or "").strip()
    if len(text) < 20:                            # skip chatter
        return
    _bg(_pre_approval_check, client, event["ts"], text)


def _pre_approval_check(client, ts, text):
    fields = parse_proposal(text)
    if not fields.get("event") or not fields.get("date"):
        return
    status = budget_status(fields)
    if not status:
        return
    client.chat_postMessage(channel=CHANNEL, thread_ts=ts, text=pre_approval_text(status))
    log.info("posted pre-approval %s warning for %s", status["band"], ts)


@app.command("/check-budget")
def cmd_check_budget(ack, body, client):
    ack()
    try:
        client.views_open(trigger_id=body["trigger_id"],
                          view=check_budget_modal(body.get("channel_id")))
    except Exception:
        log.exception("failed to open check-budget modal")


@app.view("check_budget")
def on_check_budget(ack, body, view, client):
    ack()                                         # close the modal immediately
    vals = view["state"]["values"]
    locations = [o["value"] for o in vals["loc"]["v"]["selected_options"]]
    months = [o["value"] for o in vals["months"]["v"]["selected_options"]]
    channel = view.get("private_metadata") or None
    user = body["user"]["id"]
    _bg(_send_budget_report, client, channel, user, locations, months)


def _send_budget_report(client, channel, user, locations, months):
    text = build_budget_report(locations, months)
    if channel:
        try:
            client.chat_postEphemeral(channel=channel, user=user, text=text)
            log.info("posted budget report for %s (%s / %s)", user, locations, months)
            return
        except Exception:
            log.warning("ephemeral post failed; DMing the report instead")
    client.chat_postMessage(channel=user, text=text)


class _Health(BaseHTTPRequestHandler):
    """Trivial 200-OK responder."""
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):    # silence per-request logging
        pass


def start_health_server():
    """Open a port so hosts that health-check for one (e.g. Replit Reserved VM)
    consider the app 'ready'. The bot itself talks to Slack over an outbound
    websocket and needs no inbound port; this server exists only for the check."""
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()


if __name__ == "__main__":
    log.info("events-bot starting (socket mode)")
    threading.Thread(target=start_health_server, daemon=True).start()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
