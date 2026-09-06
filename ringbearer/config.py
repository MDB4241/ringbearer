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


# The two dials above are the defaults for both routes. Either can be
# overridden for one route alone: the webhook route is a fixed target you speak
# at and walk away from, the MCP route is a conversation you address by name,
# and they do not always want the same treatment. Unset means "use the global",
# which is what every install written before these keys has, so nothing changes
# until one is set. Resolution lives in `telegram.dials_for`, and no route can
# see another route's value.
def optional_flag(key: str) -> bool | None:
    """A true/false setting that may be absent.

    Parameters:
      key (str): the environment variable name.

    Returns: True, False, or None when the key is unset or blank — the caller
      falls back to the global.

    Raises: exits the process naming the key. An unrecognized value would
      otherwise read as `false`, which is a wrong answer nobody would see.
    """
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return None
    if raw not in {"true", "false"}:
        sys.exit(f"{key} must be 'true' or 'false' (got: {raw!r})")
    return raw == "true"


def optional_delivery_context(key: str) -> str | None:
    """A DELIVERY_CONTEXT-shaped setting that may be absent. None when unset or
    blank; exits on anything that is not `conversation` or `one_shot`."""
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return None
    if raw not in {"conversation", "one_shot"}:
        sys.exit(f"{key} must be 'conversation' or 'one_shot' (got: {raw!r})")
    return raw


WEBHOOK_TOPIC_PER_CAPTURE = optional_flag("WEBHOOK_TOPIC_PER_CAPTURE")
WEBHOOK_DELIVERY_CONTEXT = optional_delivery_context("WEBHOOK_DELIVERY_CONTEXT")
MCP_TOPIC_PER_CAPTURE = optional_flag("MCP_TOPIC_PER_CAPTURE")
MCP_DELIVERY_CONTEXT = optional_delivery_context("MCP_DELIVERY_CONTEXT")
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
def slug(name: str) -> str:
    """The roster/enum token for a display name: `Hermes` -> `hermes`.

    Roster names are lowercase tokens because a speech-driven agent has to
    reproduce them; this is the one rule that makes them, and setup asks it
    the same question the environment does.
    """
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "assistant"


# The default assistant's roster/enum name, e.g. Hermes -> hermes. Slugged to
# a lowercase token like every other roster name.
ASSISTANT_SLUG = slug(ASSISTANT_NAME)


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

# The webhook door's fixed destination (A10). The phone sends no routing
# information and the door never reads the words to invent any, so where a
# webhook capture lands is configuration and nothing else. Validated here for
# the same reason ASSISTANTS is: a name that is not in the roster would
# otherwise fail on every capture, silently, from the ring's point of view.
# Which doors this install actually uses. Both are always served — this is a
# declaration, not a switch — but it decides what `setup` and `run` print and
# what `probe` probes, so a one-assistant install is never handed MCP settings
# it will never paste into the phone. Unset means both, which is what every
# install written before this key has: nothing to migrate.
ALL_ROUTES = ("webhook", "mcp")
_routes_raw = os.environ.get("ROUTES", "").strip()
if _routes_raw:
    _named = [name.strip().lower() for name in _routes_raw.split(",")]
    _named = [name for name in _named if name]
    if not _named or any(name not in ALL_ROUTES for name in _named):
        sys.exit(
            "ROUTES must be a comma-separated list of "
            f"{' and '.join(ALL_ROUTES)} (got: {_routes_raw!r}) — edit "
            f"{STATE_DIR / '.env'}"
        )
    # Canonical order, deduplicated: the fast door first, everywhere it is printed.
    ROUTES = tuple(name for name in ALL_ROUTES if name in _named)
else:
    ROUTES = ALL_ROUTES

_webhook_assistant = os.environ.get("WEBHOOK_ASSISTANT", "").strip()
WEBHOOK_ASSISTANT = _webhook_assistant.lower() or DEFAULT_ASSISTANT
if WEBHOOK_ASSISTANT not in ASSISTANT_ROSTER:
    sys.exit(
        f"WEBHOOK_ASSISTANT {_webhook_assistant!r} is not a configured assistant "
        f"(roster: {', '.join(ASSISTANT_ROSTER)}) — edit {STATE_DIR / '.env'}"
    )
