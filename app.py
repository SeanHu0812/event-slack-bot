import os, json, base64, csv, io, re, time, logging, threading
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
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

# Weekly rep-assignment rundown (Mondays 10:00 ET).
RUNDOWN_TZ = ZoneInfo("America/New_York")
RUNDOWN_CITY = "NYC"                     # scope: NYC only for now
RUNDOWN_HEADER = "_Events this week in NYC_ :statue_of_liberty:"
# Channel IDs to post the rundown to (bot must be invited to each).
# Defaults to #ny-vc-squad and #qualifiers-across-department; override with the env var.
RUNDOWN_CHANNELS = [c.strip() for c in os.environ.get(
    "RUNDOWN_CHANNELS", "C077WPGU528,C08KPMCU6P9").split(",") if c.strip()]
DREW_ID = "U037HBMJBHU"                  # reps-assignment owner, DMed when reps are missing
DONE_EMOJI = "done"                      # Drew reacts this to release the rundown
REPS_REMINDER_SENTINEL = "Reps assignment are missing"
MY_EVENTS_HORIZON_DAYS = 60              # how far ahead /my-event looks
ASSIGN_HORIZON_DAYS = 90                 # how far ahead @-mention reassignments can reach
BOT_USER_ID = None                       # resolved at startup, to ignore our own @mentions

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


def ask_json(prompt, max_tokens=700):
    """Call Claude and parse its reply as JSON. Returns {} on failure. Skips any
    non-text (e.g. extended-thinking) blocks and strips code fences."""
    out = claude.messages.create(
        model="claude-sonnet-5", max_tokens=max_tokens,
        system="You output only valid JSON. No prose, no markdown fences.",
        messages=[{"role": "user", "content": prompt}])
    raw = "".join(b.text for b in out.content if b.type == "text").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("could not parse model output as JSON: %r", raw)
        return {}


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
    return ask_json(prompt, max_tokens=500) or {"event": None}


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
# Weekly rep-assignment rundown
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+")


def first_url(text):
    """Extract the first URL from a messy field (invite links sometimes have
    trailing notes/emails appended)."""
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,);") if m else None


def rep_map():
    """name(lowercased) -> Slack user ID, from the published REP_MAP_CSV tab.
    The Slack-ID cell is detected by shape, so column order/headers don't matter."""
    url = os.environ.get("REP_MAP_CSV")
    if not url:
        return {}
    try:
        grid = _fetch_csv_grid(url)
    except Exception:
        log.exception("could not read REP_MAP_CSV")
        return {}
    m = {}
    for row in grid:
        sid = next((c.strip() for c in row if re.fullmatch(r"[UW][A-Z0-9]{6,}", c.strip())), None)
        if not sid:
            continue
        name = next((c.strip() for c in row if c.strip() and c.strip() != sid), None)
        if name:
            m[name.lower()] = sid
    return m


def rep_mention(name, mapping):
    """Slack <@ID> mention if the rep is mapped, else the plain name."""
    sid = mapping.get(name.strip().lower())
    return f"<@{sid}>" if sid else name


def week_range():
    """(monday, sunday) ISO dates for the current week in RUNDOWN_TZ."""
    today = datetime.now(RUNDOWN_TZ).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def _plain(rich):
    return "".join(t.get("plain_text", "") for t in (rich or []))


def fetch_week_events():
    """This week's NYC events (HOLD placeholders excluded), sorted by date."""
    start, end = week_range()
    r = notion.data_sources.query(
        data_source_id=data_source_id(),
        filter={"and": [
            {"property": "City", "select": {"equals": RUNDOWN_CITY}},
            {"property": "Date", "date": {"on_or_after": start}},
            {"property": "Date", "date": {"on_or_before": end}},
        ]},
        sorts=[{"property": "Date", "direction": "ascending"}])
    events = []
    for page in r["results"]:
        props = page["properties"]
        name = _plain(props["Event"]["title"]).strip()
        if name.upper().startswith("HOLD") or name.upper().startswith("[HOLD"):
            continue
        d = (props.get("Date") or {}).get("date") or {}
        if not d.get("start"):
            continue
        events.append({
            "event": name,
            "date": d["start"][:10],
            "invite": first_url(_plain(props.get("Invite Link", {}).get("rich_text"))),
            "reps": [o["name"] for o in props.get("Reps", {}).get("multi_select", [])],
            "url": page["url"],
        })
    events.sort(key=lambda e: e["date"])
    return events


