"""Skill: Claude Intelligence — changelog, knowledge base, capability search."""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core import telegram_bot, claude_llm

logger = logging.getLogger(__name__)

# ── Skill metadata (auto-discovery) ──────────────────────────────────
COMMANDS = [
    {"type": "exact", "pattern": "changelog", "call": "get_changelog"},
    {"type": "exact", "pattern": "claude updates", "call": "get_changelog"},
    {"type": "exact", "pattern": "claude news", "call": "get_changelog"},
    {"type": "prefix", "pattern": "claude has ", "call": "search_capability"},
    {"type": "prefix", "pattern": "claude can ", "call": "search_capability"},
    {"type": "regex",
     "pattern": r"(?i)is\s+there\s+a\s+(?:claude\s+)?(?:plugin|tool|feature|skill)\s+for\s+(.+?)\??$",
     "call": "search_capability", "args": "raw", "priority": 35},
    {"type": "prefix", "pattern": "claude use ", "call": "explain_feature"},
    {"type": "regex",
     "pattern": r"(?i)how\s+(?:can|do|to)\s+(?:i\s+)?use\s+claude(?:'?s)?\s+(.+?)\??$",
     "call": "explain_feature", "args": "raw", "priority": 35},
    {"type": "prefix", "pattern": "claude add ", "call": "add_knowledge"},
]
SCHEDULE = []
HELP_ORDER = 1
HELP = (
    "*Claude Intel*\n"
    "`changelog` — latest Claude/Anthropic updates\n"
    "`claude has MCP` — search for a capability\n"
    "`claude use tool use` — explain use cases\n"
    "`claude add <text>` — add to knowledge base"
)

# ── Database ─────────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent.parent / "data" / "claude_knowledge.db"
PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "changelog_usecase.txt"

_MAX_DIGEST = 5
_MAX_TELEGRAM = 4000


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_db():
    with _conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS changelog_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_hash TEXT UNIQUE NOT NULL,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                date TEXT,
                url TEXT,
                summary TEXT,
                category TEXT,
                capabilities TEXT,
                relevance_score INTEGER,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                notified BOOLEAN DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ce_hash ON changelog_entries(entry_hash);
            CREATE INDEX IF NOT EXISTS idx_ce_category ON changelog_entries(category);
            CREATE INDEX IF NOT EXISTS idx_ce_date ON changelog_entries(date);
            CREATE INDEX IF NOT EXISTS idx_ce_notified ON changelog_entries(notified);

            CREATE TABLE IF NOT EXISTS github_releases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo TEXT NOT NULL,
                tag TEXT NOT NULL,
                name TEXT,
                body TEXT,
                published_at TEXT,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                notified BOOLEAN DEFAULT 0,
                UNIQUE(repo, tag)
            );

            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS docs_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                content_hash TEXT NOT NULL,
                checked_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)


_init_db()


# ── Helpers ──────────────────────────────────────────────────────────

def _escape_md(text: str) -> str:
    """Escape _ and * for Telegram legacy Markdown."""
    return text.replace("_", "\\_").replace("*", "\\*")


def _truncate(text: str, limit: int = _MAX_TELEGRAM) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 20] + "\n\n... (truncated)"


def _entry_date(row) -> str:
    raw = row["date"] or row["fetched_at"] or ""
    if len(raw) >= 10:
        d = raw[:10]
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            return dt.strftime("%b %d")
        except ValueError:
            return d
    return "?"


def _source_icon(source: str) -> str:
    icons = {
        "anthropic_news": "\U0001f4f0",
        "github_release": "\U0001f527",
        "github_commit": "\U0001f50c",
        "docs": "\U0001f4d6",
        "manual": "\u270f\ufe0f",
    }
    return icons.get(source, "\U0001f4cb")


# ── Core functions ───────────────────────────────────────────────────

def process_sync() -> None:
    """Process unnotified entries and send Telegram digest."""
    with _conn() as conn:
        entries = conn.execute(
            "SELECT * FROM changelog_entries WHERE notified=0 AND category != 'skipped' ORDER BY date DESC"
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM github_releases WHERE notified=0 ORDER BY published_at DESC"
        ).fetchall()

    total = len(entries) + len(releases)
    if total == 0:
        logger.info("process_sync: no unnotified entries")
        return

    logger.info("process_sync: %d entries, %d releases to notify", len(entries), len(releases))

    lines = ["*New Claude Updates*", "\u2501" * 19]

    shown = 0
    for row in entries[:_MAX_DIGEST]:
        icon = _source_icon(row["source"])
        date = _entry_date(row)
        title = _escape_md(row["title"])
        summary = ""
        if row["summary"]:
            summary = "\n  _" + _escape_md(row["summary"][:180]) + "_"
        lines.append(f"{icon} *{title}*{summary}")
        shown += 1

    for row in releases[:max(0, _MAX_DIGEST - shown)]:
        date = (row["published_at"] or "")[:10]
        repo = row["repo"].split("/")[-1]
        tag = _escape_md(row["tag"])
        body = ""
        if row["body"]:
            first_line = row["body"].strip().splitlines()[0][:180]
            body = "\n  _" + _escape_md(first_line) + "_"
        lines.append(f"\U0001f527 *{_escape_md(repo)} {tag}* `{date}`{body}")
        shown += 1

    remaining = total - shown
    if remaining > 0:
        lines.append(f"\n_...and {remaining} more — send `changelog` to see all_")

    msg = _truncate("\n".join(lines))
    telegram_bot.send(msg)

    with _conn() as conn:
        entry_ids = [r["id"] for r in entries]
        release_ids = [r["id"] for r in releases]
        if entry_ids:
            placeholders = ",".join("?" * len(entry_ids))
            conn.execute(
                f"UPDATE changelog_entries SET notified=1 WHERE id IN ({placeholders})",
                entry_ids,
            )
        if release_ids:
            placeholders = ",".join("?" * len(release_ids))
            conn.execute(
                f"UPDATE github_releases SET notified=1 WHERE id IN ({placeholders})",
                release_ids,
            )


