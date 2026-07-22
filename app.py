import os, json, logging
from datetime import date
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from notion_client import Client
from anthropic import Anthropic

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("events-bot")

CHANNEL       = "C08K7K31ML6"    # #community-team
APPROVE_EMOJI = "approved"       # Slack sends the bare name, no colons
APPROVERS     = {"U03MEKGQPFC",  # Justin
                 "U0BER9VC6NA"}  # Sean
DB_ID         = "5acc7ada733042a3ace3433f828455b6"   # 2026 Events & Community Calendar

VALID_CITIES = {"Atlanta", "Austin", "Boston", "Chicago", "Holiday", "LA/El Segundo",
    "Miami", "Montana", "NYC", "Nashville", "New Mexico", "Phoenix", "SF", "San Diego",
    "Seattle", "Vegas", "DC"}

app    = App(token=os.environ["SLACK_BOT_TOKEN"])
notion = Client(auth=os.environ["NOTION_TOKEN"])
claude = Anthropic()


def rt(text):
    """Notion rich_text property from a plain string."""
    return {"rich_text": [{"text": {"content": text or ""}}]}


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
        "Cost":        rt(f.get("cost")),
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


@app.event("reaction_added")
def on_reaction(event, client):
    try:
        item = event.get("item", {})
        log.info("reaction_added received: reaction=%r user=%r channel=%r type=%r",
                 event.get("reaction"), event.get("user"),
                 item.get("channel"), item.get("type"))
        if event.get("reaction") != APPROVE_EMOJI:
            log.info("skip: reaction %r != %r", event.get("reaction"), APPROVE_EMOJI)
            return
        if event.get("user") not in APPROVERS:
            log.info("skip: user %r not in approvers %r", event.get("user"), APPROVERS)
            return
        if item.get("channel") != CHANNEL or item.get("type") != "message":
            log.info("skip: channel/type mismatch %r/%r", item.get("channel"), item.get("type"))
            return
        ts = item["ts"]

        # Dedup first: reaction_added can fire twice and the process restarts.
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

        url = create_notion_page(fields, ts)
        client.chat_postMessage(
            channel=CHANNEL, thread_ts=ts, text=f"Notion page created: {url}")
        log.info("created page for %s -> %s", ts, url)
    except Exception:
        log.exception("failed handling reaction on %s", event.get("item", {}).get("ts"))


if __name__ == "__main__":
    log.info("events-bot starting (socket mode)")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
