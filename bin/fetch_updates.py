#!/usr/bin/env python3
"""Scheduled agent: fetch Claude/Anthropic updates → write to DB → create .sync_trigger.

Run every 6h via cron or launchd.
No external deps beyond requests (already required by the project).
"""

import hashlib
import logging
import re
import sqlite3
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

# ── Sources ───────────────────────────────────────────────────────────────
GITHUB_REPOS = [
    "anthropics/claude-code",
    "anthropics/anthropic-sdk-python",
    "anthropics/anthropic-sdk-typescript",
    "modelcontextprotocol/python-sdk",
    "modelcontextprotocol/typescript-sdk",
    "anthropics/anthropic-cookbook",
]

DOCS_URLS = [
    "https://docs.anthropic.com/en/release-notes/overview",
    "https://docs.anthropic.com/en/release-notes/api",
    "https://docs.anthropic.com/en/release-notes/claude-code",
]

SITEMAP_URL = "https://www.anthropic.com/sitemap.xml"
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

REQUEST_TIMEOUT = 20
HEADERS = {"User-Agent": "claude-intel-bot/1.0 (+github.com/anthropics)"}

# ── Logging ───────────────────────────────────────────────────────────────
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


# ── DB helpers ────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# ── GitHub releases ───────────────────────────────────────────────────────

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

        with _conn() as conn:
            for rel in releases:
                tag = rel.get("tag_name", "")
                name = rel.get("name") or tag
                body = (rel.get("body") or "")[:1000]
                published_at = rel.get("published_at", "")[:10]
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO github_releases
                               (repo, tag, name, body, published_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (repo, tag, name, body, published_at),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        new += 1
                        log.info("New release: %s %s", repo, tag)
                except sqlite3.Error as e:
                    log.error("DB insert failed (%s %s): %s", repo, tag, e)

    return new


# ── Anthropic news via sitemap ────────────────────────────────────────────

def _slug_to_title(slug: str) -> str:
    """Convert URL slug to readable title as fallback."""
    return slug.replace("-", " ").title()


def _fetch_article_meta(url: str) -> tuple[str | None, str | None]:
    """Fetch (og:title, og:description) from an article page."""
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


# Positive: must match at least one (checked against slug, case-insensitive)
_RELEVANT_KEYWORDS = [
    "claude", "sonnet", "opus", "haiku", "mcp", "model-context-protocol",
    "tool-use", "computer-use", "artifact", "operator", "claude-code",
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


def _is_relevant(slug: str) -> bool:
    s = slug.lower().replace("-", " ")
    if any(skip in s for skip in _SKIP_PATTERNS):
        return False
    return any(kw.replace("-", " ") in s for kw in _RELEVANT_KEYWORDS)


def _is_relevant_title(title: str) -> bool:
    t = title.lower()
    if any(skip in t for skip in _SKIP_PATTERNS):
        return False
    return True  # slug already passed positive check


def fetch_anthropic_news() -> int:
    """Detect new articles via sitemap; fetch titles from individual pages."""
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

    # Find which URLs we haven't stored yet
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

        # Pre-filter by slug — no HTTP request for irrelevant articles
        if not _is_relevant(slug):
            entry_hash = _hash("anthropic_news", url)
            with _conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO changelog_entries
                           (entry_hash, source, title, date, url, category, notified)
                       VALUES (?, 'anthropic_news', ?, ?, ?, 'skipped', 1)""",
                    (entry_hash, _slug_to_title(slug), today, url),
                )
            continue

        # Fetch title + description for relevant articles
        title, summary = _fetch_article_meta(url)
        title = title or _slug_to_title(slug)

        # Secondary filter on real title (slug may have been ambiguous)
        if not _is_relevant_title(title):
            log.info("Skipped after title check: %s", title[:80])
            entry_hash = _hash("anthropic_news", url)
            with _conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO changelog_entries
                           (entry_hash, source, title, date, url, category, notified)
                       VALUES (?, 'anthropic_news', ?, ?, ?, 'skipped', 1)""",
                    (entry_hash, title, today, url),
                )
            continue

        entry_hash = _hash("anthropic_news", url)
        with _conn() as conn:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO changelog_entries
                           (entry_hash, source, title, date, url, summary, category)
                       VALUES (?, 'anthropic_news', ?, ?, ?, ?, 'announcement')""",
                    (entry_hash, title, today, url, summary),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    new += 1
                    log.info("New article: %s", title[:80])
            except sqlite3.Error as e:
                log.error("DB insert failed (%s): %s", url, e)

    return new


# ── Docs change detection ─────────────────────────────────────────────────

def fetch_docs_changes() -> int:
    new = 0
    for url in DOCS_URLS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            log.error("Docs fetch failed (%s): %s", url, e)
            continue

        # Strip scripts/styles/attrs to avoid hashing dynamic session data
        stable_text = re.sub(r'<script[^>]*>.*?</script>', '', resp.text, flags=re.DOTALL)
        stable_text = re.sub(r'<style[^>]*>.*?</style>', '', stable_text, flags=re.DOTALL)
        stable_text = re.sub(r'<[^>]+>', '', stable_text)
        stable_text = re.sub(r'\s+', ' ', stable_text).strip()
        content_hash = hashlib.sha256(stable_text.encode()).hexdigest()[:32]
        now = datetime.now(timezone.utc).isoformat()

        with _conn() as conn:
            existing = conn.execute(
                "SELECT content_hash FROM docs_snapshots WHERE url=?", (url,)
            ).fetchone()

            if existing is None:
                # First time seeing this URL — store snapshot, no notification
                conn.execute(
                    "INSERT INTO docs_snapshots (url, content_hash, checked_at) VALUES (?, ?, ?)",
                    (url, content_hash, now),
                )
                log.info("Docs baseline stored: %s", url)

            elif existing["content_hash"] != content_hash:
                # Content changed — update snapshot and add changelog entry
                conn.execute(
                    "UPDATE docs_snapshots SET content_hash=?, checked_at=? WHERE url=?",
                    (content_hash, now, url),
                )
                entry_hash = _hash("docs_change", url, content_hash)
                page = url.split("/")[-1].replace("-", " ").title()
                title = f"Docs updated: {page}"
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                conn.execute(
                    """INSERT OR IGNORE INTO changelog_entries
                           (entry_hash, source, title, date, url, category)
                       VALUES (?, 'docs', ?, ?, ?, 'docs')""",
                    (entry_hash, title, today, url),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    new += 1
                    log.info("Docs changed: %s", url)
            else:
                log.info("Docs unchanged: %s", url)

    return new


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    log.info("=== fetch_updates starting ===")

    total = 0
    total += fetch_github_releases()
    total += fetch_anthropic_news()
    total += fetch_docs_changes()

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
