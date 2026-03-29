# CLAUDE.md — Claude Intel Bot

Telegram bot that monitors Claude/Anthropic updates and maintains a local knowledge base. Python 3, SQLite, Claude CLI for LLM calls.

## Quick reference

```bash
make start       # start bot
make stop        # stop bot
make restart     # restart
make status      # check if running
make logs        # tail -f logs/app.log

make install     # install + enable launchd autostart
make uninstall   # disable launchd autostart

python3 -m py_compile main.py core/*.py skills/*.py   # compile check
```

## Project layout

```
claude-news/
├── main.py                   # Entry point — poll loop, command router, sync trigger check
├── core/
│   ├── telegram_bot.py       # Telegram send/receive (uses TELEGRAM_BOT_TOKEN)
│   └── claude_llm.py         # Claude Sonnet via CLI subprocess, no API key needed
├── skills/
│   ├── claude_changelog.py   # Changelog, KB search, explain feature, add knowledge, process_sync
│   └── ideas.py              # Save/list ideas
├── prompts/
│   └── changelog_usecase.txt # LLM prompt template for explain_feature (uses {var} format)
├── data/                     # Runtime data (gitignored)
│   ├── claude_knowledge.db   # SQLite knowledge base (WAL mode)
│   └── .sync_trigger         # Written by scheduled agent → triggers process_sync()
├── logs/                     # Rotated daily logs (gitignored)
├── scripts/                  # Utility scripts
├── bin/
│   ├── start                 # nohup start with PID file + preflight checks
│   └── com.claude.intel.plist  # launchd plist (symlinked to ~/Library/LaunchAgents/)
└── Makefile                  # start, stop, restart, status, logs, install, uninstall
```

## Architecture

Two components share `data/claude_knowledge.db`:

1. **Claude Code scheduled agent** (cron, every 6h) — fetches sources, writes to DB, creates `data/.sync_trigger`
2. **This bot** — polls Telegram, handles commands, checks trigger every 30s, sends digest

```
Scheduled agent → data/claude_knowledge.db + data/.sync_trigger
                                                        ↓
Bot scheduler tick (30s) → process_sync() → Telegram digest
```

## Database schema (`data/claude_knowledge.db`)

```sql
changelog_entries  -- news, docs, commits (entry_hash UNIQUE, notified bool)
github_releases    -- SDK/tool releases (repo+tag UNIQUE, notified bool)
ideas              -- user ideas inbox
docs_snapshots     -- URL + content_hash for change detection
```

All tables use WAL mode, `busy_timeout=5000`. Both the bot and the scheduled agent access the same DB — safe because WAL handles concurrent readers/writers.

## Critical details

- **LLM**: `claude_llm.chat(prompt)` uses `claude -p --output-format text` via stdin piping. No API key needed — uses your Claude subscription. Thread-safe via `_CLAUDE_LOCK`.
- **Trigger file**: `data/.sync_trigger` — main loop checks `os.path.exists()` every 30s. If found, calls `process_sync()` then deletes it. Idempotent (WHERE notified=0).
- **Startup catch-up**: `process_sync()` runs once on startup to send any entries missed while the bot was offline.
- **Telegram Markdown**: uses legacy `parse_mode="Markdown"` (not V2). Escape `_` and `*` in dynamic content via `_escape_md()` in `claude_changelog.py`.
- **Message limit**: truncate at 4000 chars via `_truncate()`. Digest capped at 5 entries.
- **prompt template braces**: `prompts/changelog_usecase.txt` uses `{var}` for Python `.format()`. Literal braces must be `{{ }}`.

## Key patterns

- **Command routing**: `main.py` iterates `_COMMANDS` list sorted by type (exact < prefix < regex) then priority. First match wins.
- **Skill auto-discovery**: `_COMMANDS` is built from `COMMANDS` metadata on each skill module. Add a new skill → import it in `main.py` and append to the module list.
- **DB connection**: always call `_conn()` per operation, never hold connections open. Uses `with _conn() as conn:` for auto-commit/rollback.
- **process_sync idempotency**: queries `WHERE notified=0`, marks `notified=1` after sending. Safe to call multiple times.

## Environment

`.env` holds `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` — never commit.

launchd plist adds `/Users/antesucue/.local/bin` to PATH so `claude` CLI is found.

## Don'ts

- Don't switch to async/await
- Don't add new dependencies without asking (`requests` is the only external dep)
- Don't change `parse_mode` to MarkdownV2 — would break all format strings
- Don't store credentials anywhere other than `.env`
