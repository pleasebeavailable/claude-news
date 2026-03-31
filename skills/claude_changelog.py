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
                urgency TEXT DEFAULT 'B',
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
                developer_summary TEXT,
                published_at TEXT,
                urgency TEXT DEFAULT 'B',
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                notified BOOLEAN DEFAULT 0,
                UNIQUE(repo, tag)
            );

            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Migrate existing tables — safe to run repeatedly
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    migrations = [
        ("changelog_entries", "urgency", "TEXT DEFAULT 'B'"),
        ("github_releases", "urgency", "TEXT DEFAULT 'B'"),
        ("github_releases", "developer_summary", "TEXT"),
    ]
    for table, col, col_def in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists


_init_db()


# ── Helpers ──────────────────────────────────────────────────────────

def _escape_md(text: str) -> str:
    """Escape _ and * for Telegram legacy Markdown."""
    return text.replace("_", "\\_").replace("*", "\\*")


def _truncate(text: str, limit: int = _MAX_TELEGRAM) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 20] + "\n\n... (truncated)"


def _source_icon(source: str) -> str:
    icons = {
        "anthropic_news": "\U0001f4f0",
        "release_notes": "\U0001f195",
        "status": "\U0001f6a8",
        "github_release": "\U0001f527",
        "github_commit": "\U0001f50c",
        "manual": "\u270f\ufe0f",
    }
    return icons.get(source, "\U0001f4cb")


def _tier_a_icon_for_entry(row) -> str:
    category = (row["category"] or "").lower()
    source = (row["source"] or "").lower()
    summary = (row["summary"] or "").lower()
    title = (row["title"] or "").lower()
    combined = summary + " " + title
    if category == "incident" or source == "status":
        return "\U0001f6a8"  # 🚨
    if any(p in combined for p in ["breaking", "deprecated", "removed ", "migration"]):
        return "\u26a0\ufe0f"  # ⚠️
    if source == "release_notes":
        return "\U0001f195"  # 🆕
    return "\U0001f4e3"  # 📣


def _tier_a_icon_for_release(row) -> str:
    body = (row["body"] or "").lower()
    repo = (row["repo"] or "")
    if any(p in body for p in ["breaking change", "breaking:", "deprecated", "removed "]):
        return "\u26a0\ufe0f"  # ⚠️
    if "claude-code" in repo:
        return "\U0001f527"  # 🔧
    return "\U0001f195"  # 🆕


# ── Core functions ───────────────────────────────────────────────────

def _send_tier_a_entry(row) -> None:
    """Send a single Tier A changelog entry as its own Telegram message."""
    icon = _tier_a_icon_for_entry(row)
    title = _escape_md(row["title"])
    summary = (row["summary"] or "")[:220]
    url = row["url"] or ""

    lines = [f"{icon} *{title}*"]
    if summary:
        lines.append(f"_{_escape_md(summary)}_")
    if url:
        lines.append(url)

    telegram_bot.send("\n".join(lines))


def _send_tier_a_release(row) -> None:
    """Send a single Tier A GitHub release as its own Telegram message."""
    r = dict(row)
    icon = _tier_a_icon_for_release(row)
    repo_short = r["repo"].split("/")[-1]
    tag = r["tag"]
    title = _escape_md(f"{repo_short} {tag}")

    summary = r.get("developer_summary") or ""
    if not summary and r.get("body"):
        # Fall back to first meaningful line of release body
        for line in r["body"].strip().splitlines():
            line = line.strip().lstrip("#- ")
            if len(line) > 20:
                summary = line[:220]
                break

    url = f"https://github.com/{r['repo']}/releases/tag/{r['tag']}"

    lines = [f"{icon} *{title}*"]
    if summary:
        lines.append(f"_{_escape_md(summary)}_")
    lines.append(url)

    telegram_bot.send("\n".join(lines))


def _send_tier_b_digest(entries, releases) -> None:
    """Send a compact digest of Tier B (non-urgent) updates."""
    today = datetime.now(timezone.utc).strftime("%b %d")
    total = len(entries) + len(releases)
    lines = [f"*{total} update{'s' if total != 1 else ''} \u00b7 {today}*", "\u2501" * 19]

    shown = 0
    for row in entries[:_MAX_DIGEST]:
        icon = _source_icon(row["source"])
        title = (row["title"] or "")[:60]
        snippet = (row["summary"] or "")[:80]
        if snippet:
            lines.append(f"{icon} {_escape_md(title)} \u2014 {_escape_md(snippet)}")
        else:
            lines.append(f"{icon} {_escape_md(title)}")
        shown += 1

    for row in releases[:max(0, _MAX_DIGEST - shown)]:
        r = dict(row)
        repo_short = r["repo"].split("/")[-1]
        tag = r["tag"]
        snippet = r.get("developer_summary") or ""
        if not snippet and r.get("body"):
            for line in r["body"].strip().splitlines():
                line = line.strip().lstrip("#- ")
                if len(line) > 20:
                    snippet = line[:80]
                    break
        if snippet:
            lines.append(f"\U0001f527 {_escape_md(repo_short)} {_escape_md(tag)} \u2014 {_escape_md(snippet)}")
        else:
            lines.append(f"\U0001f527 {_escape_md(repo_short)} {_escape_md(tag)}")
        shown += 1

    remaining = total - shown
    if remaining > 0:
        lines.append(f"_...and {remaining} more_")
    lines.append("\n`changelog` for details")

    telegram_bot.send(_truncate("\n".join(lines)))


def process_sync() -> None:
    """Process unnotified entries and send tiered Telegram notifications."""
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

    tier_a_entries = [r for r in entries if (r["urgency"] or "B") == "A"]
    tier_b_entries = [r for r in entries if (r["urgency"] or "B") != "A"]
    tier_a_releases = [r for r in releases if (r["urgency"] or "B") == "A"]
    tier_b_releases = [r for r in releases if (r["urgency"] or "B") != "A"]

    logger.info(
        "Tier A: %d entries + %d releases | Tier B: %d entries + %d releases",
        len(tier_a_entries), len(tier_a_releases),
        len(tier_b_entries), len(tier_b_releases),
    )

    # Tier A: one message per item
    for row in tier_a_entries:
        _send_tier_a_entry(row)
    for row in tier_a_releases:
        _send_tier_a_release(row)

    # Tier B: single digest
    if tier_b_entries or tier_b_releases:
        _send_tier_b_digest(tier_b_entries, tier_b_releases)

    # Mark everything notified
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
            lines.append(f"{icon} *{title}*{summary}{caps}")
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