def fmt_day(iso):
    dt = datetime.strptime(iso, "%Y-%m-%d")
    return f"{dt.strftime('%A, %B')} {dt.day}"


def build_rundown(events):
    """The weekly rundown message text (Slack mrkdwn)."""
    mapping = rep_map()
    by_day = {}
    for e in events:
        by_day.setdefault(e["date"], []).append(e)
    out = [RUNDOWN_HEADER, ""]
    for d in sorted(by_day):
        out.append(f"{fmt_day(d)}:")
        out.append("")
        for e in by_day[d]:
            link = f"<{e['invite']}|{e['event']}>" if e["invite"] else e["event"]
            reps = " ".join(rep_mention(r, mapping) for r in e["reps"])
            out.append(f"• {link}" + (f" - {reps}" if reps else ""))
        out.append("")
    return "\n".join(out).strip()


def post_rundown(client, events):
    text = build_rundown(events)
    for ch in RUNDOWN_CHANNELS:
        client.chat_postMessage(channel=ch, text=text)
    log.info("posted weekly rundown to %s (%d events)", RUNDOWN_CHANNELS, len(events))


def send_reps_reminder(client, missing):
    lines = [f"<@{DREW_ID}> Hi happy Monday! {REPS_REMINDER_SENTINEL} for the following events:"]
    for e in missing:
        lines.append(f"{fmt_day(e['date'])} — {e['event']} {e['url']}")
    lines.append("Please complete the assignment and react :done: below. Thanks!")
    dm = client.conversations_open(users=DREW_ID)["channel"]["id"]
    resp = client.chat_postMessage(channel=dm, text="\n".join(lines))
    try:
        client.reactions_add(channel=dm, timestamp=resp["ts"], name=DONE_EMOJI)
    except Exception:
        log.warning("could not add :%s: reaction (needs reactions:write / valid emoji)", DONE_EMOJI)
    log.info("DMed Drew a reps reminder for %d event(s)", len(missing))


def run_weekly_rundown(client):
    """Monday 10am job: post the rundown, or nudge Drew if reps are missing."""
    events = fetch_week_events()
    if not events:
        log.info("no %s events this week; skipping rundown", RUNDOWN_CITY)
        return
    missing = [e for e in events if not e["reps"]]
    if missing:
        send_reps_reminder(client, missing)
    else:
        post_rundown(client, events)


def handle_reps_done(client, channel, ts):
    """Drew reacted :done: on the reminder DM — post the rundown now."""
    msg = client.conversations_history(
        channel=channel, latest=ts, inclusive=True, limit=1)["messages"][0]
    if REPS_REMINDER_SENTINEL not in (msg.get("text") or ""):
        return                                    # not our reminder message
    events = fetch_week_events()
    if events:
        post_rundown(client, events)
    log.info("Drew confirmed reps; posted rundown")


def weekly_scheduler(client):
    """Fire run_weekly_rundown once each Monday during the 10:00 ET hour."""
    last = None
    while True:
        now = datetime.now(RUNDOWN_TZ)
        if now.weekday() == 0 and now.hour == 10 and now.date() != last:
            last = now.date()
            log.info("weekly rundown trigger firing")
            _bg(run_weekly_rundown, client)
        time.sleep(30)


# ---------------------------------------------------------------------------
# /my-event — a rep's own upcoming assignments
# ---------------------------------------------------------------------------

