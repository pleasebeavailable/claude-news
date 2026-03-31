#!/usr/bin/env python3
"""Scheduled agent: fetch Claude/Anthropic updates → write to DB → create .sync_trigger.

Run every 6h via cron or launchd.
No external deps beyond requests (already required by the project).
"""

import hashlib
import logging
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Paths ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "claude_knowledge.db"
TRIGGER_PATH = ROOT / "data" / ".sync_trigger"
LAST_FETCH_PATH = ROOT / "data" / ".last_fetch_time"
LOG_PATH = ROOT / "logs" / "fetch.log"

# ── Sources ────────────────────────────────────────────────────────────────
GITHUB_REPOS = [
    "anthropics/claude-code",
    "anthropics/anthropic-sdk-python",
    "anthropics/anthropic-sdk-typescript",
    "modelcontextprotocol/python-sdk",
    "modelcontextprotocol/typescript-sdk",
    "modelcontextprotocol/servers",
    "anthropics/anthropic-cookbook",
]

# API release notes — parsed as structured dated entries (not hash-compared)
RELEASE_NOTE_URLS = [
    "https://docs.anthropic.com/en/release-notes/api",
]

STATUS_API = "https://status.anthropic.com/api/v2/incidents/unresolved.json"
SITEMAP_URL = "https://www.anthropic.com/sitemap.xml"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}
HEADERS = {"User-Agent": "claude-intel-bot/1.0 (+github.com/anthropics)"}
REQUEST_TIMEOUT = 20

# Positive: slug must match at least one (checked case-insensitively)
_RELEVANT_KEYWORDS = [
    "claude", "sonnet", "opus", "haiku", "mcp", "model-context-protocol",
    "tool-use", "computer-use", "artifact", "operator", "claude-code", "claude-desktop",
    "extended-thinking", "vision", "multimodal",
]

# Negative: if ANY of these appear in slug OR title, skip the article
_SKIP_PATTERNS = [
    "chooses", "selects", "powers customer", "powers llnl", "increases productivity",
    "partner network", "economic index", "branches of government", "fedramp",
    "il4", "il5", "for nonprofits", "for life sciences", "for financial services",
    "for enterprise", "investment in", "invests", "expands to", "comes to",
    "joins", "available in brazil", "available in canada", "available in the eu",
    "available in the uk", "case study",
]

# Release body patterns that indicate a breaking/important change
_BREAKING_PATTERNS = [
    "breaking change", "breaking:", "breaking -", "deprecated", "deprecation",
    "migration required", "removed ", "incompatible",
]

# ── Logging ────────────────────────────────────────────────────────────────
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("fetch_updates")


# ── DB helpers ─────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# ── LLM summarization ──────────────────────────────────────────────────────

def _llm_summarize(context: str, body: str) -> str | None:
    """Ask Claude to produce a 1-sentence developer-impact summary of release notes."""
    if not body or len(body.strip()) < 80:
        return None
    prompt = (
        f"Release notes for: {context}\n\n"
        f"{body[:1500]}\n\n"
        f"Write exactly 1 sentence (max 20 words) describing the developer impact. "
        f"Be specific: name new commands/features, breaking changes, or key fixes. "
        f"No filler phrases like 'this release includes'."
    )
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            summary = result.stdout.strip()
            # Keep only the first sentence
            first = re.split(r"(?<=[.!?])\s", summary)[0].strip()
            return first if first else None
    except Exception as e:
        log.warning("LLM summarize failed (%s): %s", context, e)
    return None


# ── Urgency classification ─────────────────────────────────────────────────

def _release_urgency(repo: str, tag: str, body: str) -> str:
    """Return 'A' for Tier A (send individually), 'B' for digest."""
    # claude-code is always Tier A — user uses it every day
    if repo == "anthropics/claude-code":
        return "A"
    body_lower = (body or "").lower()
    if any(p in body_lower for p in _BREAKING_PATTERNS):
        return "A"
    # Major version bump (vN.0.0)
    if re.match(r"v?\d+\.0\.0$", tag):
        return "A"
    return "B"


def _entry_urgency(source: str, content: str) -> str:
    """Return 'A' for release note entries that describe new models or breaking changes."""
    c = content.lower()
    if source == "status":
        return "A"
    if any(p in c for p in ["breaking", "deprecated", "migration required"]):
        return "A"
    # New model announcement
    if re.search(r"claude-\d", c) and any(kw in c for kw in ["new", "available", "launch", "introduc", "released"]):
        return "A"
    if "new model" in c or "introducing claude" in c:
        return "A"
    return "B"


# ── GitHub releases ────────────────────────────────────────────────────────

