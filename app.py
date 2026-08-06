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

try:
    from googleapiclient.discovery import build as gcal_build
    from google.oauth2.credentials import Credentials as OAuthCredentials
    _CALENDAR_AVAILABLE = True
except ImportError:
    _CALENDAR_AVAILABLE = False

try:
    import snowflake.connector as snowflake_connector
    _SNOWFLAKE_AVAILABLE = True
except ImportError:
    _SNOWFLAKE_AVAILABLE = False

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("events-bot")

CHANNEL       = "C08K7K31ML6"    # #community-team
APPROVE_EMOJI = "approved"       # Slack sends the bare name, no colons
CONFIRM_EMOJI = "white_check_mark"   # ✅ used to confirm an over-budget event
DELETE_EMOJI = "wastebasket"     # 🗑️ an approver reacts to delete one of the bot's own messages
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

# Weekly rep-assignment rundown. Sent Mondays 10:00 ET, covering the current week.
RUNDOWN_TZ = ZoneInfo("America/New_York")
RUNDOWN_WEEKDAY = 0                      # 0=Mon .. 4=Fri — the day the rundown is sent
RUNDOWN_CITY = "NYC"                     # scope: NYC only for now
RUNDOWN_HEADER = "_Events this week in NYC_ :statue_of_liberty:"
RUNDOWN_SENTINEL = "Events this week in NYC"   # to recognize a rundown message
RUNDOWN_REPLY_LINE = "Can no longer make it to an event? Tag me and let me know. Thanks!"
_rundown_msgs = set()                    # (channel, ts) of the latest rundown posts

# Google Calendar: clone rundown events from Sean's personal calendar to the shared one.
CAL_SOURCE = "sean.hu@rho.co"            # personal calendar events are cloned FROM
CAL_TARGET = ("c_a6779362659cf757210d14e15b7010a789e7c861a40c61957bb120527c5d550a"
              "@group.calendar.google.com")   # New York Event Calendar
ENRICHMENT_FOLDER_ID = "1pgMUAiBOOVFMleeGuPSFKi5-IXyjNpCB"   # Drive: Enrichment OUTPUT (lead lists)
# Channel IDs to post the rundown to (bot must be invited to each).
# Defaults to #ny-vc-squad and #qualifiers-across-department; override with the env var.
RUNDOWN_CHANNELS = [c.strip() for c in os.environ.get(
    "RUNDOWN_CHANNELS", "C077WPGU528,C08KPMCU6P9").split(",") if c.strip()]
REPS_ALERT_USER = "U0BER9VC6NA"          # Sean — DMed (FYI) when events are missing reps
REPS_REMINDER_SENTINEL = "Reps assignment are missing"
MY_EVENTS_HORIZON_DAYS = 60              # how far ahead /my-event looks
ASSIGN_HORIZON_DAYS = 90                 # how far ahead @-mention reassignments can reach
BOT_USER_ID = None                       # resolved at startup, to ignore our own @mentions

# --- Auto-assessment of event proposals -------------------------------------
# When a proposal is posted in #community-team, the bot scores it 1-10 across
# three aspects: past feedback, business-goal fit, and past revenue.
# Feedback is read straight from the #events-feedback channel (Tally form
# submissions) — the source-of-truth superset; the synced Notion DB was lossy.
# The bot must be a member of this channel (channels:history).
FEEDBACK_CHANNEL = os.environ.get("FEEDBACK_CHANNEL_ID", "C092PD0RD4L")
FEEDBACK_HISTORY_PAGES = 4               # ~400 most-recent messages scanned
ASSESS_SENTINEL = "Event assessment"     # header text; also used to skip re-assessing
ASSESS_MIN_LEN = 40                      # ignore short chatter; proposals are longer
ASSESS_EMOJI = "eyes"                    # 👀 — manually trigger an assessment on any message

# Self-learning: people reply in an assessment thread with insight; the bot logs
# each signal to a Notion "memory" page and, once a theme recurs, distills it into
# a guideline that gets injected into future assessments. Set ASSESSMENT_MEMORY_PAGE_ID
# to a blank Notion page shared with the integration; without it, learning is disabled.
MEMORY_PAGE_ID = os.environ.get("ASSESSMENT_MEMORY_PAGE_ID")
LEARN_THRESHOLD = 3                      # signals on one theme before it becomes a guideline
_assessment_threads = set()              # (channel, proposal_ts) where we posted an assessment

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


# Model IDs are hardcoded on purpose. They are NOT read from env — a stale/bad
# env override (e.g. FAST_MODEL=claude-3-5-haiku-latest, which this key can't use)
# was causing 404s. To change a model, edit it here.
PARSE_MODEL = "claude-sonnet-5"                  # accurate default
FAST_MODEL = "claude-haiku-4-5-20251001"         # faster/cheaper


def ask_json(prompt, max_tokens=700, model=PARSE_MODEL):
    """Call Claude and parse its reply as JSON. Returns {} on any failure (API
    error or unparseable output) so a hiccup degrades a feature rather than
    crashing the handler. If a non-default model fails (e.g. FAST_MODEL is
    deprecated/unavailable), transparently retry with PARSE_MODEL so features keep
    working. Skips non-text blocks and strips code fences."""
    def _create(mdl):
        return claude.messages.create(
            model=mdl, max_tokens=max_tokens,
            system="You output only valid JSON. No prose, no markdown fences.",
            messages=[{"role": "user", "content": prompt}])
    try:
        out = _create(model)
    except Exception:
        log.exception("Claude API call failed (model=%s)", model)
        if model == PARSE_MODEL:
            return {}
        log.info("retrying with fallback model %s", PARSE_MODEL)
        try:
            out = _create(PARSE_MODEL)
        except Exception:
            log.exception("fallback Claude call also failed (model=%s)", PARSE_MODEL)
            return {}
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
    invalidate_week_cache()
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


_csv_cache = {}                                    # url -> (fetched_monotonic, grid)
_CSV_TTL = 60                                      # seconds; rep/budget sheets change rarely


def _fetch_csv_grid(url):
    """Fetch a published-to-web CSV URL as a list of rows, cached for _CSV_TTL so
    repeated rep_map()/rep_emails() calls within one action don't re-download it."""
    hit = _csv_cache.get(url)
    if hit and time.monotonic() - hit[0] < _CSV_TTL:
        return hit[1]
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = resp.read().decode("utf-8", "replace")
    grid = list(csv.reader(io.StringIO(data)))
    _csv_cache[url] = (time.monotonic(), grid)
    return grid


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


def rundown_monday():
    """Monday (date) of the week the rundown describes — the current week."""
    today = datetime.now(RUNDOWN_TZ).date()
    return today - timedelta(days=today.weekday())


def week_range():
    """(monday, sunday) ISO dates for the current week in RUNDOWN_TZ."""
    monday = rundown_monday()
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def _plain(rich):
    return "".join(t.get("plain_text", "") for t in (rich or []))


_week_cache = None                               # (fetched_monotonic, events)
_WEEK_TTL = 20                                   # short: dedupes repeated reads within one action


def invalidate_week_cache():
    """Drop the this-week events cache — call right after any Notion write so the
    next read reflects the change (e.g. the rundown edit after a rep change)."""
    global _week_cache
    _week_cache = None


def fetch_week_events():
    """This week's NYC events (HOLD placeholders excluded), sorted by date.
    Cached briefly so a single change doesn't re-query Notion 2-3 times."""
    global _week_cache
    if _week_cache and time.monotonic() - _week_cache[0] < _WEEK_TTL:
        return _week_cache[1]
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
            "id": page["id"],
            "event": name,
            "date": d["start"][:10],
            "invite": first_url(_plain(props.get("Invite Link", {}).get("rich_text"))),
            "reps": [o["name"] for o in props.get("Reps", {}).get("multi_select", [])],
            "url": page["url"],
        })
    events.sort(key=lambda e: e["date"])
    _week_cache = (time.monotonic(), events)
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
    out.append(RUNDOWN_REPLY_LINE)
    return "\n".join(out).strip()


def cycle_start_epoch():
    """Unix ts for 00:00 ET on this week's Monday — bounds the dedup/edit scans to
    the current week."""
    monday = rundown_monday()
    return datetime(monday.year, monday.month, monday.day, tzinfo=RUNDOWN_TZ).timestamp()


def rundown_posted_this_week(client):
    """Scan the rundown channels for a rundown message already posted this week
    (so a restart/republish doesn't cause a duplicate post)."""
    start = cycle_start_epoch()
    for ch in RUNDOWN_CHANNELS:
        try:
            msgs = client.conversations_history(channel=ch, limit=100)["messages"]
        except Exception:
            continue
        for m in msgs:
            if (m.get("bot_id") or m.get("user") == BOT_USER_ID) \
                    and RUNDOWN_SENTINEL in (m.get("text") or "") \
                    and float(m.get("ts", 0)) >= start:
                return True
    return False