def _query_all(filt, sorts):
    """Query the events data source across all pages for a filter."""
    results, cursor = [], None
    while True:
        kwargs = {"data_source_id": data_source_id(), "filter": filt,
                  "sorts": sorts, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        r = notion.data_sources.query(**kwargs)
        results.extend(r["results"])
        if not r.get("has_more"):
            return results
        cursor = r.get("next_cursor")


def fetch_assigned_events(slack_user_id):
    """Upcoming events (any city, next MY_EVENTS_HORIZON_DAYS) assigned to this
    Slack user. Returns None if the user isn't in the rep map at all."""
    mapping = rep_map()
    my_names = {name for name, sid in mapping.items() if sid == slack_user_id}
    if not my_names:
        return None
    today = datetime.now(RUNDOWN_TZ).date()
    end = today + timedelta(days=MY_EVENTS_HORIZON_DAYS)
    pages = _query_all(
        {"and": [
            {"property": "Date", "date": {"on_or_after": today.isoformat()}},
            {"property": "Date", "date": {"on_or_before": end.isoformat()}},
        ]},
        [{"property": "Date", "direction": "ascending"}])
    events = []
    for page in pages:
        props = page["properties"]
        reps = [o["name"] for o in props.get("Reps", {}).get("multi_select", [])]
        if not any(r.strip().lower() in my_names for r in reps):
            continue
        name = _plain(props["Event"]["title"]).strip()
        if name.upper().startswith("HOLD") or name.upper().startswith("[HOLD"):
            continue
        d = (props.get("Date") or {}).get("date") or {}
        if not d.get("start"):
            continue
        events.append({
            "event": name,
            "date": d["start"][:10],
            "city": (props.get("City", {}).get("select") or {}).get("name"),
            "invite": first_url(_plain(props.get("Invite Link", {}).get("rich_text"))),
        })
    return events


def build_my_events(slack_user_id):
    events = fetch_assigned_events(slack_user_id)
    if events is None:
        return ("I don't have you mapped to a rep yet, so I can't look up your events. "
                "Ask an admin to add your name + Slack ID to the rep sheet.")
    if not events:
        return f"You have no events assigned in the next {MY_EVENTS_HORIZON_DAYS} days. :tada:"
    out = ["_Your upcoming events_ :calendar:", ""]
    for e in events:
        link = f"<{e['invite']}|{e['event']}>" if e["invite"] else e["event"]
        city = f" ({e['city']})" if e.get("city") else ""
        out.append(f"• {fmt_day(e['date'])}{city} — {link}")
    out.append("")
    out.append("Can no longer make it to an event? Tag me and let me know the change. Thank you!")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# @-mention / DM rep-assignment changes (writes to Notion)
# ---------------------------------------------------------------------------

_rep_options_cache = None
_bot_threads = set()             # (channel, thread_root_ts) the bot is conversing in


def thread_transcript(client, channel, thread_ts, msg_ts):
    """Prior messages in the thread (labelled rep/bot) so follow-ups have context.
    Empty for a brand-new thread (thread_ts == the current message)."""
    if thread_ts == msg_ts:
        return ""
    try:
        msgs = client.conversations_replies(channel=channel, ts=thread_ts, limit=30)["messages"]
    except Exception:
        return ""
    lines = []
    for m in msgs:
        if m.get("ts") == msg_ts:
            continue
        who = "bot" if (m.get("bot_id") or m.get("user") == BOT_USER_ID) else "rep"
        t = (m.get("text") or "").strip()
        if t:
            lines.append(f"{who}: {t}")
    return "\n".join(lines[-12:])


def valid_rep_options():
    """All names configured in the Notion Reps multi-select (cached).
    Used to keep reassignments from creating junk options (like City)."""
    global _rep_options_cache
    if _rep_options_cache is None:
        ds = notion.data_sources.retrieve(data_source_id=data_source_id())
        _rep_options_cache = [o["name"] for o in ds["properties"]["Reps"]["multi_select"]["options"]]
    return _rep_options_cache


def upcoming_events_for_change():
    """Upcoming events (any city, next ASSIGN_HORIZON_DAYS) that may be edited."""
    today = datetime.now(RUNDOWN_TZ).date()
    end = today + timedelta(days=ASSIGN_HORIZON_DAYS)
    pages = _query_all(
        {"and": [
            {"property": "Date", "date": {"on_or_after": today.isoformat()}},
            {"property": "Date", "date": {"on_or_before": end.isoformat()}},
        ]},
        [{"property": "Date", "direction": "ascending"}])
    evs = []
    for p in pages:
        pr = p["properties"]
        name = _plain(pr["Event"]["title"]).strip()
        if not name or name.upper().startswith("HOLD") or name.upper().startswith("[HOLD"):
            continue
        d = (pr.get("Date") or {}).get("date") or {}
        if not d.get("start"):
            continue
        evs.append({"id": p["id"], "event": name, "date": d["start"][:10],
                    "city": (pr.get("City", {}).get("select") or {}).get("name"),
                    "reps": [o["name"] for o in pr.get("Reps", {}).get("multi_select", [])]})
    return evs


def parse_mention(text, requester_names, events, valid_opts, context=""):
    """Classify a rep's message to the bot and produce response data:
    {intent: change|question|none, event_index, remove[], add[], answer}."""
    lines = [f"{i}: {e['date']} | {e.get('city') or '?'} | {e['event']} | "
             f"reps: {', '.join(e['reps']) or 'none'}" for i, e in enumerate(events)]
    who = ", ".join(sorted(requester_names)) if requester_names else "unknown (not in the rep list)"
    convo = f"CONVERSATION SO FAR (oldest first):\n{context}\n\n" if context else ""
    prompt = (
        "A Rho events rep sent a Slack message to the events bot. Decide what they want.\n"
        f"The sender is known in Notion as: {who}.\n"
        'Return ONLY JSON: {"intent": "change"|"question"|"none", "event_index": <int or '
        'null>, "remove": [<names>], "add": [<names>], "answer": <string>}.\n'
        "Intents:\n"
        "- \"change\": modify rep assignments for ONE event. Set event_index to that event's "
        "index below (null if unclear/ambiguous) and fill remove/add. Leave answer empty.\n"
        "- \"question\": they are ASKING about events or assignments (who is on an event, what "
        "someone is assigned to, how many, when, etc.). Put a concise Slack-formatted answer in "
        "'answer', computed ONLY from the UPCOMING EVENTS below; list events as "
        "'• <date> — <event>'. If nothing matches, say so plainly. Leave the change fields empty.\n"
        "- \"none\": anything else (greetings, chit-chat, unrelated). All fields empty.\n"
        "Change rules:\n"
        "- Use the conversation so far to resolve follow-ups ('the one on the 24th', 'yes').\n"
        "- remove: if the sender refers to themselves ('I','me','can't make it') include their "
        "Notion name(s); use names exactly as they appear in that event's current reps.\n"
        "- add: names EXACTLY as in VALID REPS; never invent a name.\n"
        "Notion rep names may be short forms (e.g. 'Lavar' == 'Lavar Buckmon'); match sensibly "
        "for both changes and questions.\n\n"
        f"VALID REPS: {valid_opts}\n\nUPCOMING EVENTS (index | date | city | event | reps):\n"
        + "\n".join(lines) + f"\n\n{convo}LATEST MESSAGE:\n{text}"
    )
    return ask_json(prompt)


def handle_mention(client, channel, thread_ts, msg_ts, user, text):
    text = re.sub(r"^\s*<@[A-Z0-9]+>\s*", "", text or "").strip()   # drop leading @bot
    if not text:
        return
    context = thread_transcript(client, channel, thread_ts, msg_ts)
    events = upcoming_events_for_change()
    valid = valid_rep_options()
    requester = {n for n, sid in rep_map().items() if sid == user}
    parsed = parse_mention(text, requester, events, valid, context)
    intent = parsed.get("intent")

    # Answering a question about events / assignments.
    if intent == "question":
        answer = (parsed.get("answer") or "").strip()
        if answer:
            _bot_threads.add((channel, thread_ts))
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=answer)
            log.info("answered assignment question from %s", user)
        else:
            log.info("question with no answer; staying silent")
        return

    # Anything that isn't a concrete change for a specific event -> stay silent.
    if intent != "change":
        log.info("mention/DM not actionable; no response")
        return
    idx = parsed.get("event_index")
    if not isinstance(idx, int) or not 0 <= idx < len(events):
        log.info("change request without a specific event; staying silent")
        return

    _bot_threads.add((channel, thread_ts))         # engaged — follow the rest of this thread
    ev = events[idx]
    current = ev["reps"]
    remove = {r.strip().lower() for r in parsed.get("remove", [])}
    by_lower = {v.lower(): v for v in valid}
    add, invalid = [], []
    for a in parsed.get("add", []):
        canon = a if a in valid else by_lower.get(a.strip().lower())
        (add if canon else invalid).append(canon or a)
    new = [r for r in current if r.strip().lower() not in remove]
    for a in add:
        if a not in new:
            new.append(a)

    if set(new) == set(current):
        note = f" (couldn't find in the rep list: {', '.join(invalid)})" if invalid else ""
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"No changes made to *{ev['event']}* ({fmt_day(ev['date'])})." + note)
        return

    notion.pages.update(page_id=ev["id"],
                        properties={"Reps": {"multi_select": [{"name": n} for n in new]}})
    mapping = rep_map()
    removed = [r for r in current if r.strip().lower() in remove]
    parts = [f":white_check_mark: Updated *{ev['event']}* ({fmt_day(ev['date'])})."]
    if removed:
        parts.append("Removed: " + ", ".join(rep_mention(r, mapping) for r in removed))
    if add:
        parts.append("Added: " + ", ".join(rep_mention(a, mapping) for a in add))
    parts.append("Reps now: " + (", ".join(rep_mention(n, mapping) for n in new) or "none"))
    if invalid:
        parts.append(f"Couldn't find in the rep list, skipped: {', '.join(invalid)}")
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="\n".join(parts))
    log.info("assignment change on %r by %s: -%s +%s", ev["event"], user, removed, add)


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
    channel, ts = item.get("channel"), item.get("ts")
    log.info("reaction_added: reaction=%r user=%r channel=%r type=%r",
             reaction, user, channel, item.get("type"))
    if item.get("type") != "message":
        return
    # Drew releasing the weekly rundown from the reminder DM.
    if reaction == DONE_EMOJI and user == DREW_ID:
        _bg(handle_reps_done, client, channel, ts)
        return
    # Event approvals / over-budget confirmations in #community-team.
    if channel != CHANNEL or user not in APPROVERS:
        return
    if reaction == APPROVE_EMOJI:
        _bg(handle_approval, client, ts)          # slow work off the dispatch path
    elif reaction == CONFIRM_EMOJI:
        _bg(handle_confirmation, client, ts)


