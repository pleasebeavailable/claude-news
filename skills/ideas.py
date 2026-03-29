"""Skill: Ideas Inbox — save and list ideas via Telegram."""

import logging

from skills.claude_changelog import _conn, _escape_md, _truncate

logger = logging.getLogger(__name__)

# ── Skill metadata (auto-discovery) ──────────────────────────────────
COMMANDS = [
    {"type": "prefix", "pattern": "idea ", "call": "save_idea"},
    {"type": "exact", "pattern": "ideas", "call": "list_ideas"},
]
SCHEDULE = []
HELP_ORDER = 2
HELP = "*Ideas*\n`idea <text>` — save an idea\n`ideas` — list all ideas"


def save_idea(text: str) -> str:
    """Save an idea to the database."""
    text = text.strip()
    if not text:
        return "Empty idea — nothing saved."

    try:
        with _conn() as conn:
            conn.execute("INSERT INTO ideas (text) VALUES (?)", (text,))
            row = conn.execute("SELECT last_insert_rowid() as id").fetchone()
            idea_id = row["id"] if row else "?"
        logger.info("Idea #%s saved: %s", idea_id, text[:60])
        return f"Idea #{idea_id} saved."
    except Exception as e:
        logger.error("save_idea failed: %s", e)
        return f"Failed to save idea: {e}"


def list_ideas() -> str:
    """List all saved ideas."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, text, created_at FROM ideas ORDER BY created_at DESC"
        ).fetchall()

    if not rows:
        return "No ideas saved yet. Use `idea <text>` to add one."

    lines = ["*Ideas*", "\u2501" * 19]
    for row in rows:
        date = (row["created_at"] or "")[:10]
        text = _escape_md(row["text"])
        lines.append(f"{row['id']}. [{date}] {text}")

    return _truncate("\n".join(lines))