def reminder_sent_this_week(client):
    """Has the missing-reps FYI already been DMed this week?"""
    try:
        dm = client.conversations_open(users=REPS_ALERT_USER)["channel"]["id"]
        msgs = client.conversations_history(channel=dm, limit=50)["messages"]
    except Exception:
        return False
    start = cycle_start_epoch()
    return any(REPS_REMINDER_SENTINEL in (m.get("text") or "") and float(m.get("ts", 0)) >= start
               for m in msgs)


def post_rundown(client, events):
    if rundown_posted_this_week(client):
        log.info("rundown already posted this week; not re-posting")
        return
    text = build_rundown(events)
    _rundown_msgs.clear()                          # track this week's rundown messages
    for ch in RUNDOWN_CHANNELS:
        try:
            resp = client.chat_postMessage(channel=ch, text=text)
            _rundown_msgs.add((ch, resp["ts"]))
        except Exception:
            log.exception("failed to post rundown to %s", ch)
    log.info("posted weekly rundown to %s (%d events)", RUNDOWN_CHANNELS, len(events))
    try:
        reconcile_calendar()                       # mirror this week's events to gcal
    except Exception:
        log.exception("weekly calendar sync failed")


def rundown_ts_all(client, channel):
    """Every rundown message the bot posted THIS WEEK in a channel — from memory
    plus a history scan — so duplicate posts all stay in sync. Logs read errors
    (missing scope / not in channel) instead of hiding them."""
    found = {ts for ch, ts in _rundown_msgs if ch == channel}
    start = cycle_start_epoch()
    try:
        msgs = client.conversations_history(channel=channel, limit=200)["messages"]
    except Exception:
        log.exception("couldn't read history of %s (bot not in channel? missing scope?)", channel)
        return sorted(found)
    for m in msgs:
        if (m.get("bot_id") or m.get("user") == BOT_USER_ID) \
                and RUNDOWN_SENTINEL in (m.get("text") or "") \
                and float(m.get("ts", 0)) >= start:
            found.add(m["ts"])
    if not found:
        log.warning("no rundown message found in %s (scanned %d msgs this week)", channel, len(msgs))
    return sorted(found)


def edit_rundowns(client):
    """Regenerate the rundown from current Notion data and edit EVERY rundown
    message this week in each channel in place, so all copies stay in sync."""
    text = build_rundown(fetch_week_events())
    edited = 0
    for ch in RUNDOWN_CHANNELS:
        for ts in rundown_ts_all(client, ch):
            try:
                client.chat_update(channel=ch, ts=ts, text=text)
                edited += 1
            except Exception:
                log.exception("failed to update rundown %s/%s", ch, ts)
    log.info("edited %d rundown message(s) across %s", edited, RUNDOWN_CHANNELS)


def send_reps_reminder(client, missing):
    """FYI DM to Sean listing this week's events that still have no reps."""
    lines = [f"Hi! {REPS_REMINDER_SENTINEL} for the following events this week:"]
    for e in missing:
        lines.append(f"{fmt_day(e['date'])} — {e['event']} {e['url']}")
    lines.append("The rundown has been posted; assign reps and it'll update. Thanks!")
    dm = client.conversations_open(users=REPS_ALERT_USER)["channel"]["id"]
    client.chat_postMessage(channel=dm, text="\n".join(lines))
    log.info("DMed the missing-reps FYI for %d event(s)", len(missing))


# ---------------------------------------------------------------------------
# Google Calendar sync (clone rundown events + keep guests in step with Notion)
# ---------------------------------------------------------------------------

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar",
                 "https://www.googleapis.com/auth/drive.readonly"]
_google_creds = _calendar = _drive = None


def google_creds():
    """OAuth credentials acting as Sean (shared by Calendar + Drive). None if the
    libs or OAuth env vars aren't configured."""
    global _google_creds
    if _google_creds is None:
        if not _CALENDAR_AVAILABLE:
            return None
        cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
        csec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
        rtok = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
        if not (cid and csec and rtok):
            return None
        _google_creds = OAuthCredentials(
            None, refresh_token=rtok, client_id=cid, client_secret=csec,
            token_uri="https://oauth2.googleapis.com/token", scopes=GOOGLE_SCOPES)
    return _google_creds


def calendar():
    """Google Calendar client (acting as Sean — a service account can't invite guests)."""
    global _calendar
    if _calendar is None:
        creds = google_creds()
        if creds is None:
            return None
        _calendar = gcal_build("calendar", "v3", credentials=creds, cache_discovery=False)
        log.info("google calendar client ready (acting as %s)", CAL_SOURCE)
    return _calendar


def drive():
    """Google Drive client (read-only) for looking up lead lists. Needs the
    drive.readonly scope on the OAuth token (re-mint if it was calendar-only)."""
    global _drive
    if _drive is None:
        creds = google_creds()
        if creds is None:
            return None
        _drive = gcal_build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive


_LEAD_STOP = {"the", "and", "x", "hosted", "by", "for", "with", "rho", "event", "events",
              "lead", "list", "leadlist", "enriched", "enrichment", "final", "copy", "of",
              "xlsx", "csv", "sheet", "nyc"}


def _lead_tokens(s):
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _LEAD_STOP and len(t) > 2}