def fetch_github_releases() -> int:
    new = 0
    for repo in GITHUB_REPOS:
        url = f"https://api.github.com/repos/{repo}/releases"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, params={"per_page": 5})
            if resp.status_code == 404:
                log.warning("GitHub 404: %s", repo)
                continue
            resp.raise_for_status()
            releases = resp.json()
        except Exception as e:
            log.error("GitHub fetch failed (%s): %s", repo, e)
            continue

        # Identify new releases without holding a DB connection during LLM calls
        new_rels = []
        with _conn() as conn:
            for rel in releases:
                tag = rel.get("tag_name", "")
                existing = conn.execute(
                    "SELECT id FROM github_releases WHERE repo=? AND tag=?", (repo, tag)
                ).fetchone()
                if not existing:
                    new_rels.append(rel)

        # LLM summarization happens outside the DB connection
        for rel in new_rels:
            tag = rel.get("tag_name", "")
            name = rel.get("name") or tag
            body = (rel.get("body") or "")[:1000]
            published_at = rel.get("published_at", "")[:10]
            urgency = _release_urgency(repo, tag, body)
            dev_summary = _llm_summarize(f"{repo} {tag}", body)

            with _conn() as conn:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO github_releases
                               (repo, tag, name, body, developer_summary, published_at, urgency)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (repo, tag, name, body, dev_summary, published_at, urgency),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        new += 1
                        log.info("New release [%s]: %s %s", urgency, repo, tag)
                except sqlite3.Error as e:
                    log.error("DB insert failed (%s %s): %s", repo, tag, e)

    return new


# ── Anthropic news via sitemap ─────────────────────────────────────────────

def _slug_to_title(slug: str) -> str:
    return slug.replace("-", " ").title()


def _fetch_article_meta(url: str) -> tuple[str | None, str | None]:
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None, None
        title = None
        desc = None
        m = re.search(r'og:title[^>]+content="([^"]+)"', r.text)
        if m:
            title = m.group(1).strip()
        else:
            m = re.search(r'<title>(.*?)</title>', r.text)
            if m:
                title = re.sub(r'\s*[|\\]\s*Anthropic.*$', '', m.group(1)).strip() or None
        m = re.search(r'og:description[^>]+content="([^"]+)"', r.text)
        if m:
            desc = m.group(1).strip()
        return title, desc
    except Exception:
        return None, None


def _is_relevant(slug: str) -> bool:
    s = slug.lower().replace("-", " ")
    if any(skip in s for skip in _SKIP_PATTERNS):
        return False
    return any(kw.replace("-", " ") in s for kw in _RELEVANT_KEYWORDS)


def _is_relevant_title(title: str) -> bool:
    t = title.lower()
    if any(skip in t for skip in _SKIP_PATTERNS):
        return False
    return True


