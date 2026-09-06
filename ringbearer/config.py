"""Everything the process learns before it does anything: where state lives,
what the environment says, and whether that adds up to a runnable install.

Imported by every other module, and deliberately the only place that reads
os.environ. The sys.exit() calls here fire at import time on purpose — a
misconfigured install should die while someone is looking at a terminal, not
on the first capture from a ring on a walk.

Names here are read through this module (`config.TELEGRAM_ENABLED`), never
copied into another module's namespace: tests rebind several of them, and a
by-value copy would go stale the moment they did.
"""

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

# Keep the room quiet: Telethon narrates connections and updates, and the MCP
# SDK logs "Terminating session" per stateless request — noise that buries the
# output that matters. Set here because every entry point imports this module.
import logging

logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

# The checkout root — the directory holding the ringbearer.py launcher, .venv
# and logs/. One level up from this package.
HERE = Path(__file__).parent.parent
# Private mutable state (.env, the Telegram session, captures.jsonl) lives in
# RINGBEARER_STATE_DIR when set — the seam that keeps a Docker image
# disposable while user data persists. Unset, it IS the checkout, and nothing
# changes for native installs. Process environment only, never .env: this
# value decides where .env is. Must be absolute — a relative path would move
# with the launching directory, silently splitting state between cwds; empty
# means unset, not cwd.
_state_raw = os.environ.get("RINGBEARER_STATE_DIR", "").strip()
if _state_raw and not Path(_state_raw).expanduser().is_absolute():
    sys.exit(f"RINGBEARER_STATE_DIR must be an absolute path (got: {_state_raw!r})")
STATE_DIR = (Path(_state_raw).expanduser() if _state_raw else HERE).resolve()
load_dotenv(STATE_DIR / ".env")

CAPTURES = STATE_DIR / "captures.jsonl"
DRY_RUN_PREFIX = "DRYRUN:"

BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
TELEGRAM_ENABLED = os.environ.get("TELEGRAM_ENABLED", "false").lower() == "true"
NEW_TOPIC_PER_CAPTURE = (
    os.environ.get("NEW_TOPIC_PER_CAPTURE", "false").lower() == "true"
)
# @username, or a bare numeric chat id. A digits-only STRING gets resolved as
# a phone number, so numeric ids are coerced to int. Numeric ids are best-
# effort in Telethon — they resolve only from the session file's own entity
# cache, which starts EMPTY after a fresh login (using the chat in the
# official apps populates nothing here). The server therefore resolves the
# chat once at startup and fails fast with advice, instead of failing on
# every send. @username always works.
_chat = os.environ.get("ASSISTANT_CHAT", "")
ASSISTANT_CHAT = int(_chat) if re.fullmatch(r"-?\d+", _chat) else _chat
ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "assistant")
RING_PREFIX = os.environ.get("RING_PREFIX", "\U0001f3a4 ")
DELIVERY_CONTEXT = os.environ.get("DELIVERY_CONTEXT", "conversation").lower()
if DELIVERY_CONTEXT not in {"conversation", "one_shot"}:
    sys.exit(
        "DELIVERY_CONTEXT must be 'conversation' or 'one_shot' "
        f"(got: {DELIVERY_CONTEXT!r})"
    )
TG_API_ID = os.environ.get("TG_API_ID", "")
if TG_API_ID and not TG_API_ID.isdigit():
    sys.exit(f"TG_API_ID in .env is not a number — edit {STATE_DIR / '.env'}")
TG_API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_NAME = os.environ.get("SESSION_NAME", "ringbearer")
# Telethon appends ".session" only when the name lacks it — a name already
# carrying the suffix would desync every existence check and the post-login
# chmod (checks would look for name.session.session). Normalize once here.
if SESSION_NAME.endswith(".session"):
    SESSION_NAME = SESSION_NAME[: -len(".session")]
# A bare file name only: a separator or dot-dot would silently escape
# STATE_DIR's containment promise (Path("/data") / "/tmp/x" IS /tmp/x).
if "/" in SESSION_NAME or SESSION_NAME in ("", ".", ".."):
    sys.exit(f"SESSION_NAME must be a bare file name, not a path (got: {SESSION_NAME!r})")
# The faster-whisper model the webhook door uses when the phone sends audio
# instead of a transcript. `base.en` is the starting point; `small.en` is
# slower and sharper. Anything in transcribe.DISABLED_VALUES ("", "off",
# "none") turns transcription off entirely — an install whose phone always
# sends a `transcription` part never needs the model.
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "base.en").strip()
MCP_MOUNT = os.environ.get("MCP_MOUNT", "/ringbearer")
BIND_HOST = os.environ.get("BIND_HOST", "")
try:
    BIND_PORT = int(os.environ.get("BIND_PORT", "8787"))
except ValueError:
    sys.exit(f"BIND_PORT in .env is not a number — edit {STATE_DIR / '.env'}")

# The tool name is fixed and generic: the destination is configuration, not
# identity, so the name never has to change when assistants are renamed or
# added — and `probe` works against any install without matching its config.
# ASSISTANT_NAME still personalizes everything the app's LLM reads (the tool
# description) and everything the user reads (acks, setup text).
TOOL_NAME = "send_to_assistant"
# The default assistant's roster/enum name, e.g. Hermes -> hermes. Slugged to
# a lowercase token like every other roster name.
ASSISTANT_SLUG = re.sub(r"[^a-z0-9_]+", "_", ASSISTANT_NAME.lower()).strip("_") or "assistant"


def parse_assistants(raw: str) -> dict:
    """Extra assistants from ASSISTANTS: comma-separated name:chat pairs,
    e.g. `plutus:@plutus_bot,quartermaster:@qm_bot`. Names are lowercase
    tokens — they become enum values a speech-driven agent must reproduce,
    so keep them short and speakable. Chats take the same forms as
    ASSISTANT_CHAT. Raises ValueError naming the bad entry; blank means no
    extras."""
    roster: dict = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        name, sep, chat = (s.strip() for s in pair.partition(":"))
        if not sep or not chat:
            raise ValueError(f"{pair!r} is not a name:chat pair")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(
                f"assistant name {name!r} must be a lowercase token "
                "(letters, digits, underscores; starts with a letter)"
            )
        if name in roster:
            raise ValueError(f"duplicate assistant name {name!r}")
        roster[name] = int(chat) if re.fullmatch(r"-?\d+", chat) else chat
    return roster


try:
    _extras = parse_assistants(os.environ.get("ASSISTANTS", ""))
except ValueError as e:
    sys.exit(f"ASSISTANTS in .env is invalid: {e} — edit {STATE_DIR / '.env'}")
if ASSISTANT_SLUG in _extras:
    sys.exit(
        f"ASSISTANTS name {ASSISTANT_SLUG!r} collides with ASSISTANT_NAME — "
        f"the default assistant already answers to it. Edit {STATE_DIR / '.env'}."
    )
# The full routing table, default first. A single-assistant install has
# exactly one row and never sees any of the multi-assistant machinery.
DEFAULT_ASSISTANT = ASSISTANT_SLUG
ASSISTANT_ROSTER = {DEFAULT_ASSISTANT: ASSISTANT_CHAT, **_extras}