def find_lead_list(event_name):
    """Best-matching lead-list file in the Enrichment OUTPUT folder, or None.
    Matches by name-token overlap and only returns a confident match."""
    d = drive()
    if d is None:
        return None
    try:
        files = d.files().list(
            q=f"'{ENRICHMENT_FOLDER_ID}' in parents and trashed=false",
            fields="files(id,name,mimeType,webViewLink)", pageSize=1000,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", [])
    except Exception:
        log.exception("drive lead-list lookup failed (drive.readonly scope? folder shared?)")
        return None
    # Match on the core event name — strip "[Hosted by ...]" / "(...)" host qualifiers,
    # which lead-list file names omit and which would otherwise dilute the overlap.
    core = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", event_name)
    et = _lead_tokens(core)
    if not et:
        return None
    best, best_score = None, 0.0
    for f in files:
        score = len(et & _lead_tokens(f["name"])) / len(et)
        if score > best_score:
            best, best_score = f, score
    return best if best and best_score >= 0.5 else None


def send_lead_list(client, rep_name, event_name, invite=None):
    """DM a newly-assigned rep the event's lead list (if one exists). Links the event
    name to its invite page when available. Skips silently when there's no list."""
    sid = rep_map().get(rep_name.strip().lower())
    if not sid:
        return
    lead = find_lead_list(event_name)
    link = (lead or {}).get("webViewLink")
    if not link:
        return
    event_ref = f"<{invite}|{event_name}>" if invite else event_name
    try:
        dm = client.conversations_open(users=sid)["channel"]["id"]
        client.chat_postMessage(
            channel=dm, text=f"Hi <@{sid}>, here's the <{link}|Lead List> for {event_ref}")
        log.info("sent lead list to %s for %r", rep_name, event_name)
    except Exception:
        log.exception("failed to send lead list to %s", rep_name)


def rep_emails():
    """name(lowercased) -> email, from the REP_MAP_CSV tab's email column."""
    url = os.environ.get("REP_MAP_CSV")
    if not url:
        return {}
    try:
        grid = _fetch_csv_grid(url)
    except Exception:
        return {}
    m = {}
    for row in grid:
        email = next((c.strip() for c in row if "@" in c and "." in c.split("@")[-1]), None)
        if not email:
            continue
        name = next((c.strip() for c in row if c.strip() and c.strip() != email
                     and not re.fullmatch(r"[UW][A-Z0-9]{6,}", c.strip())), None)
        if name:
            m[name.lower()] = email
    return m


def _rep_attendees(rep_names, emails):
    return [{"email": emails[r.strip().lower()]} for r in rep_names
            if r.strip().lower() in emails]


def find_clone(page_id):
    """The New-York-calendar event cloned from a given Notion page, or None.
    Located by the notionPageId we stamp into the event's private metadata."""
    cal = calendar()
    if cal is None:
        return None
    try:
        items = cal.events().list(
            calendarId=CAL_TARGET, privateExtendedProperty=f"notionPageId={page_id}",
            showDeleted=False, maxResults=1).execute().get("items", [])
    except Exception:
        log.exception("calendar lookup failed for %s", page_id)
        return None
    return items[0] if items else None


def move_calendar_date(page_id, new_date):
    """Move this page's New-York-calendar clone to new_date (YYYY-MM-DD), keeping
    its time-of-day and duration. Silent (no guest email). Returns True if moved."""
    cal = calendar()
    if cal is None:
        return False
    clone = find_clone(page_id)
    if not clone:
        return False
    start, end = clone.get("start", {}), clone.get("end", {})
    try:
        nd = date.fromisoformat(new_date)
    except ValueError:
        return False
    if start.get("dateTime"):                        # timed event: keep time + duration
        s = datetime.fromisoformat(start["dateTime"])
        e = datetime.fromisoformat(end["dateTime"]) if end.get("dateTime") else s
        dur = e - s
        ns = s.replace(year=nd.year, month=nd.month, day=nd.day)
        new_start = {"dateTime": ns.isoformat()}
        new_end = {"dateTime": (ns + dur).isoformat()}
        if start.get("timeZone"):
            new_start["timeZone"] = start["timeZone"]
        if end.get("timeZone"):
            new_end["timeZone"] = end["timeZone"]
    else:                                            # all-day: keep the span length
        os_ = date.fromisoformat(start["date"]) if start.get("date") else nd
        oe_ = date.fromisoformat(end["date"]) if end.get("date") else (nd + timedelta(days=1))
        span = (oe_ - os_) or timedelta(days=1)
        new_start = {"date": nd.isoformat()}
        new_end = {"date": (nd + span).isoformat()}
    try:
        cal.events().patch(calendarId=CAL_TARGET, eventId=clone["id"],
                           body={"start": new_start, "end": new_end},
                           sendUpdates="none").execute()
        return True
    except Exception:
        log.exception("failed to move calendar clone for %s", page_id)
        return False


def rename_calendar_clone(page_id, new_name):
    """Rename this page's calendar clone's summary. Silent. Returns True if renamed."""
    cal = calendar()
    if cal is None:
        return False
    clone = find_clone(page_id)
    if not clone:
        return False
    try:
        cal.events().patch(calendarId=CAL_TARGET, eventId=clone["id"],
                           body={"summary": new_name}, sendUpdates="none").execute()
        return True
    except Exception:
        log.exception("failed to rename calendar clone for %s", page_id)
        return False


def _norm(s):
    return " ".join((s or "").split()).strip().lower()


def _ev_date(ev):
    s = ev.get("start", {})
    return (s.get("dateTime") or s.get("date") or "")[:10]


def _match_calendar_sources(events, cal_events):
    """Map each rundown event to the same-day personal-calendar event it clones
    from (titles differ, so let the model align them). Returns {rundown_idx: cal_idx}."""
    if not events or not cal_events:
        return {}
    r_lines = [f"R{i}: {e['date']} | {e['event']}" for i, e in enumerate(events)]
    c_lines = [f"C{j}: {(e.get('start', {}).get('dateTime') or '')[:10]} | {e.get('summary', '')}"
               for j, e in enumerate(cal_events)]
    out = ask_json(
        "Match each rundown event (R#) to the calendar event (C#) that is the SAME event "
        "(same date; titles may be worded differently). Return ONLY JSON "
        '{"matches": {"R0": <C index or null>, ...}}. Only match same-day events.\n\n'
        "RUNDOWN:\n" + "\n".join(r_lines) + "\n\nCALENDAR:\n" + "\n".join(c_lines),
        max_tokens=500)
    result = {}
    for k, v in (out.get("matches") or {}).items():
        try:
            result[int(k[1:])] = int(v) if v is not None else None
        except (ValueError, TypeError):
            pass
    return result


def reconcile_calendar():
    """Make the New York calendar match this week's rundown (Notion = source of truth):
    clone missing events (guests + invite), and align guest lists on events already there —
    including pre-existing copies the bot didn't create, which it adopts (stamps) and manages
    while preserving any non-rep guests. Guest changes are silent (no email). Returns a list
    of action strings, or None if the calendar isn't configured."""
    cal = calendar()
    if cal is None:
        return None
    events = fetch_week_events()
    if not events:
        return []
    emails = rep_emails()
    known = {v.strip().lower() for v in emails.values()}
    monday, sunday = week_range()
    tmin = datetime.fromisoformat(monday).replace(tzinfo=RUNDOWN_TZ).isoformat()
    tmax = (datetime.fromisoformat(sunday) + timedelta(days=1)).replace(tzinfo=RUNDOWN_TZ).isoformat()

    # Everything already on the New York calendar this week (one read).
    try:
        target = cal.events().list(
            calendarId=CAL_TARGET, timeMin=tmin, timeMax=tmax,
            singleEvents=True, maxResults=250).execute().get("items", [])
    except Exception:
        log.exception("could not read the New York calendar")
        return None
    by_stamp = {}
    for ev in target:
        pid = ((ev.get("extendedProperties") or {}).get("private") or {}).get("notionPageId")
        if pid:
            by_stamp[pid] = ev

    # Personal-calendar sources — only needed when something isn't already on the target.
    src_events, matches = [], {}
    if any(e["id"] not in by_stamp for e in events):
        try:
            src_events = cal.events().list(
                calendarId=CAL_SOURCE, timeMin=tmin, timeMax=tmax,
                singleEvents=True, orderBy="startTime", maxResults=250).execute().get("items", [])
        except Exception:
            log.exception("could not read the personal calendar")
        src_events = [s for s in src_events if s.get("start", {}).get("dateTime")]
        matches = _match_calendar_sources(events, src_events)

    actions = []
    for i, e in enumerate(events):
        # Desired rep guests (reps that have an email in the sheet), + display names.
        name_of, desired = {}, []
        for r in e["reps"]:
            em = emails.get(r.strip().lower())
            if em:
                desired.append({"email": em})
                name_of[em.strip().lower()] = r
        desired_set = set(name_of)

        # Locate the copy on the target: by our stamp, else by a same-day title match.
        clone, adopt = by_stamp.get(e["id"]), False
        if clone is None:
            titles = {_norm(e["event"])}
            ci = matches.get(i)
            if ci is not None and 0 <= ci < len(src_events):
                titles.add(_norm(src_events[ci].get("summary")))
            clone = next((ev for ev in target
                          if _ev_date(ev) == e["date"] and _norm(ev.get("summary")) in titles), None)
            adopt = clone is not None

        if clone is not None:                            # on the calendar -> align guests
            existing = clone.get("attendees", [])
            existing_reps = {a["email"].strip().lower() for a in existing
                             if a.get("email")} & known
            nonreps = [a for a in existing
                       if a.get("email") and a["email"].strip().lower() not in known]
            if existing_reps == desired_set and not adopt:
                continue                                 # already in sync and already linked
            body = {"attendees": nonreps + desired}      # keep non-rep guests; set reps to Notion
            if adopt:                                    # stamp it so future syncs find it fast
                priv = dict((clone.get("extendedProperties") or {}).get("private") or {})
                priv["notionPageId"] = e["id"]
                body["extendedProperties"] = {"private": priv}
            try:
                cal.events().patch(calendarId=CAL_TARGET, eventId=clone["id"],
                                   body=body, sendUpdates="none").execute()
            except Exception:
                log.exception("failed to update %r on the calendar", e["event"])
                actions.append(f":x: Couldn't update *{e['event']}*")
                continue
            if adopt:
                actions.append(f":link: Adopted *{e['event']}* (already on the calendar) — "
                               f"guests set to {len(desired)}")
            else:
                bits = []
                if desired_set - existing_reps:
                    bits.append("+" + ", ".join(sorted(name_of[m] for m in desired_set - existing_reps)))
                if existing_reps - desired_set:
                    bits.append("-" + ", ".join(sorted(existing_reps - desired_set)))
                actions.append(f":arrows_counterclockwise: Updated guests on *{e['event']}* "
                               f"({'; '.join(bits)})")
            continue

        # Not on the target at all -> clone from the personal calendar.
        ci = matches.get(i)
        if ci is None or not 0 <= ci < len(src_events):
            actions.append(f":warning: No calendar source found for *{e['event']}* — not cloned")
            continue
        src = src_events[ci]
        body = {
            "summary": src.get("summary"), "start": src.get("start"), "end": src.get("end"),
            "location": src.get("location"), "description": src.get("description"),
            "attendees": desired,
            "extendedProperties": {"private": {"notionPageId": e["id"]}},
        }
        try:
            cal.events().insert(calendarId=CAL_TARGET, body=body, sendUpdates="all").execute()
            actions.append(f":new: Cloned *{e['event']}* with {len(desired)} guest(s)")
        except Exception:
            log.exception("failed to clone %r", e["event"])
            actions.append(f":x: Couldn't clone *{e['event']}*")
    return actions


def run_gcal_sync():
    """Reconcile the calendar and return a Slack message of the actions taken."""
    actions = reconcile_calendar()
    if actions is None:
        return "Google Calendar isn't configured (missing OAuth secrets), so I can't sync."
    if not actions:
        return ":white_check_mark: Calendar already matches this week's rundown — no changes needed."
    return ":calendar: *Calendar synced — here's what I did:*\n" + "\n".join(actions)


def update_calendar_guests(page_id, rep_names):
    """Sync a cloned event's guest list to Notion's reps — silently (no emails)."""
    ev = find_clone(page_id)
    if not ev:
        return                                     # event was never cloned; nothing to do
    attendees = _rep_attendees(rep_names, rep_emails())
    try:
        calendar().events().patch(
            calendarId=CAL_TARGET, eventId=ev["id"],
            body={"attendees": attendees}, sendUpdates="none").execute()
        log.info("updated calendar guests for %s (%d)", page_id, len(attendees))
    except Exception:
        log.exception("failed to update calendar guests for %s", page_id)


def run_weekly_rundown(client):
    """Monday 10am job: always post the rundown, and DM Sean an FYI if any events
    are still missing reps (no longer gated on a reaction)."""
    events = fetch_week_events()
    if not events:
        log.info("no %s events this week; skipping rundown", RUNDOWN_CITY)
        return
    post_rundown(client, events)
    missing = [e for e in events if not e["reps"]]
    if missing and not reminder_sent_this_week(client):
        send_reps_reminder(client, missing)


def weekly_scheduler(client):
    """Fire run_weekly_rundown once on the send day (Mon), at or after 10:00 ET.
    Using >= 10 (not == 10) means a republish/restart later in the day still catches
    up and sends that day; run_weekly_rundown's already-posted-this-week scan keeps it
    from double-sending."""
    last = None
    while True:
        now = datetime.now(RUNDOWN_TZ)
        if now.weekday() == RUNDOWN_WEEKDAY and now.hour >= 10 and now.date() != last:
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
    {intent: change|edit|question|none, event_index, remove[], add[], changes{}, answer}."""
    today = date.today().isoformat()
    lines = [f"{i}: {e['date']} | {e.get('city') or '?'} | {e['event']} | "
             f"reps: {', '.join(e['reps']) or 'none'}" for i, e in enumerate(events)]
    who = ", ".join(sorted(requester_names)) if requester_names else "unknown (not in the rep list)"
    convo = f"CONVERSATION SO FAR (oldest first):\n{context}\n\n" if context else ""
    prompt = (
        "A Rho events rep sent a Slack message to the events bot. Decide what they want.\n"
        f"The sender is known in Notion as: {who}. Today's date is {today}.\n"
        'Return ONLY JSON: {"intent": "change"|"edit"|"question"|"feedback"|"none", '
        '"event_index": <int or null>, "remove": [<names>], "add": [<names>], '
        '"changes": {"date": <YYYY-MM-DD or null>, "city": <string or null>, '
        '"cost": <number or null>, "partner": <string or null>, "invite_link": <string or null>, '
        '"event_name": <string or null>}, "topic": <string or null>, "answer": <string>}.\n'
        "Intents:\n"
        "- \"change\": modify rep assignments for ONE event. Set event_index and fill remove/add.\n"
        "- \"edit\": modify a FIELD of ONE event (its date, city, cost, partner, invite link, or "
        "name). Set event_index and put ONLY the requested field(s) in 'changes'; leave the rest "
        "null. Use this for messages like 'change the date for X to 8-12' or 'rename Y to Z'.\n"
        "- \"feedback\": they are asking about PAST FEEDBACK / how prior events went (with a "
        "partner, host, or format) — e.g. 'any feedback from prior events with Verci?', 'how did "
        "the CADRE dinners go?'. Set 'topic' to the partner/host/format they're asking about "
        "(e.g. 'Verci'). Leave other fields empty.\n"
        "- \"question\": they are ASKING about events or ASSIGNMENTS (who is on an event, what "
        "someone is assigned to, how many, when, etc.). Put a concise Slack-formatted answer in "
        "'answer', computed ONLY from the UPCOMING EVENTS below; list events as "
        "'• <date> — <event>'. If nothing matches, say so plainly. Leave other fields empty.\n"
        "- \"none\": anything else (greetings, chit-chat, unrelated). All fields empty.\n"
        "For both change and edit, set event_index to the matching event's index below (null if "
        "unclear/ambiguous); match the event by name sensibly (partial/reworded names are fine).\n"
        "Edit rules:\n"
        "- date: convert to YYYY-MM-DD. If only month/day is given, pick the year that makes the "
        "date fall on or after today (the next upcoming occurrence).\n"
        f"- city: must be exactly one of {sorted(VALID_CITIES)}, or null.\n"
        "- cost: a plain number of US dollars (e.g. '$3k' -> 3000). null if not mentioned.\n"
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
    # Generous budget: answers can list many events across multiple matching reps.
    return ask_json(prompt, max_tokens=2000, model=FAST_MODEL)


def active_reps_this_week():
    """Reps assigned to any event this week — the practical replacement pool."""
    return sorted({r for e in fetch_week_events() for r in e["reps"]})


def calendar_busy(emails, start_iso, end_iso):
    """Emails with a busy block overlapping [start, end] per Google free/busy.
    Reps whose free/busy is hidden/unreadable simply don't appear busy (optimistic)."""
    cal = calendar()
    emails = [e for e in emails if e]
    if cal is None or not (emails and start_iso and end_iso):
        return set()
    try:
        resp = cal.freebusy().query(body={
            "timeMin": start_iso, "timeMax": end_iso,
            "items": [{"id": e} for e in emails[:50]]}).execute()
    except Exception:
        log.exception("free/busy query failed")
        return set()
    return {em.strip().lower() for em, info in (resp.get("calendars") or {}).items()
            if info.get("busy")}


def event_window(event):
    """(start_iso, end_iso) for a rundown event — from its New-York-calendar clone,
    else a same-day title match on the personal calendar. None if no timed match."""
    cal = calendar()
    if cal is None:
        return None
    src = find_clone(event["id"])
    if not src:
        try:
            d = event["date"]
            tmin = datetime.fromisoformat(d).replace(tzinfo=RUNDOWN_TZ).isoformat()
            tmax = (datetime.fromisoformat(d) + timedelta(days=1)).replace(tzinfo=RUNDOWN_TZ).isoformat()
            items = cal.events().list(calendarId=CAL_SOURCE, timeMin=tmin, timeMax=tmax,
                                      singleEvents=True, maxResults=250).execute().get("items", [])
        except Exception:
            return None
        n = _norm(event["event"])
        src = next((it for it in items if _norm(it.get("summary")) == n
                    or n in _norm(it.get("summary")) or _norm(it.get("summary")) in n), None)
    if not src:
        return None
    s = (src.get("start") or {}).get("dateTime")
    e = (src.get("end") or {}).get("dateTime")
    return (s, e) if s and e else None


def reps_available_for(event, exclude=(), window=None, use_calendar=True):
    """Active reps free for this event. With use_calendar, checks real Google
    free/busy at the event's time (used by /reps-availability, on demand). Without
    it, the fast Notion 'not already booked that day' rule (used inline on a change,
    to keep the response snappy)."""
    excl = {r.strip().lower() for r in exclude}
    pool = [r for r in active_reps_this_week() if r.strip().lower() not in excl]
    if use_calendar:
        if window is None:
            window = event_window(event)
        if window:
            emails = rep_emails()
            pool_email = {r: emails.get(r.strip().lower()) for r in pool}
            busy = calendar_busy([e for e in pool_email.values() if e], window[0], window[1])
            return [r for r in pool if (pool_email[r] or "").strip().lower() not in busy]
    busyday = {r.strip().lower() for e in fetch_week_events()
               if e["date"] == event["date"] for r in e["reps"]}
    return [r for r in pool if r.strip().lower() not in busyday]


def replacement_prompt(removed, event):
    """Ask who is covering for a dropped rep and list who's free (no tags)."""
    if len(removed) == 1:
        n = removed[0]
        poss = f"{n}'" if n.endswith("s") else f"{n}'s"
        head = f"Who will be taking {poss} place?"
    else:
        head = "Who will be taking their place?"
    avail = reps_available_for(event, exclude=removed, use_calendar=False)   # inline: keep it fast
    lines = [head]
    if avail:
        lines.append("The reps available that day are:")
        lines += [f"• {r}" for r in avail]
    else:
        lines.append("(No other active reps look free.)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-assessment of event proposals (feedback + business fit + revenue)
# ---------------------------------------------------------------------------

# Tarlon's "Event Partner Screening Framework", distilled into a scoring rubric.
# Refresh this if the source doc changes:
# https://docs.google.com/document/d/1lxKg9V8rw2ibVa-gOcHeI9h4kCJgaFfAEc3sor8jbV4
BUSINESS_FRAMEWORK = """\
Rho Event Partner Screening Framework (how we decide if an event is worth it).

Philosophy: fewer, higher-quality events with intentional partners beat volume.
We lean toward thought leadership, panels, curated dinners, and real conversations
over loud, open, high-volume parties. The core question every screen answers:
"Does this put the right founders in the room with Rho, in a setting that reflects
well on us?" When in doubt, pass.

The bar:
- ICP: pre-seed -> Series A founders, ideally in SF, NYC, Boston, or LA.
- Ideal model: a VC/accelerator partner fills the room -> a service partner sponsors.
- Budget: $1-3K smaller events, $5-10K flagship activations.
- Lean toward: curated dinners, panels, thought leadership, intimate mixers.
- Avoid: open/unvetted RSVPs, gimmick formats, doubling up on the same service-partner category.
- Auto-pass: wrong-ICP audience, or unprofessional/blacklisted behavior in planning.

Score each of the six factors Green (strong) / Flag (caution) / Fail (dealbreaker):
1. Audience & ICP fit (MOST IMPORTANT): pre-seed->Series A founders in core markets,
   curated & vetted. Fail if broad/junior/wrong-geo or open unvetted RSVPs.
2. Room-fill credibility: partner owns their list and can fill the room. Fail if the
   partner expects Rho to do all the attendance heavy lifting.
3. Format & vibe fit: intimate dinners, curated mixers, panels, thought leadership.
   Dinners (~$2-4K) consistently beat large mixers per activation. Fail on poker
   nights, workout classes, gimmicks, or open-RSVP mixers with no curation.
4. Partner behavior: professional, relationship-first. Flag transactional/intel-fishing
   tone; fail/blacklist unprofessional conduct or last-minute drop-outs.
5. Budget & sponsorship structure: ask proportionate to audience quality; partner
   co-sponsors. Flag a high ask only justifiable if turnout is strong. Watch category
   doubling (two law firms, two accounting firms in one room).
6. Track record & ROI path: proven execution, clear turnout numbers, a follow-up system
   to convert the room. Fail on a history of low turnout or a big-format ask with no
   follow-up mechanism.

Verdict: GO (clears the bar), FLAG (proceed only with fixes/conditions), or PASS.
"""

_STOP = {"the", "a", "an", "and", "or", "of", "for", "with", "to", "in", "at", "on",
         "by", "hosted", "event", "dinner", "founder", "founders", "rho", "vc", "night"}


def _tok(s):
    """Lowercase alnum tokens (>=3 chars), minus common event stopwords."""
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) >= 3 and w not in _STOP}