def fetch_anthropic_news() -> int:
    try:
        resp = requests.get(SITEMAP_URL, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        log.error("Sitemap fetch failed: %s", e)
        return 0

    all_urls = re.findall(
        r'<loc>(https://www\.anthropic\.com/news/[a-z0-9][a-z0-9\-]+)</loc>',
        resp.text,
    )
    log.info("Sitemap: %d news URLs found", len(all_urls))

    with _conn() as conn:
        known = {
            row[0] for row in conn.execute(
                "SELECT url FROM changelog_entries WHERE source='anthropic_news'"
            ).fetchall()
        }

    new_urls = [u for u in all_urls if u not in known]
    log.info("New news URLs to process: %d", len(new_urls))

    new = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for url in new_urls:
        slug = url.split("/news/")[-1]

        if not _is_relevant(slug):
            entry_hash = _hash("anthropic_news", url)
            with _conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO changelog_entries
                           (entry_hash, source, title, date, url, category, urgency, notified)
                       VALUES (?, 'anthropic_news', ?, ?, ?, 'skipped', 'B', 1)""",
                    (entry_hash, _slug_to_title(slug), today, url),
                )
            continue

        title, summary = _fetch_article_meta(url)
        title = title or _slug_to_title(slug)

        if not _is_relevant_title(title):
            log.info("Skipped after title check: %s", title[:80])
            entry_hash = _hash("anthropic_news", url)
            with _conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO changelog_entries
                           (entry_hash, source, title, date, url, category, urgency, notified)
                       VALUES (?, 'anthropic_news', ?, ?, ?, 'skipped', 'B', 1)""",
                    (entry_hash, title, today, url),
                )
            continue

        entry_hash = _hash("anthropic_news", url)
        urgency = _entry_urgency("anthropic_news", (title or "") + " " + (summary or ""))
        with _conn() as conn:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO changelog_entries
                           (entry_hash, source, title, date, url, summary, category, urgency)
                       VALUES (?, 'anthropic_news', ?, ?, ?, ?, 'announcement', ?)""",
                    (entry_hash, title, today, url, summary, urgency),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    new += 1
                    log.info("New article [%s]: %s", urgency, title[:80])
            except sqlite3.Error as e:
                log.error("DB insert failed (%s): %s", url, e)

    return new


# ── API release notes (structured entry parsing) ───────────────────────────

def _parse_iso_date(date_str: str) -> str:
    """Convert 'February 27, 2025' → '2025-02-27'. Falls back to today on failure."""
    try:
        normalized = date_str.replace(",", "").strip()
        return datetime.strptime(normalized, "%B %d %Y").strftime("%Y-%m-%d")
    except ValueError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


_DATE_HEADING_RE = re.compile(
    r"((?:January|February|March|April|May|June|July|August"
    r"|September|October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


def _parse_release_note_sections(html: str) -> list[tuple[str, str]]:
    """Parse dated sections from a release notes page.

    Returns list of (date_str, content_text) pairs.
    """
    # Split on h2/h3 opening tags — each piece starts with the heading content
    parts = re.split(r"<h[23][^>]*>", html)
    results = []

    for part in parts:
        # Isolate the heading text (up to </h2> or </h3>)
        close = re.search(r"</h[23]>", part)
        if not close:
            continue
        heading_raw = part[: close.start()]
        heading_text = re.sub(r"<[^>]+>", "", heading_raw).strip()

        m = _DATE_HEADING_RE.search(heading_text)
        if not m:
            continue
        date_str = m.group(1)

        # Extract text content after the heading close tag
        body_html = part[close.end():]
        # Strip scripts/styles
        body_html = re.sub(r"<script[^>]*>.*?</script>", "", body_html, flags=re.DOTALL)
        body_html = re.sub(r"<style[^>]*>.*?</style>", "", body_html, flags=re.DOTALL)
        # Tags → spaces, decode basic entities
        content = re.sub(r"<[^>]+>", " ", body_html)
        content = re.sub(r"&nbsp;", " ", content)
        content = re.sub(r"&amp;", "&", content)
        content = re.sub(r"&lt;", "<", content)
        content = re.sub(r"&gt;", ">", content)
        content = re.sub(r"\s+", " ", content).strip()

        if content:
            results.append((date_str, content[:600]))

    return results


def fetch_release_note_entries() -> int:
    new = 0

    for url in RELEASE_NOTE_URLS:
        try:
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            log.error("Release notes fetch failed (%s): %s", url, e)
            continue

        sections = _parse_release_note_sections(resp.text)
        if not sections:
            log.warning(
                "Release notes %s: 0 sections found — page may be JS-rendered or structure changed",
                url.split("/")[-1],
            )
            continue
        log.info("Release notes %s: %d sections found", url.split("/")[-1], len(sections))

        for date_str, content in sections:
            entry_hash = _hash("release_notes", url, date_str)
            urgency = _entry_urgency("release_notes", content)
            iso_date = _parse_iso_date(date_str)

            # Build a concise title from the first meaningful phrase
            first_sentence = re.split(r"[.!?\n]", content)[0].strip()[:100]
            title = first_sentence if first_sentence else f"API update · {date_str}"

            with _conn() as conn:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO changelog_entries
                               (entry_hash, source, title, date, url, summary, category, urgency)
                           VALUES (?, 'release_notes', ?, ?, ?, ?, 'api_update', ?)""",
                        (entry_hash, title, iso_date, url, content[:300], urgency),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        new += 1
                        log.info("New release note [%s]: %s — %s", urgency, date_str, title[:60])
                except sqlite3.Error as e:
                    log.error("DB insert failed (release note %s): %s", date_str, e)

    return new


# ── Anthropic status page ──────────────────────────────────────────────────

def fetch_status_page() -> int:
    try:
        resp = requests.get(STATUS_API, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("Status page fetch failed: %s", e)
        return 0

    incidents = data.get("incidents", [])
    if not incidents:
        log.info("Status page: no active incidents")
        return 0

    new = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with _conn() as conn:
        for inc in incidents:
            inc_id = inc.get("id", "")
            name = inc.get("name", "Unknown incident")
            shortlink = inc.get("shortlink", "https://status.anthropic.com")
            updates = inc.get("incident_updates", [])
            summary = updates[0].get("body", "")[:300] if updates else ""

            entry_hash = _hash("status", inc_id)
            title = f"API Incident: {name}"

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO changelog_entries
                           (entry_hash, source, title, date, url, summary, category, urgency)
                       VALUES (?, 'status', ?, ?, ?, ?, 'incident', 'A')""",
                    (entry_hash, title, today, shortlink, summary),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    new += 1
                    log.info("New incident [A]: %s", name)
            except sqlite3.Error as e:
                log.error("DB insert failed (incident %s): %s", inc_id, e)

    return new


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    log.info("=== fetch_updates starting ===")

    total = 0
    total += fetch_status_page()        # Check incidents first (highest priority)
    total += fetch_github_releases()
    total += fetch_anthropic_news()
    total += fetch_release_note_entries()

    log.info("=== fetch_updates done: %d new items ===", total)

    LAST_FETCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_FETCH_PATH.write_text(str(time.time()))

    if total > 0:
        TRIGGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRIGGER_PATH.touch()
        log.info("Trigger file written: %s", TRIGGER_PATH)
    else:
        log.info("No new items — trigger not written")


if __name__ == "__main__":
    main()