def get_changelog() -> str:
    """On-demand: show recent entries from knowledge base."""
    with _conn() as conn:
        entries = conn.execute(
            "SELECT * FROM changelog_entries WHERE category != 'skipped' ORDER BY date DESC, fetched_at DESC LIMIT 10"
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM github_releases ORDER BY published_at DESC LIMIT 5"
        ).fetchall()

    if not entries and not releases:
        return "No Claude updates in the knowledge base yet. The scheduled agent will populate it."

    lines = ["*Claude Updates*", "\u2501" * 19]

    if entries:
        lines.append("")
        for row in entries:
            icon = _source_icon(row["source"])
            title = _escape_md(row["title"])
            summary = ""
            if row["summary"]:
                summary = "\n  _" + _escape_md(row["summary"][:180]) + "_"
            lines.append(f"{icon} *{title}*{summary}")

    if releases:
        lines.append("\n\U0001f527 *Releases*")
        for row in releases:
            repo = row["repo"].split("/")[-1]
            tag = _escape_md(row["tag"])
            date = (row["published_at"] or "")[:10]
            body = ""
            if row["body"]:
                first_line = row["body"].strip().splitlines()[0][:180]
                body = "\n  _" + _escape_md(first_line) + "_"
            lines.append(f"  `{date}` {_escape_md(repo)} {tag}{body}")

    return _truncate("\n".join(lines))


def search_capability(query: str) -> str:
    """Search knowledge base for a Claude capability."""
    q = f"%{query}%"
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM changelog_entries
            WHERE title LIKE ? OR capabilities LIKE ? OR summary LIKE ? OR category LIKE ?
            ORDER BY date DESC LIMIT 10
        """, (q, q, q, q)).fetchall()

    if rows:
        lines = [f"*Claude: {_escape_md(query)}*", "\u2501" * 19]
        for row in rows:
            icon = _source_icon(row["source"])
            date = _entry_date(row)
            title = _escape_md(row["title"])
            summary = ""
            if row["summary"]:
                summary = "\n  " + _escape_md(row["summary"][:200])
            caps = ""
            if row["capabilities"]:
                try:
                    cap_list = json.loads(row["capabilities"])
                    caps = "\n  Tags: " + ", ".join(cap_list[:5])
                except (json.JSONDecodeError, TypeError):
                    pass
            lines.append(f"{icon} [{date}] *{title}*{summary}{caps}")
        return _truncate("\n".join(lines))

    logger.info("search_capability(%s): no DB matches, asking Claude", query)
    prompt = (
        f"The user asks: 'Is there a Claude feature, plugin, or tool for {query}?'\n\n"
        f"Based on your knowledge of Claude's capabilities (Claude Code, Claude API, "
        f"MCP servers, plugins, tools, SDK features), answer concisely:\n"
        f"1. Does this capability exist? If yes, what is it called?\n"
        f"2. How to access/enable it\n"
        f"3. Brief example of usage\n"
        f"Keep it under 200 words."
    )
    return claude_llm.chat(prompt, max_tokens=1024)


def explain_feature(feature: str) -> str:
    """LLM-powered explanation of use cases for a Claude feature."""
    q = f"%{feature}%"
    with _conn() as conn:
        rows = conn.execute("""
            SELECT title, summary, category, capabilities, date
            FROM changelog_entries
            WHERE title LIKE ? OR capabilities LIKE ? OR summary LIKE ?
            ORDER BY date DESC LIMIT 5
        """, (q, q, q)).fetchall()

    entries_text = "No specific entries found in knowledge base."
    if rows:
        entries_text = "\n".join(
            f"- [{r['date'] or '?'}] {r['title']}: {r['summary'] or 'no summary'}"
            for r in rows
        )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(PROMPT_PATH) as f:
            template = f.read()
        prompt = template.format(today=today, feature=feature, entries=entries_text)
    except FileNotFoundError:
        prompt = (
            f"Today is {today}. The user wants to understand: \"{feature}\"\n\n"
            f"Known Claude updates:\n{entries_text}\n\n"
            f"Explain: what it does, who benefits, 2-3 use cases, limitations. "
            f"Under 300 words, practical and specific."
        )

    return claude_llm.chat(prompt, max_tokens=2048)


def add_knowledge(text: str) -> str:
    """Manually add an entry to the knowledge base."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if ": " in text:
        title, summary = text.split(": ", 1)
    else:
        title = text
        summary = ""

    entry_hash = hashlib.sha256((today + title).encode()).hexdigest()[:16]

    try:
        with _conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO changelog_entries
                    (entry_hash, source, title, date, summary, category)
                VALUES (?, 'manual', ?, ?, ?, 'feature')
            """, (entry_hash, title.strip(), today, summary.strip()))
        return f"Added to knowledge base: *{_escape_md(title.strip())}*"
    except Exception as e:
        logger.error("add_knowledge failed: %s", e)
        return f"Failed to add entry: {e}"