_feedback_cache = None
_feedback_ts = 0.0
_FEEDBACK_TTL = 600               # 10 min
# Tally posts each submission as "*label*\nvalue" pairs; capture every pair.
_TALLY_FIELD_RE = re.compile(r"\*([^*\n]+)\*[ \t]*\n(.*?)(?=\n\*[^*\n]+\*[ \t]*\n|\Z)", re.DOTALL)


def _message_fulltext(m):
    """All human-readable text of a Slack message, incl. attachments/blocks —
    Tally submissions often carry their content there rather than in `text`.
    Normalizes structured field pairs (title/value) into "*title*\\nvalue" so the
    Tally field regex can read them regardless of whether Tally posts mrkdwn text,
    block fields, or legacy attachment fields."""
    parts = [m.get("text") or ""]
    for a in m.get("attachments") or []:
        parts += [a.get("pretext") or "", a.get("text") or "", a.get("fallback") or ""]
        for f in a.get("fields") or []:                 # legacy attachment fields
            title, val = (f.get("title") or "").strip(), (f.get("value") or "").strip()
            if title or val:
                parts.append(f"*{title}*\n{val}" if title else val)
    for b in m.get("blocks") or []:
        t = b.get("text")
        if isinstance(t, dict):
            parts.append(t.get("text") or "")
        for f in b.get("fields") or []:                 # Block Kit section fields
            if isinstance(f, dict):
                parts.append(f.get("text") or "")
    return "\n".join(p for p in parts if p)