@app.event("app_mention")
def on_app_mention(event, client):
    """A rep @-mentioned the bot with a rep-assignment change."""
    root = event.get("thread_ts") or event["ts"]
    _bg(handle_mention, client, event["channel"], root, event["ts"],
        event.get("user"), event.get("text", ""))


@app.event("message")
def on_message(event, client):
    if event.get("subtype") or event.get("bot_id"):
        return                                    # edits, joins, bot messages
    channel, user = event.get("channel"), event.get("user")
    ts, thread_ts = event.get("ts"), event.get("thread_ts")
    text = (event.get("text") or "").strip()

    # A DM to the bot is a rep-assignment change request (incl. thread replies).
    if event.get("channel_type") == "im":
        if len(text) >= 3:
            _bg(handle_mention, client, channel, thread_ts or ts, ts, user, text)
        return

    # Channel @mentions are handled by on_app_mention (avoid double-processing).
    if BOT_USER_ID and f"<@{BOT_USER_ID}>" in text:
        return
    # A reply in a thread the bot is already conversing in — continue it.
    if thread_ts:
        if (channel, thread_ts) in _bot_threads:
            _bg(handle_mention, client, channel, thread_ts, ts, user, text)
        return
    # Top-level community-channel message — proposal budget heads-up.
    if channel != CHANNEL:
        return
    if len(text) < 20:                            # skip chatter
        return
    _bg(_pre_approval_check, client, ts, text)


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


