"""Claude Sonnet LLM client via claude CLI subprocess.

Falls back gracefully if the Claude CLI is unavailable.
"""

import logging
import subprocess
import threading

logger = logging.getLogger(__name__)

_CLAUDE_LOCK = threading.Lock()
_CLAUDE_PATH = "claude"


def chat(prompt: str, max_tokens: int = 2048, timeout: int = 120) -> str:
    """Send prompt to Claude Sonnet via CLI. Returns response text.

    Uses stdin piping to avoid shell argument length limits.
    Thread-safe via _CLAUDE_LOCK.
    """
    logger.info("Claude CLI call — %d chars, timeout=%ds", len(prompt), timeout)
    try:
        with _CLAUDE_LOCK:
            result = subprocess.run(
                [_CLAUDE_PATH, "-p", "--output-format", "text"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        if result.returncode != 0:
            logger.error("Claude CLI error (rc=%d): %s",
                         result.returncode, result.stderr[:200])
            return "Sorry, Claude is temporarily unavailable."
        response = result.stdout.strip()
        logger.info("Claude CLI response — %d chars", len(response))
        return response
    except subprocess.TimeoutExpired:
        logger.warning("Claude CLI timed out after %ds", timeout)
        return "Sorry, Claude timed out. Try again."
    except FileNotFoundError:
        logger.warning("Claude CLI not found at '%s'", _CLAUDE_PATH)
        return "Claude CLI not installed. Install claude-code first."


def is_available() -> bool:
    """Check if claude CLI is reachable."""
    try:
        r = subprocess.run(
            [_CLAUDE_PATH, "--version"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False
