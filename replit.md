# Community Events Slack Bot

A Slack bot for **#community-team** that listens for `:approved:` reactions from Justin, parses event proposals with Claude (Anthropic), and creates pages in the Notion events calendar.

## How to run

```
python app.py
```

The bot uses Slack Socket Mode — no public URL required.

## Required secrets

Set these in Replit Secrets:

| Secret | Description |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-` bot token |
| `SLACK_APP_TOKEN` | `xapp-` app-level token (Socket Mode, `connections:write`) |
| `NOTION_TOKEN` | `ntn_` integration secret |
| `ANTHROPIC_API_KEY` | `sk-ant-` Anthropic key |

## Stack

- Python + [slack-bolt](https://github.com/slackapi/bolt-python) (Socket Mode)
- [notion-client](https://github.com/ramnes/notion-sdk-py)
- [anthropic](https://github.com/anthropics/anthropic-sdk-python) (Claude for parsing proposals)
- python-dotenv

## User preferences

_None recorded yet._