def _parse_tally(text):
    """Parse one Tally feedback submission into a compact dict, or None if the
    message isn't a (real, non-test) submission."""
    if "New submission" not in text:
        return None
    city = ""
    header = re.search(r"New submission for\s+\*?([^*\n.]+)", text)
    if header:
        name = header.group(1).upper()
        city = "NYC" if "NYC" in name else "SF" if "SF" in name else ""
    row = {"city": city, "event": "", "partner": "", "date": "",
           "feedback": "", "leads": "", "right_people": "", "adjust": ""}
    for label, value in _TALLY_FIELD_RE.findall(text):
        l, v = label.strip().lower(), value.strip()
        if l == "event":
            row["event"] = v
        elif l.startswith("partner"):
            row["partner"] = v
        elif l == "date":
            row["date"] = v
        elif "high-quality leads" in l:
            row["leads"] = v
        elif "adjust" in l:
            row["adjust"] = v
        elif "right people" in l:
            row["right_people"] = v
        elif "feedback" in l or l.startswith("what did you think"):
            row["feedback"] = v
    if not (row["event"] or row["feedback"]):
        return None
    if row["event"].strip().upper().startswith("TEST") or \
            row["feedback"].strip().upper() == "TEST":
        return None                                # skip Tally test rows
    return row


def fetch_all_feedback():
    """Recent #events-feedback submissions as compact dicts (cached). Degrades to
    [] if the bot can't read the channel (not a member / missing scope)."""
    global _feedback_cache, _feedback_ts
    if _feedback_cache is not None and (time.time() - _feedback_ts) < _FEEDBACK_TTL:
        return _feedback_cache
    rows, cursor, scanned, ok = [], None, 0, True
    try:
        for _ in range(FEEDBACK_HISTORY_PAGES):
            kw = {"channel": FEEDBACK_CHANNEL, "limit": 100}
            if cursor:
                kw["cursor"] = cursor
            r = app.client.conversations_history(**kw)
            msgs = r.get("messages", [])
            scanned += len(msgs)
            for m in msgs:
                row = _parse_tally(_message_fulltext(m))
                if row:
                    rows.append(row)
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception:
        ok = False
        log.exception("could not read #events-feedback %s — is the bot a member "
                      "(/invite) and is the channel ID right?", FEEDBACK_CHANNEL)
    log.info("feedback: scanned %d msg(s) from %s, parsed %d submission(s)",
             scanned, FEEDBACK_CHANNEL, len(rows))
    if ok:                                           # don't cache a failed read
        _feedback_cache, _feedback_ts = rows, time.time()
    return rows


def relevant_feedback(partner, event_name, limit=8):
    """Feedback rows most similar to this partner/format, by token overlap."""
    terms = _tok(partner) | _tok(event_name)
    all_rows = fetch_all_feedback()
    if not terms:
        log.info("feedback match: no usable terms from partner=%r event=%r (had %d rows)",
                 partner, event_name, len(all_rows))
        return []
    scored = []
    for r in all_rows:
        overlap = len(terms & (_tok(r["partner"]) | _tok(r["event"])))
        if overlap:
            scored.append((overlap, r))
    scored.sort(key=lambda x: -x[0])
    log.info("feedback match: %d of %d rows relevant to terms %s",
             len(scored), len(all_rows), sorted(terms))
    return [r for _, r in scored[:limit]]


def _feedback_block(rows):
    if not rows:
        return "No past feedback found for this partner or a similar format."
    out = []
    for r in rows:
        loc = ", ".join(x for x in [r.get("city"), r.get("date")] if x)
        head = r.get("event") or "(event)"
        if loc:
            head = f"{head} ({loc})"
        bits = [head]
        if r.get("partner"):
            bits.append(f"partner: {r['partner']}")
        if r.get("leads"):
            bits.append(f"high-quality leads: {r['leads']}")
        if r.get("right_people"):
            bits.append(f"right people?: {r['right_people']}")
        if r.get("feedback"):
            bits.append(f"feedback: {r['feedback']}")
        if r.get("adjust"):
            bits.append(f"adjust next time: {r['adjust']}")
        out.append("- " + " | ".join(bits))
    return "\n".join(out)


def answer_feedback_question(text, topic):
    """Answer a 'how did past events with X go?' question from #events-feedback."""
    rows = relevant_feedback(topic or "", topic or "")
    if not rows:
        if not fetch_all_feedback():                 # nothing readable at all
            return ("I couldn't find any feedback to check — I may not have access to the "
                    "#events-feedback channel yet (I need to be invited to it).")
        label = f" for “{topic}”" if topic else ""
        return f"I don't have any logged event feedback{label}."
    out = ask_json(
        "A teammate asked the events bot about PAST event feedback. Using ONLY the feedback "
        "entries below, write a concise Slack-formatted answer (a short summary plus 2-4 bullet "
        "highlights: partner/audience fit, lead quality, and any recurring praise or issues). If "
        "the entries don't really address the question, say so plainly. Do not invent anything. "
        "Return ONLY JSON {\"answer\": <string>}.\n\n"
        f"QUESTION:\n{text}\n\nFEEDBACK ENTRIES:\n{_feedback_block(rows)}",
        max_tokens=800)
    return (out.get("answer") or "").strip() or f"Here's what I found:\n{_feedback_block(rows)}"


_revenue_cache = {}               # term -> (text, timestamp)
_REVENUE_TTL = 900


