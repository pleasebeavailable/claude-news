# Claude Intel Bot

Telegram bot that monitors Claude/Anthropic updates and maintains a local knowledge base.

## What it does

- **Changelog monitoring** — tracks `anthropic.com/news`, GitHub releases (6 Anthropic repos), and docs changes every 6h
- **Knowledge base** — SQLite DB of Claude features, plugins, models, SDK releases
- **Capability search** — ask "is there a Claude plugin for X?" and get answers from the KB or Claude directly
- **Feature explainer** — ask "how do I use extended thinking?" and get practical use cases
- **Ideas inbox** — save ideas from Telegram, list them anytime

## Project layout

```
claude-news/
├── main.py                  # Entry point — Telegram poll loop
├── core/
│   ├── telegram_bot.py      # Telegram send/receive
│   └── claude_llm.py        # Claude Sonnet via CLI (no API key needed)
├── skills/
│   ├── claude_changelog.py  # Changelog + knowledge base skill
│   └── ideas.py             # Ideas inbox skill
├── prompts/
│   └── changelog_usecase.txt  # LLM prompt for feature explanations
├── data/
│   ├── claude_knowledge.db  # SQLite knowledge base (gitignored)
│   └── .sync_trigger        # Written by scheduled agent to trigger sync
├── logs/                    # App logs (gitignored)
└── bin/
    ├── start                # Start script
    └── com.claude.intel.plist  # launchd plist for autostart
```

## Setup

### 1. Install dependencies

```bash
pip3 install requests
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env — add your Telegram bot token and chat ID
```

Get a bot token from [@BotFather](https://t.me/BotFather). Get your chat ID:
```bash
curl -s "https://api.telegram.org/botYOUR_TOKEN/getUpdates" | python3 -m json.tool | grep '"id"'
```

### 3. Start the bot

```bash
bin/start
```

### 4. Autostart on login (optional)

```bash
ln -sf "$(pwd)/bin/com.claude.intel.plist" ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.claude.intel.plist
```

### 5. Keep Mac awake when plugged in (optional, for 24/7 operation)

```bash
sudo pmset -c sleep 0        # Never sleep on AC power
sudo pmset -c displaysleep 10  # Screen off after 10 min
```

## Telegram commands

| Command | Description |
|---|---|
| `changelog` / `claude updates` / `claude news` | Latest updates from knowledge base |
| `claude has <query>` | Search for a Claude capability |
| `claude can <query>` | Same as above |
| `is there a claude plugin for X?` | Natural language capability search |
| `claude use <feature>` | Explain use cases of a feature |
| `how can I use claude's X?` | Same as above |
| `claude add <title>: <desc>` | Manually add entry to knowledge base |
| `idea <text>` | Save an idea |
| `ideas` | List all saved ideas |
| `help` | Show all commands |

Free-form messages are answered by Claude Sonnet (uses `claude` CLI — no extra API key needed).

## Scheduled agent

The bot itself does not fetch external sources. A separate Claude Code scheduled agent
runs every 6h and writes to `data/claude_knowledge.db`, then creates `data/.sync_trigger`
which causes the bot to send a Telegram digest of new updates.

To set up the scheduled agent, use Claude Code's `/schedule` command.

## Logs

```bash
tail -f logs/app.log          # Main bot activity
tail -f logs/launchd-stdout.log  # stdout when running via launchd
```

## Manage the service

```bash
launchctl stop com.claude.intel     # Stop
launchctl start com.claude.intel    # Start
launchctl list | grep claude.intel  # Check status (PID + exit code)
```
