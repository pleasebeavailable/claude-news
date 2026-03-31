"""Claude Intel Bot — Telegram bot for Claude changelog, knowledge base, and ideas."""

import logging
import logging.handlers
import os
import re
import subprocess
import sys
import threading
import time

# Load .env
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                if _line.startswith("export "):
                    _line = _line[7:]
                _k, _v = _line.split("=", 1)
                _v = _v.strip()
                if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ("'", '"'):
                    _v = _v[1:-1]
                else:
                    _v = _v.split("#", 1)[0].strip()
                os.environ.setdefault(_k.strip(), _v)

# ── Logging ──────────────────────────────────────────────────────────
_LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s — %(message)s"

_formatter = logging.Formatter(_LOG_FORMAT)
_handlers = []

_app_handler = logging.handlers.TimedRotatingFileHandler(
    os.path.join(_LOG_DIR, "app.log"),
    when="midnight", backupCount=30, utc=True,
)
_app_handler.setFormatter(_formatter)
_handlers.append(_app_handler)

_console = logging.StreamHandler()
_console.setFormatter(_formatter)
_handlers.append(_console)

logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=_handlers)
logger = logging.getLogger(__name__)

# ── Imports ──────────────────────────────────────────────────────────
from core import telegram_bot, claude_llm
from skills import claude_changelog, ideas

# ── Skill discovery ──────────────────────────────────────────────────
_COMMANDS = []
for _mod in (claude_changelog, ideas):
    for _cmd in getattr(_mod, "COMMANDS", []):
        _COMMANDS.append((_cmd, _mod))

_type_order = {"exact": 0, "prefix": 1, "regex": 2}
_COMMANDS.sort(key=lambda c: (_type_order.get(c[0]["type"], 9), c[0].get("priority", 50)))

# ── Chat history ─────────────────────────────────────────────────────
_SYSTEM_PROMPT = (
    "You are Claude Intel, a helpful assistant focused on Claude/Anthropic "
    "features, plugins, tools, and capabilities. Be concise and practical."
)
_CHAT_HISTORY: list[dict] = []
_CHAT_HISTORY_MAX = 20
_CHAT_LOCK = threading.Lock()


# ── Command routing ──────────────────────────────────────────────────
def _help_text() -> str:
    sections = []
    seen = set()
    for _, mod in _COMMANDS:
        if mod.__name__ not in seen:
            seen.add(mod.__name__)
            h = getattr(mod, "HELP", None)
            if h:
                order = getattr(mod, "HELP_ORDER", 99)
                sections.append((order, h))
    sections.sort(key=lambda x: x[0])
    return (
        "*Claude Intel — Commands*\n\n"
        + "\n\n".join(s for _, s in sections)
        + "\n\n*System*\n`help` — this message"
    )


def _try_command(cmd, mod, text, text_lower):
    ctype = cmd["type"]
    if ctype == "exact":
        if text_lower != cmd["pattern"]:
            return None
        args = ()
    elif ctype == "prefix":
        if not text_lower.startswith(cmd["pattern"]):
            return None
        args = (text[len(cmd["pattern"]):].strip(),)
    elif ctype == "regex":
        m = re.match(cmd["pattern"], text)
        if not m:
            return None
        arg_mode = cmd.get("args")
        if arg_mode == "upper":
            args = tuple(g.upper() for g in m.groups())
        elif arg_mode == "raw":
            args = m.groups()
        else:
            args = ()
    else:
        return None

    func = getattr(mod, cmd["call"])
    logger.info("routing: %s.%s", mod.__name__, cmd["call"])
    return func(*args)


def handle_message(text: str) -> str:
    t = text.strip()
    tl = t.lower()

    if tl in ("help", "/help", "/start"):
        return _help_text()

    for cmd, mod in _COMMANDS:
        result = _try_command(cmd, mod, t, tl)
        if result is not None:
            return result

    # Free-form chat via Claude
    logger.info("routing: free-form chat")
    with _CHAT_LOCK:
        _CHAT_HISTORY.append({"role": "user", "content": t})
        if len(_CHAT_HISTORY) > _CHAT_HISTORY_MAX:
            del _CHAT_HISTORY[:-_CHAT_HISTORY_MAX]
        history_text = "\n".join(
            f"{m['role']}: {m['content']}" for m in _CHAT_HISTORY
        )

    prompt = _SYSTEM_PROMPT + "\n\nConversation:\n" + history_text
    reply = claude_llm.chat(prompt, max_tokens=2048)

    with _CHAT_LOCK:
        _CHAT_HISTORY.append({"role": "assistant", "content": reply})
        if len(_CHAT_HISTORY) > _CHAT_HISTORY_MAX:
            del _CHAT_HISTORY[:-_CHAT_HISTORY_MAX]

    return reply


# ── Sync trigger checker ────────────────────────────────────────────
_TRIGGER_PATH = os.path.join(os.path.dirname(__file__), "data", ".sync_trigger")
_LAST_FETCH_PATH = os.path.join(os.path.dirname(__file__), "data", ".last_fetch_time")
_FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "bin", "fetch_updates.py")
_FETCH_INTERVAL = 6 * 3600  # 6 hours


def _read_last_fetch() -> float:
    try:
        with open(_LAST_FETCH_PATH) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def _sync_checker():
    """Background thread: catch-up fetch on wake + process sync trigger every 30s."""
    while True:
        elapsed = time.time() - _read_last_fetch()
        if elapsed > _FETCH_INTERVAL:
            logger.info("%.1fh since last fetch — running catch-up", elapsed / 3600)
            try:
                subprocess.run([sys.executable, _FETCH_SCRIPT], timeout=120, check=False)
            except Exception as e:
                logger.error("Catch-up fetch failed: %s", e)

        if os.path.exists(_TRIGGER_PATH):
            try:
                claude_changelog.process_sync()
                os.remove(_TRIGGER_PATH)
                logger.info("Processed sync trigger")
            except Exception as e:
                logger.error("Sync trigger failed: %s", e)

        time.sleep(30)


# ── Poll loop ────────────────────────────────────────────────────────
_OFFSET_FILE = os.path.join(os.path.dirname(__file__), "data", ".offset")


def _load_offset() -> int:
    try:
        with open(_OFFSET_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def _save_offset(offset: int):
    try:
        os.makedirs(os.path.dirname(_OFFSET_FILE), exist_ok=True)
        with open(_OFFSET_FILE, "w") as f:
            f.write(str(offset))
    except Exception as e:
        logger.warning("Could not save offset: %s", e)


def poll_loop():
    offset = _load_offset()
    logger.info("Poll loop started (offset=%d)", offset)
    while True:
        try:
            updates = telegram_bot.get_updates(offset=offset)
            for update in updates:
                offset = update["update_id"] + 1
                _save_offset(offset)
                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue
                if msg.get("chat", {}).get("id") != telegram_bot.chat_id():
                    continue
                text = msg.get("text", "").strip()
                if not text:
                    continue
                logger.info("Received: %r", text)
                reply = handle_message(text)
                if reply:
                    telegram_bot.send(reply)
        except Exception as e:
            logger.error("Poll loop error: %s", e)
            time.sleep(5)


# ── Main ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Claude Intel starting — %d commands", len(_COMMANDS))

    # Startup catch-up
    try:
        claude_changelog.process_sync()
    except Exception as e:
        logger.warning("Startup sync failed: %s", e)

    # Background sync checker
    threading.Thread(target=_sync_checker, daemon=True).start()

    telegram_bot.send("Claude Intel online. Send `help` for commands.")
    logger.info("Startup message sent")
    poll_loop()