def snowflake_revenue(partner, event_type, city, event_name):
    """Past revenue tied to this partner/format, from Snowflake. Fully driven by
    env: the SNOWFLAKE_* connection vars plus REVENUE_SQL, a SELECT that uses the
    named binds %(partner)s %(event_type)s %(city)s %(term)s. Returns a short text
    summary of the rows, or None when revenue isn't configured / is unavailable."""
    sql = os.environ.get("REVENUE_SQL")
    if not (_SNOWFLAKE_AVAILABLE and sql and os.environ.get("SNOWFLAKE_ACCOUNT")):
        return None
    term = (partner or event_name or "").strip()
    cached = _revenue_cache.get(term)
    if cached and (time.time() - cached[1]) < _REVENUE_TTL:
        return cached[0]
    binds = {"partner": partner or "", "event_type": event_type or "",
             "city": city or "", "term": term}
    try:
        conn = snowflake_connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            password=os.environ.get("SNOWFLAKE_PASSWORD"),
            authenticator=os.environ.get("SNOWFLAKE_AUTHENTICATOR"),
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
            database=os.environ.get("SNOWFLAKE_DATABASE"),
            schema=os.environ.get("SNOWFLAKE_SCHEMA"),
            role=os.environ.get("SNOWFLAKE_ROLE"),
            login_timeout=15, network_timeout=30)
    except Exception:
        log.exception("snowflake connection failed")
        return None
    try:
        cur = conn.cursor()
        cur.execute(sql, binds)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchmany(25)
    except Exception:
        log.exception("snowflake revenue query failed")
        return None
    finally:
        conn.close()
    if not rows:
        text = "No comparable past revenue found in Snowflake."
    else:
        text = "\n".join(", ".join(f"{c}={v}" for c, v in zip(cols, row)) for row in rows)
    _revenue_cache[term] = (text, time.time())
    return text


def revenue_configured():
    """True when the Snowflake revenue aspect is wired up (lib + creds + query)."""
    return bool(_SNOWFLAKE_AVAILABLE and os.environ.get("SNOWFLAKE_ACCOUNT")
                and os.environ.get("REVENUE_SQL"))


def assess_proposal(fields, text):
    """Score a proposal 1-10 across business-fit + feedback (+ revenue when Snowflake
    is configured). Returns the parsed JSON assessment, or {} on failure."""
    partner = fields.get("partner") or ""
    event_name = fields.get("event") or ""
    city = fields.get("city") or ""
    cost = fields.get("cost")
    fb = _feedback_block(relevant_feedback(partner, event_name))
    rev_enabled = revenue_configured()

    aspects = "THREE" if rev_enabled else "TWO"
    revenue_section = ""
    revenue_rule = ""
    revenue_json = ""
    if rev_enabled:
        rev = snowflake_revenue(partner, None, city, event_name)
        rev_block = rev if rev is not None else "No comparable past revenue found."
        revenue_section = ("=== PAST REVENUE (from Snowflake, tied to this partner/format) ===\n"
                           + rev_block + "\n\n")
        revenue_rule = ("- revenue.score: what past revenue implies. If it shows no comparable "
                        "events, set revenue.score to null and say so — do not penalize.\n")
        revenue_json = "\"revenue\": {\"score\": int|null, \"reason\": str}, "

    prompt = (
        "You are assessing whether Rho should invest in a proposed community event. "
        f"Score it 1-10 across {aspects} aspects and give a short reason for each. Base your "
        "judgement ONLY on the material below; never invent history or numbers.\n\n"
        "=== BUSINESS-GOAL FRAMEWORK ===\n" + BUSINESS_FRAMEWORK + learned_guidelines_text() + "\n"
        "=== PAST FEEDBACK (from prior events with this partner or similar formats) ===\n"
        + fb + "\n\n"
        + revenue_section +
        "=== THE PROPOSAL ===\n"
        f"Event: {event_name}\nPartner: {partner or 'unknown'}\nCity: {city or 'unknown'}\n"
        f"Estimated cost: {cost if cost is not None else 'unknown'}\n"
        f"Full message:\n{text}\n\n"
        "Scoring rules:\n"
        "- business.score: how well it fits the framework above (this is the primary driver; "
        "weight audience/ICP fit most heavily).\n"
        "- feedback.score: what prior feedback implies about this partner/format. If there is "
        "NO relevant feedback, set feedback.score to null and say so — do not penalize.\n"
        + revenue_rule +
        "- overall: a 1-10 holistic score, driven mainly by business fit and adjusted by whatever "
        "evidence exists. Do NOT average in nulls.\n"
        "- verdict: exactly one of GO, FLAG, or PASS.\n\n"
        "Return ONLY JSON: {\"overall\": int, \"verdict\": \"GO|FLAG|PASS\", "
        "\"business\": {\"score\": int, \"reason\": str}, "
        "\"feedback\": {\"score\": int|null, \"reason\": str}, "
        + revenue_json +
        "\"summary\": str}. Keep every reason to one sentence; summary to one sentence."
    )
    a = ask_json(prompt, max_tokens=1200) or {}
    if a:
        a["_revenue_enabled"] = rev_enabled
    return a


def _score_str(s):
    return f"{s}/10" if isinstance(s, (int, float)) else "n/a"


def assessment_text(a):
    verdict = (a.get("verdict") or "").upper()
    emoji = {"GO": ":large_green_circle:", "FLAG": ":large_yellow_circle:",
             "PASS": ":red_circle:"}.get(verdict, ":white_circle:")
    b = a.get("business") or {}
    f = a.get("feedback") or {}
    lines = [
        f"*{ASSESS_SENTINEL}* {emoji} *{verdict or '—'}* · *{_score_str(a.get('overall'))}*",
        f"> *Business fit* ({_score_str(b.get('score'))}): {b.get('reason', '—')}",
        f"> *Past feedback* ({_score_str(f.get('score'))}): {f.get('reason', '—')}",
    ]
    if a.get("_revenue_enabled"):
        r = a.get("revenue") or {}
        lines.append(f"> *Past revenue* ({_score_str(r.get('score'))}): {r.get('reason', '—')}")
    if a.get("summary"):
        lines += ["", a["summary"]]
    return "\n".join(lines)


def already_assessed(client, ts):
    """True if the bot has already posted an assessment in this proposal's thread."""
    try:
        msgs = client.conversations_replies(channel=CHANNEL, ts=ts, limit=50)["messages"]
    except Exception:
        return False
    return any((m.get("bot_id") or m.get("user") == BOT_USER_ID)
               and ASSESS_SENTINEL in (m.get("text") or "") for m in msgs)


# --- Self-learning memory (Notion-backed) -----------------------------------
# The memory page holds one paragraph block per entry, each a JSON line prefixed
# "SIGNAL " (a raw insight) or "GUIDELINE " (a distilled, active standard).
_SIG_PREFIX = "SIGNAL "
_GUIDE_PREFIX = "GUIDELINE "
_memory_cache = None
_memory_ts = 0.0
_MEMORY_TTL = 60


def memory_enabled():
    return bool(MEMORY_PAGE_ID)


def _block_text(block):
    t = block.get("type")
    return _plain((block.get(t) or {}).get("rich_text") or []) if t else ""


def _memory_blocks():
    """(block_id, text) for every paragraph on the memory page."""
    out, cursor = [], None
    while True:
        kw = {"block_id": MEMORY_PAGE_ID, "page_size": 100}
        if cursor:
            kw["start_cursor"] = cursor
        r = notion.blocks.children.list(**kw)
        out += [(b["id"], _block_text(b)) for b in r["results"]]
        if not r.get("has_more"):
            return out
        cursor = r.get("next_cursor")


def _append_memory(line):
    notion.blocks.children.append(block_id=MEMORY_PAGE_ID, children=[{
        "object": "block", "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": line[:1900]}}]}}])


def _invalidate_memory():
    global _memory_ts
    _memory_ts = 0.0


def load_memory():
    """(signals, guidelines) parsed from the memory page. Cached briefly. Each
    guideline carries a private '_bid' (its block id) for in-place updates."""
    global _memory_cache, _memory_ts
    if not MEMORY_PAGE_ID:
        return ([], [])
    if _memory_cache is not None and (time.time() - _memory_ts) < _MEMORY_TTL:
        return _memory_cache
    signals, guidelines = [], []
    try:
        for bid, txt in _memory_blocks():
            if txt.startswith(_SIG_PREFIX):
                try:
                    signals.append(json.loads(txt[len(_SIG_PREFIX):]))
                except json.JSONDecodeError:
                    pass
            elif txt.startswith(_GUIDE_PREFIX):
                try:
                    g = json.loads(txt[len(_GUIDE_PREFIX):])
                    g["_bid"] = bid
                    guidelines.append(g)
                except json.JSONDecodeError:
                    pass
    except Exception:
        log.exception("could not read assessment memory page (is it shared with the integration?)")
        return ([], [])
    _memory_cache, _memory_ts = (signals, guidelines), time.time()
    return _memory_cache


def learned_guidelines_text():
    """Active learned guidelines, formatted to append to the framework prompt."""
    _, guidelines = load_memory()
    active = [g["guideline"] for g in guidelines
              if g.get("active", True) and g.get("guideline")]
    if not active:
        return ""
    return ("\n\n=== LEARNED ADJUSTMENTS (from the team's feedback on past assessments; "
            "apply these on top of the framework) ===\n"
            + "\n".join(f"- {g}" for g in active) + "\n")