@app.command("/events-this-week")   # accept both spellings of the command name
@app.command("/event-this-week")
def cmd_events_week(ack, body, client):
    ack()
    _bg(_send_week_preview, client, body.get("channel_id"), body.get("user_id"))


def _send_week_preview(client, channel, user):
    events = fetch_week_events()
    text = build_rundown(events) if events else "No NYC events this week."
    try:
        client.chat_postEphemeral(channel=channel, user=user, text=text)
    except Exception:
        client.chat_postMessage(channel=user, text=text)
    log.info("posted /events-this-week preview for %s", user)


@app.command("/my-events")          # accept both spellings
@app.command("/my-event")
def cmd_my_events(ack, body, client):
    ack()
    _bg(_send_my_events, client, body.get("channel_id"), body.get("user_id"))


def _send_my_events(client, channel, user):
    text = build_my_events(user)
    try:
        client.chat_postEphemeral(channel=channel, user=user, text=text)
    except Exception:
        client.chat_postMessage(channel=user, text=text)
    log.info("posted /my-event for %s", user)


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
    try:
        BOT_USER_ID = app.client.auth_test()["user_id"]
        log.info("bot user id resolved: %s", BOT_USER_ID)
    except Exception:
        log.warning("could not resolve bot user id")
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=weekly_scheduler, args=(app.client,), daemon=True).start()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