def _extract_lesson(comment, proposal, assessment):
    """Decide if a thread reply carries a generalizable assessment insight, and
    normalize it into a guideline + grouping theme."""
    prompt = (
        "A Rho team member replied in the thread of an event-proposal assessment that I (a bot) "
        "posted. Decide whether their reply contains a GENERALIZABLE insight about how event "
        "proposals should be judged (a preference, a red/green flag, a weighting) — as opposed "
        "to small talk, a one-off logistics note, or a question. If it does, phrase it as ONE "
        "general, imperative guideline usable for FUTURE proposals (not specific to this event), "
        "give a short kebab-case 'theme' to group similar insights, and a 'stance': 'raise', "
        "'lower', or 'neutral'.\n\n"
        f"PROPOSAL:\n{(proposal or '')[:1500]}\n\n"
        f"MY ASSESSMENT:\n{(assessment or '')[:800]}\n\n"
        f"THEIR REPLY:\n{(comment or '')[:1000]}\n\n"
        "Return ONLY JSON: {\"actionable\": bool, \"lesson\": str, \"theme\": str, "
        "\"stance\": \"raise|lower|neutral\"}."
    )
    return ask_json(prompt, max_tokens=400, model=FAST_MODEL) or {}


def _distill_guideline(theme, signals):
    """Turn several same-theme signals into one concise standing guideline."""
    joined = "\n".join(f"- {s.get('lesson')}" for s in signals if s.get("lesson"))
    prompt = (
        "These are recurring pieces of feedback from Rho's events team about how to assess event "
        f"proposals, all on the theme '{theme}':\n{joined}\n\n"
        "Distill them into ONE concise, general assessment guideline (max 30 words, imperative) "
        "to apply when scoring future proposals. Return ONLY JSON {\"guideline\": str}."
    )
    return (ask_json(prompt, max_tokens=200) or {}).get("guideline")


def handle_assessment_feedback(client, channel, thread_ts, msg_ts, user, text):
    """A reply in an assessment thread: capture the insight, and adjust the standard
    only once a theme recurs (never off a single comment)."""
    text = re.sub(r"^\s*<@[A-Z0-9]+>\s*", "", text or "").strip()
    if len(text) < 3:
        return
    proposal, assessment = "", ""
    try:
        msgs = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)["messages"]
        proposal = (msgs[0].get("text") if msgs else "") or ""
        for m in msgs:
            if (m.get("bot_id") or m.get("user") == BOT_USER_ID) \
                    and ASSESS_SENTINEL in (m.get("text") or ""):
                assessment = m.get("text") or ""
                break
    except Exception:
        pass

    info = _extract_lesson(text, proposal, assessment)
    if not info.get("actionable") or not info.get("lesson"):
        try:                                      # low-noise acknowledgement
            client.reactions_add(channel=channel, timestamp=msg_ts, name="+1")
        except Exception:
            pass
        return

    if not memory_enabled():
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="Thanks — noted. (Persistent learning isn't set up yet, so I can't save this "
                 "across restarts. Ask an admin to configure the memory page.)")
        return

    lesson = info["lesson"].strip()
    theme = (info.get("theme") or "general").strip().lower()
    signal = {"lesson": lesson, "theme": theme, "stance": info.get("stance", "neutral"),
              "user": user, "event": proposal[:80], "ts": msg_ts}
    try:
        _append_memory(_SIG_PREFIX + json.dumps(signal, ensure_ascii=False))
        _invalidate_memory()
    except Exception:
        log.exception("could not write learning signal")
        client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                text="Thanks — I heard you, but couldn't save it just now.")
        return

    signals, guidelines = load_memory()
    same = [s for s in signals if s.get("theme") == theme]
    n = len(same)
    existing = next((g for g in guidelines
                     if g.get("theme") == theme and g.get("active", True)), None)

    guide, promoted = None, False
    if n >= LEARN_THRESHOLD and not existing:
        guide = _distill_guideline(theme, same)
        if guide:
            _append_memory(_GUIDE_PREFIX + json.dumps(
                {"theme": theme, "guideline": guide, "support": n, "active": True},
                ensure_ascii=False))
            _invalidate_memory()
            promoted = True
    elif existing:
        try:                                      # keep the support count fresh
            payload = {"theme": theme, "guideline": existing.get("guideline"),
                       "support": n, "active": True}
            notion.blocks.update(block_id=existing["_bid"], paragraph={"rich_text": [
                {"type": "text", "text": {"content": (_GUIDE_PREFIX + json.dumps(
                    payload, ensure_ascii=False))[:1900]}}]})
            _invalidate_memory()
        except Exception:
            log.exception("could not update guideline support")

    if promoted:
        ack = (f"Got it — that's a consistent pattern now ({n} similar notes), so I've folded it "
               f"into how I assess going forward:\n> {guide}")
    elif existing:
        ack = (f"Noted :+1: — that reinforces a standard I already apply: "
               f"“{existing.get('guideline')}”.")
    else:
        left = max(1, LEARN_THRESHOLD - n)
        ack = (f"Noted :+1: — logged this. I won't reweight my scoring off a single comment; if "
               f"the same point comes up ~{left} more time(s), I'll bake it into my standard.")
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=ack)
    log.info("learning signal on '%s' (%d/%d)%s from %s",
             theme, n, LEARN_THRESHOLD, " -> promoted" if promoted else "", user)


def _is_assessment_thread(client, channel, thread_ts):
    """True if this thread is one where the bot posted an assessment."""
    if channel != CHANNEL:
        return False
    if (channel, thread_ts) in _assessment_threads:
        return True
    try:
        msgs = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)["messages"]
    except Exception:
        return False
    found = any((m.get("bot_id") or m.get("user") == BOT_USER_ID)
                and ASSESS_SENTINEL in (m.get("text") or "") for m in msgs)
    if found:
        _assessment_threads.add((channel, thread_ts))
    return found


def _build_event_edits(ev, changes):
    """From a parsed 'changes' dict, build Notion property updates + human summary.
    Returns (props, summary_bits, new_date_or_None, new_name_or_None)."""
    props, bits, new_date, new_name = {}, [], None, None
    d = (changes.get("date") or "").strip()
    if d:
        try:
            date.fromisoformat(d)
            props["Date"] = {"date": {"start": d}}
            bits.append(f"date → {fmt_day(d)}")
            new_date = d
        except ValueError:
            pass
    city = (changes.get("city") or "").strip()
    if city in VALID_CITIES:
        props["City"] = {"select": {"name": city}}
        bits.append(f"city → {city}")
    cost = to_number(changes.get("cost"))
    if cost is not None:
        props["Estimated Cost"] = {"number": cost}
        bits.append(f"est. cost → ${cost:,.0f}")
    partner = (changes.get("partner") or "").strip()
    if partner:
        props["Partner"] = rt(partner)
        bits.append(f"partner → {partner}")
    invite = (changes.get("invite_link") or "").strip()
    if invite:
        props["Invite Link"] = rt(invite)
        bits.append("invite link updated")
    name = (changes.get("event_name") or "").strip()
    if name and name != ev["event"]:
        props["Event"] = {"title": [{"text": {"content": name}}]}
        bits.append(f"name → {name}")
        new_name = name
    return props, bits, new_date, new_name


def handle_event_edit(client, channel, thread_ts, user, ev, changes):
    """Apply a field edit to an event: write Notion, then (deferred) move/rename the
    calendar clone on a date/name change and refresh the rundown."""
    props, bits, new_date, new_name = _build_event_edits(ev, changes)
    if not props:
        log.info("edit with no applicable fields; staying silent")
        return
    _bot_threads.add((channel, thread_ts))            # engaged — follow the rest of this thread
    try:
        notion.pages.update(page_id=ev["id"], properties=props)
    except Exception:
        log.exception("notion edit failed for %s", ev["id"])
        client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                text=f"Couldn't update *{ev['event']}* just now — try again.")
        return
    invalidate_week_cache()
    log.info("edited %r by %s: %s", ev["event"], user, bits)

    # Response first (snappy); slower calendar/rundown work after.
    label = new_name or ev["event"]
    client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                            text=f":white_check_mark: Updated *{label}*: " + "; ".join(bits) + ".")
    if new_date:
        try:
            move_calendar_date(ev["id"], new_date)
        except Exception:
            log.exception("calendar move failed for %s", ev["id"])
    if new_name:
        try:
            rename_calendar_clone(ev["id"], new_name)
        except Exception:
            log.exception("calendar rename failed for %s", ev["id"])
    try:
        edit_rundowns(client)                         # reflect the change in any live rundown
    except Exception:
        log.exception("rundown refresh failed")


def handle_mention(client, channel, thread_ts, msg_ts, user, text, rundown=False):
    text = re.sub(r"^\s*<@[A-Z0-9]+>\s*", "", text or "").strip()   # drop leading @bot
    if not text:
        return
    is_rundown = rundown or (channel, thread_ts) in _rundown_msgs
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

    # Answering a question about PAST FEEDBACK (from #events-feedback).
    if intent == "feedback":
        _bot_threads.add((channel, thread_ts))
        answer = answer_feedback_question(text, parsed.get("topic"))
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=answer)
        log.info("answered feedback question from %s (topic=%r)", user, parsed.get("topic"))
        return

    # Editing a field of one event (date, city, cost, partner, invite, name).
    if intent == "edit":
        idx = parsed.get("event_index")
        if not isinstance(idx, int) or not 0 <= idx < len(events):
            log.info("edit request without a specific event; staying silent")
            return
        handle_event_edit(client, channel, thread_ts, user, events[idx],
                          parsed.get("changes") or {})
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

    removed = [r for r in current if r.strip().lower() in remove]
    if set(new) == set(current):
        if is_rundown:                             # nothing to change; don't clutter the thread
            return
        note = f" (couldn't find in the rep list: {', '.join(invalid)})" if invalid else ""
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"No changes made to *{ev['event']}* ({fmt_day(ev['date'])})." + note)
        return

    notion.pages.update(page_id=ev["id"],
                        properties={"Reps": {"multi_select": [{"name": n} for n in new]}})
    invalidate_week_cache()                        # so the deferred rundown edit reflects this
    log.info("assignment change on %r by %s: -%s +%s", ev["event"], user, removed, add)

    needs_replacement = bool(removed) and not add  # dropped with no named replacement

    # --- User-facing response FIRST (keep it snappy) ---
    if is_rundown:
        edit_rundowns(client)                      # the rundown edit is the feedback here
        if needs_replacement:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=replacement_prompt(removed, ev))
    else:
        # @mention / DM: a plain-text confirmation — no @-mentions, no pings.
        parts = [f":white_check_mark: Updated *{ev['event']}* ({fmt_day(ev['date'])})."]
        if removed:
            parts.append("Removed: " + ", ".join(removed))
        if add:
            parts.append("Added: " + ", ".join(add))
        if needs_replacement:
            parts.append("")
            parts.append(replacement_prompt(removed, ev))
        else:
            parts.append("Reps now: " + (", ".join(new) or "none"))
        if invalid:
            parts.append(f"Couldn't find in the rep list, skipped: {', '.join(invalid)}")
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="\n".join(parts))

    # --- Slower side-effects AFTER the response (user isn't blocked on these) ---
    try:
        update_calendar_guests(ev["id"], new)      # keep the gcal invite in sync (no email)
    except Exception:
        log.exception("calendar guest sync failed for %s", ev["id"])
    if not is_rundown:                             # rundown path already edited above
        try:
            edit_rundowns(client)
        except Exception:
            log.exception("rundown refresh failed")
    for r in add:                                  # DM newly-assigned reps their lead list
        try:
            send_lead_list(client, r, ev["event"], ev.get("invite"))
        except Exception:
            log.exception("lead-list send failed for %s", r)


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


def _delete_bot_message(client, channel, ts):
    """Delete a message the bot posted (triggered by an approver's 🗑️ reaction).
    Slack only lets chat.delete remove messages posted by the same token, so this
    can never delete anyone else's message — a non-bot target just errors out."""
    try:
        client.chat_delete(channel=channel, ts=ts)
        log.info("deleted bot message %s in %s via 🗑️", ts, channel)
    except Exception:
        log.warning("could not delete %s in %s — not the bot's own message, or "
                    "missing permission", ts, channel)


@app.event("reaction_added")
def on_reaction(event, client):
    item = event.get("item", {})
    reaction, user = event.get("reaction"), event.get("user")
    channel, ts = item.get("channel"), item.get("ts")
    log.info("reaction_added: reaction=%r user=%r channel=%r type=%r",
             reaction, user, channel, item.get("type"))
    if item.get("type") != "message":
        return
    # 🗑️ an approver deletes one of the bot's own messages (any channel).
    if reaction == DELETE_EMOJI and user in APPROVERS:
        _bg(_delete_bot_message, client, channel, ts)
        return
    # 👀 on any #community-team message manually triggers an assessment.
    if reaction == ASSESS_EMOJI and channel == CHANNEL:
        _bg(_assess_message, client, ts)
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
    """An @-mention: assessment-thread feedback, else a rep-assignment change."""
    root = event.get("thread_ts") or event["ts"]
    _bg(_dispatch_mention, client, event["channel"], root, event["ts"],
        event.get("user"), event.get("text", ""))


def _dispatch_mention(client, channel, thread_ts, msg_ts, user, text):
    if _is_assessment_thread(client, channel, thread_ts):
        handle_assessment_feedback(client, channel, thread_ts, msg_ts, user, text)
    else:
        handle_mention(client, channel, thread_ts, msg_ts, user, text)


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
    # Plain (non-@mention) thread replies are ignored — the bot acts only on
    # @mentions and DMs. (This also covers replies under the weekly rundown.)
    if thread_ts:
        return
    # Top-level community-channel message — proposal heads-up + assessment.
    if channel != CHANNEL:
        return
    if len(text) < ASSESS_MIN_LEN:                # skip chatter
        return
    _bg(_handle_new_proposal, client, ts, text)


def _post_assessment(client, ts, fields, text):
    """Assess a proposal and post it in-thread, unless one is already there."""
    if already_assessed(client, ts):
        return
    a = assess_proposal(fields, text)
    if a and a.get("overall") is not None:
        client.chat_postMessage(channel=CHANNEL, thread_ts=ts, text=assessment_text(a))
        _assessment_threads.add((CHANNEL, ts))    # so thread replies route to learning
        log.info("posted assessment %s (%s) for %s",
                 a.get("overall"), a.get("verdict"), ts)


def _handle_new_proposal(client, ts, text):
    """A top-level #community-team message. Parse once; if it's a proposal, post a
    budget heads-up (when over threshold) and a 1-10 assessment in its thread."""
    fields = parse_proposal(text)
    if not fields.get("event"):
        return
    # Budget heads-up (needs a date to look up the month's budget).
    if fields.get("date"):
        try:
            status = budget_status(fields)
            if status:
                client.chat_postMessage(channel=CHANNEL, thread_ts=ts,
                                        text=pre_approval_text(status))
                log.info("posted pre-approval %s warning for %s", status["band"], ts)
        except Exception:
            log.exception("budget heads-up failed for %s", ts)
    try:
        _post_assessment(client, ts, fields, text)
    except Exception:
        log.exception("assessment failed for %s", ts)


def _assess_message(client, ts):
    """Manual 👀 trigger: fetch the reacted message and assess it if it's a proposal."""
    if already_assessed(client, ts):
        return
    try:
        msgs = client.conversations_history(
            channel=CHANNEL, latest=ts, inclusive=True, limit=1).get("messages", [])
    except Exception:
        log.exception("could not fetch reacted message %s", ts)
        return
    text = (msgs[0].get("text") if msgs else "") or ""
    if not text:
        return
    fields = parse_proposal(text)
    if not fields.get("event"):
        log.info("👀 on %s but it isn't an event proposal; skipping", ts)
        return
    try:
        _post_assessment(client, ts, fields, text)
    except Exception:
        log.exception("manual assessment failed for %s", ts)


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


@app.command("/gcal-sync")          # accept a couple of spellings
@app.command("/gcalsync")
def cmd_gcal_sync(ack, body, client):
    ack()
    _bg(_send_gcal_sync, client, body.get("channel_id"), body.get("user_id"))


def _send_gcal_sync(client, channel, user):
    text = run_gcal_sync()
    try:
        client.chat_postEphemeral(channel=channel, user=user, text=text)
    except Exception:
        client.chat_postMessage(channel=user, text=text)
    log.info("posted /gcal-sync report for %s", user)


@app.command("/reps-availability")          # accept a couple of spellings
@app.command("/reps-avail")
def cmd_reps_availability(ack, body, client):
    ack()
    _bg(_send_reps_availability, client, body.get("channel_id"), body.get("user_id"))


def _send_reps_availability(client, channel, user):
    events = fetch_week_events()
    if not events:
        text = "No NYC events this week."
    else:
        lines = [":calendar: *Rep availability — this week*", ""]
        for e in events:
            window = event_window(e)
            when = fmt_day(e["date"])
            if window:
                try:
                    when += ", " + datetime.fromisoformat(window[0]).strftime("%-I:%M %p")
                except Exception:
                    pass
            avail = reps_available_for(e, window=window)
            lines.append(f"*{when} — {e['event']}*")
            lines.append("Available: " + (", ".join(avail) if avail else "—"))
            lines.append("")
        text = "\n".join(lines).strip()
    try:
        client.chat_postEphemeral(channel=channel, user=user, text=text)
    except Exception:
        client.chat_postMessage(channel=user, text=text)
    log.info("posted /reps-availability for %s", user)


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
