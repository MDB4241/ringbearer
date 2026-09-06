"""The link to Telegram, and everything that rides on it.

The client itself, the health view that decides what /healthz says, the
supervisor that keeps the connection alive across outages, and the delivery
path every capture takes into the assistant's DM — `relay`, which both routes
call and neither duplicates.

`tg_client`, `recheck_now`, `tg_health` and `assistant_entities` are rebound
at runtime — by lifespan at startup, and by tests. Everything in this module
reads them as its own module globals, and everything outside it reads them
through this module object (`telegram.tg_client`), never as a by-value copy.
"""

import asyncio
import json
import random
import time
from contextlib import suppress
from contextvars import ContextVar
from datetime import datetime
from typing import NamedTuple

from . import config

tg_client = None
assistant_entities: dict = {}  # roster name -> entity, resolved at startup; see lifespan


def make_tg_client(*, serving: bool = False):
    from telethon import TelegramClient

    # The session path is explicit and absolute, so the server and
    # `ringbearer.py login` always share one file no matter which directory
    # launched the process (uvicorn, launchd, a shell — all the same).
    # No flood_sleep_threshold override here: this factory also serves
    # login/setup, where Telethon's default sleeping is the right behavior.
    # The serving-phase policy is set in lifespan, after the startup probes.
    #
    # serving=True hands reconnection to connection_supervisor(). Telethon's
    # own policy is five attempts a second apart and then a permanent teardown
    # (mtprotosender.py: _reconnect falls through to _disconnect), which is how
    # a brief home-internet drop used to end the bridge for good while the
    # process stayed up. Two loops racing to reconnect would each be half in
    # charge, so this one is switched off and the policy lives in one place.
    policy = (
        {"connection_retries": 0, "retry_delay": 0, "auto_reconnect": False}
        if serving
        else {}
    )
    return TelegramClient(
        str(config.STATE_DIR / config.SESSION_NAME),
        int(config.TG_API_ID),
        config.TG_API_HASH,
        **policy,
    )


# --- Connection health --------------------------------------------------------
#
# The bridge has to survive the network going away, which on a home connection
# it does regularly. Telethon alone does not survive it: five retries a second
# apart, then the session is torn down permanently while the process carries on
# serving a health check that only ever said "Telegram is configured". Nothing
# exited, so nothing restarted, and the ring stayed dead until someone noticed.
#
# So: unbounded reconnection on exponential backoff, and a health view built on
# whether a round trip actually completed.

RECONNECT_BACKOFF_START = 1.0  # seconds before the first retry
RECONNECT_BACKOFF_CAP = 60.0  # ceiling; keep knocking once a minute, forever
RECONNECT_JITTER = 0.2  # +/- fraction, so restarts don't fall into lockstep
HEALTH_POLL_INTERVAL = 30.0  # seconds between round trips while healthy
HEALTH_STALE_AFTER = 90.0  # no verified round trip in this long is not "up"
CONNECT_TIMEOUT = 30.0
PING_TIMEOUT = 15.0
SUPERVISOR_RESTART_DELAY = 5.0  # after the supervisor itself dies of something unplanned


class ConnectionHealth:
    """What the bridge knows about its own link to Telegram.

    Deliberately not `client.is_connected()`. That reports the socket and stays
    True while Telethon retries underneath, so it reads healthy during exactly
    the window this class exists to describe. The load-bearing fact is the last
    time a round trip completed.
    """

    def __init__(self) -> None:
        self.last_ok: float | None = None  # monotonic, last verified round trip
        self.attempts = 0  # consecutive failures; 0 whenever the link is good
        self.next_retry_in: float | None = None
        self.last_error: str | None = None
        self.fatal: str | None = None  # auth-class failure; retrying cannot fix it
        # Connections Telethon tore down on its own that the supervisor then
        # rebuilt. Not failures — no probe of ours failed — but the number
        # that would have read "840" during the fifteen-hour churn of
        # 2026-09-01, when every other field on /healthz looked fine.
        self.rebuilds = 0

    def mark_ok(self) -> None:
        self.last_ok = time.monotonic()
        self.attempts = 0
        self.next_retry_in = None
        self.last_error = None

    def mark_failure(self, error: str, next_retry_in: float) -> None:
        self.attempts += 1
        self.last_error = error
        self.next_retry_in = next_retry_in

    def mark_fatal(self, error: str) -> None:
        self.fatal = error
        self.last_error = error
        self.next_retry_in = None

    @property
    def up(self) -> bool:
        """True only when a round trip completed recently and nothing has failed
        since. The staleness arm is the backstop: if the supervisor task dies,
        `last_ok` ages out and health goes red without a second watchdog."""
        if self.fatal or self.attempts or self.last_ok is None:
            return False
        return (time.monotonic() - self.last_ok) <= HEALTH_STALE_AFTER

    def snapshot(self) -> dict:
        if not config.TELEGRAM_ENABLED:
            state = "disabled"
        elif self.fatal:
            state = "fatal"
        elif self.up:
            state = "up"
        else:
            state = "down"
        age = None if self.last_ok is None else round(time.monotonic() - self.last_ok, 1)
        return {
            "state": state,
            "last_ok_age_s": age,
            "failed_attempts": self.attempts,
            "next_retry_s": (
                None if self.next_retry_in is None else round(self.next_retry_in, 1)
            ),
            "error": self.last_error,
            "rebuilds": self.rebuilds,
        }


tg_health = ConnectionHealth()
recheck_now: asyncio.Event | None = None  # created in lifespan


def request_recheck() -> None:
    """Wake the supervisor early. A send that just failed is evidence about the
    link, so health should turn red in a second rather than at the next poll."""
    if recheck_now is not None:
        recheck_now.set()


async def boot_connect(attempts: int = 5, delay: float = 1.0) -> None:
    """Connect at startup with the tolerance Telethon used to provide.

    The serving client has Telethon's retry loop switched off, so without this a
    single slow moment at boot would be a hard start failure where it used to be
    five one-second attempts. FloodWaitError is not an OSError and so still
    reaches lifespan's handler on the first try, un-slept.
    """
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.wait_for(tg_client.connect(), timeout=CONNECT_TIMEOUT)
            return
        except (OSError, asyncio.TimeoutError):
            if attempt == attempts:
                raise
            await asyncio.sleep(delay)


async def verify_connection(*, rebuild: bool = False) -> None:
    """One real round trip to Telegram, reconnecting first if the socket is gone.

    Raises on any failure; the caller decides what the failure means. Ping is the
    cheapest call that proves the far end is answering, which `is_connected()`
    does not.

    `rebuild` tears the connection down before reconnecting instead of trusting
    `is_connected()`, and the supervisor sets it after any failure. Two reasons,
    both load-bearing. A client can report connected while the far end is gone:
    `connect()` marks the sender connected before its layer-init call returns and
    creates the keepalive task only at the very end, so a timeout landing in that
    window leaves the flag stuck True with nothing left to notice it, and every
    later poll pings a corpse forever. And on the ordinary path, Telethon's own
    teardown leaves `_update_loop` running while `connect()` overwrites its
    handle, so without an explicit disconnect one update loop leaks per
    reconnect, each issuing its own calls against the account.

    A socket Telethon already tore down on its own is rebuilt the same way,
    counted in `rebuilds` rather than as a failure — never patched with a bare
    `connect()`, for the same leak reason.
    """
    from telethon.tl.functions import PingRequest

    if not rebuild and not tg_client.is_connected():
        # Telethon tore the link down by itself: with its retries off, its
        # _reconnect ends in _disconnect, and no probe of ours failed on the
        # way. Still a full rebuild. The old update and keepalive tasks are
        # cancelled only by disconnect(), and connect() would start a second
        # pair beside them — the "Fatal error handling updates" tracebacks of
        # 2026-09-02 were several update loops applying one difference to one
        # message box, one leaked pair per silent reconnect, 840 of them.
        tg_health.rebuilds += 1
        print("[tg] link torn down underneath the supervisor — rebuilding", flush=True)
        rebuild = True
    if rebuild:
        # This connection is being discarded either way, so failing to close it
        # politely is not interesting. (CancelledError is a BaseException and
        # still propagates.)
        with suppress(Exception):
            await asyncio.wait_for(tg_client.disconnect(), timeout=CONNECT_TIMEOUT)
        await asyncio.wait_for(tg_client.connect(), timeout=CONNECT_TIMEOUT)
        clear_stale_keepalive_ping()
    await asyncio.wait_for(
        tg_client(PingRequest(ping_id=random.getrandbits(63))), timeout=PING_TIMEOUT
    )


def clear_stale_keepalive_ping() -> None:
    """Forget the keepalive ping Telethon lost with the old connection.

    Telethon 1.44 tracks its own keepalive ping in `MTProtoSender._ping` and
    clears it only when the matching pong arrives (mtprotosender.py:743). When
    the link dies with that ping in flight, Telethon's own reconnect re-sends
    every pending request, so the pong comes and the field clears. Ours cannot:
    with `auto_reconnect=False` its teardown drops every pending request
    (`_disconnect` → `_pending_state.clear()`), and the connection the
    supervisor builds next inherits the stale id. From then on every keepalive
    tick (60s) reads the stale id as "the last ping never came back" and tears
    the fresh link down (`_keepalive_ping` → `_start_reconnect`). That ran once
    a minute for fifteen hours on 2026-09-01/02 — 840 teardowns — until one of
    them landed mid-probe and killed the supervisor. Private attribute, pinned
    library version, guarded so a stub client in tests needs no sender.
    """
    sender = getattr(tg_client, "_sender", None)
    if sender is not None and hasattr(sender, "_ping"):
        sender._ping = None


def backoff_after(delay: float) -> float:
    """The next nominal delay: double it, ceiling at the cap."""
    return min(delay * 2, RECONNECT_BACKOFF_CAP)


def jittered(delay: float) -> float:
    return delay * (1 + random.uniform(-RECONNECT_JITTER, RECONNECT_JITTER))


async def connection_supervisor() -> None:
    """Keep the Telegram link alive for as long as the process runs.

    Unbounded on purpose: an outage is a long wait, not a failure, so there is no
    attempt limit and no give-up. The single exit is an auth-class error, where
    retrying forever would bury a revoked session under a retry counter instead
    of saying the one thing that fixes it.
    """
    # Two families, not one. UnauthorizedError (401) covers revoked, terminated
    # and deactivated. AuthKeyError (406) is separate and holds
    # AuthKeyDuplicatedError, which Telegram raises when one session key is used
    # from two places at once — the exact thing the README warns about, and a key
    # it has already killed. Retrying either family is knocking on a dead door.
    from telethon.errors import AuthKeyError, UnauthorizedError
    from telethon.errors.common import AuthKeyNotFound

    delay = RECONNECT_BACKOFF_START
    while True:
        failure: Exception | None = None
        try:
            await verify_connection(rebuild=tg_health.attempts > 0)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # lifespan is shutting the process down
            # Not this task's cancellation. When Telethon tears a connection
            # down without an error (mtprotosender._disconnect, error=None) it
            # cancels the future of every in-flight request, and a cancelled
            # future raises CancelledError into whoever awaits it — here, the
            # health ping. The task itself was never cancelled, and
            # cancelling() is the only thing that tells the two apart. On
            # 2026-09-02 this read as shutdown, the loop re-raised, and the
            # ring was dead for a day with nothing left to reconnect it.
            failure = ConnectionError("Telegram tore the connection down mid-request")
        except (UnauthorizedError, AuthKeyError, AuthKeyNotFound) as e:
            tg_health.mark_fatal(f"{type(e).__name__}: {e}")
            print(
                f"[tg] Telegram rejected this session ({type(e).__name__}). That is "
                "not an outage, so it will not be retried. Fix: "
                "python ringbearer.py login",
                flush=True,
            )
            return
        except Exception as e:
            failure = e

        if failure is not None:
            wait = jittered(delay)
            tg_health.mark_failure(f"{type(failure).__name__}: {failure}", wait)
            print(
                f"[tg] link down ({type(failure).__name__}: {failure}) — attempt "
                f"{tg_health.attempts}, retrying in {wait:.0f}s",
                flush=True,
            )
            await asyncio.sleep(wait)
            delay = backoff_after(delay)
            continue

        if tg_health.attempts:
            print(
                f"[tg] link restored after {tg_health.attempts} failed attempt(s)",
                flush=True,
            )
        tg_health.mark_ok()
        delay = RECONNECT_BACKOFF_START
        if recheck_now is None:
            await asyncio.sleep(HEALTH_POLL_INTERVAL)
            continue
        recheck_now.clear()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(recheck_now.wait(), timeout=HEALTH_POLL_INTERVAL)


async def keep_supervising() -> None:
    """Run the supervisor for the life of the process, restarting it if it dies
    of anything it did not plan for.

    The supervisor ends on purpose in exactly two ways: cancelled by lifespan at
    shutdown, or returned after an auth-class failure. Anything else is a bug,
    and the right response to a bug in the one thing keeping the ring alive is
    to say so and start it again — not to leave /healthz red until a person
    notices, which is how 2026-09-02 went. The death is marked as a failure so
    the restarted loop rebuilds the connection instead of trusting it.
    """
    while True:
        try:
            await connection_supervisor()
            return
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            reason = "CancelledError from a cancelled future"
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
        tg_health.mark_failure(f"supervisor died ({reason})", SUPERVISOR_RESTART_DELAY)
        print(
            f"[tg] supervisor died ({reason}) — restarting in "
            f"{SUPERVISOR_RESTART_DELAY:.0f}s",
            flush=True,
        )
        await asyncio.sleep(SUPERVISOR_RESTART_DELAY)


def log_capture(row: dict) -> None:
    config.CAPTURES.touch(mode=0o600, exist_ok=True)
    with config.CAPTURES.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


class Dials(NamedTuple):
    """The two delivery settings, resolved for one route.

    They travel together because they are asked together on every send: does
    this capture open its own Telegram topic, and does the assistant receive
    it as conversation or as a one-shot instruction.
    """

    topic_per_capture: bool
    delivery_context: str


# Which config names hold each route's override. A route absent from this map
# (or a caller that names none) gets the globals, unchanged.
ROUTE_DIAL_KEYS = {
    "mcp": ("MCP_TOPIC_PER_CAPTURE", "MCP_DELIVERY_CONTEXT"),
    "webhook": ("WEBHOOK_TOPIC_PER_CAPTURE", "WEBHOOK_DELIVERY_CONTEXT"),
}


def global_dials() -> Dials:
    """The install-wide settings both routes default to."""
    return Dials(config.NEW_TOPIC_PER_CAPTURE, config.DELIVERY_CONTEXT)


def dials_for(source: str) -> Dials:
    """One route's settings: its own override wherever it set one, the global
    everywhere else.

    Parameters:
      source (str): the route name — "mcp", "webhook", or anything else,
        which resolves to the globals alone.

    Returns: the resolved Dials. Read through `config` on every call, never
      cached: tests rebind those names, and a route must never be able to
      observe the other route's override (A13).
    """
    topic_key, context_key = ROUTE_DIAL_KEYS.get(source, (None, None))
    topic = getattr(config, topic_key) if topic_key else None
    context = getattr(config, context_key) if context_key else None
    fallback = global_dials()
    return Dials(
        fallback.topic_per_capture if topic is None else topic,
        fallback.delivery_context if context is None else context,
    )


# The dials in force for the capture this task is carrying. `relay` sets them
# once, for the length of one capture, and the delivery path below reads them
# from here — so `deliver` keeps the call shape it has always had and no route
# has to thread an argument through. A ContextVar and not a module global
# because two captures can be in flight at once (one door does not wait for
# the other), and a task inherits the context it was created in.
_dials: ContextVar[Dials | None] = ContextVar("ringbearer_dials", default=None)


def current_dials() -> Dials:
    """The dials for the capture in flight, or the globals outside a relay
    (a direct `deliver` call, the setup banner, a test)."""
    return _dials.get() or global_dials()


def topic_title(message: str) -> str:
    normalized = " ".join(message.split())
    return normalized[:80] or "Ring capture"


def delivery_mode_label(dials: Dials | None = None) -> str:
    """How this route delivers, in words, for the phone cards and the startup
    banner. No argument means the globals."""
    if (dials or current_dials()).topic_per_capture:
        return "new topic per capture"
    return "current Telegram conversation"


def format_delivery_message(message: str, dials: Dials | None = None) -> str:
    """Add recipient-side context without changing the captured transcript."""
    if (dials or current_dials()).delivery_context == "conversation":
        return f"{config.RING_PREFIX}{message}"
    return (
        f"{config.RING_PREFIX}[RING CAPTURE: ONE-SHOT]\n"
        "The user is speaking through an Index 01 ring and may not see any reply. "
        "If this message requests an action, treat the spoken request as "
        "authorization and execute it now without asking for confirmation. "
        "Resolve minor ambiguity using reasonable, low-risk defaults and known "
        "context. If essential information is unavailable, or the action would "
        "be materially unsafe or irreversible, do not invent it; leave a concise "
        "blocker for the user to find later. Report the outcome briefly after "
        "attempting the action.\n\n"
        f"Transcript:\n{message}"
    )


async def create_topic(title: str, peer) -> int:
    """Create a fresh topic in `peer`'s chat and return its topic id.

    Telethon has no high-level helper for this, so it goes through the raw
    API. `messages.CreateForumTopicRequest` takes a generic peer (that is
    what makes topics in a bot DM possible at all) and returns a raw
    `Updates` — the new topic's id is the id of the topic-created service
    message inside it. No id found means the topic did not verifiably exist,
    and delivering anyway would land the capture in whatever thread Telegram
    picks — so that raises instead.
    """
    from telethon.tl.functions.messages import CreateForumTopicRequest
    from telethon.tl.types import MessageActionTopicCreate, UpdateMessageID

    request = CreateForumTopicRequest(peer=peer, title=title)
    result = await tg_client(request)
    # The result is an Updates union: usually a container with .updates, but
    # UpdateShort carries a single .update — normalize both shapes.
    updates = getattr(result, "updates", None)
    if updates is None and hasattr(result, "update"):
        updates = [result.update]
    # Two sources for the new topic's id: the UpdateMessageID whose random_id
    # echoes this request (authoritative — the same mapping Telethon's own
    # response parser uses), and the topic-created service message (present
    # in the usual shape). Prefer the correlated one.
    scanned = None
    for update in updates or []:
        if (
            isinstance(update, UpdateMessageID)
            and update.random_id == request.random_id
        ):
            return update.id
        msg = getattr(update, "message", None)
        if isinstance(getattr(msg, "action", None), MessageActionTopicCreate):
            scanned = msg.id
    if scanned is None:
        raise RuntimeError("Telegram topic creation returned no thread identifier")
    return scanned


async def deliver(message: str, assistant: str | None = None, *, dials: Dials | None = None) -> bool:
    """Post into the target assistant's DM as the user. Returns True if
    actually sent. `assistant` is a roster name; None means the default.
    `dials` overrides the ones in force — normally `relay` has already set
    the calling route's, and there is nothing to pass."""
    if not (config.TELEGRAM_ENABLED and tg_client is not None):
        return False
    dials = dials or current_dials()
    name = assistant or config.DEFAULT_ASSISTANT
    target = assistant_entities.get(name)
    if target is None:
        target = config.ASSISTANT_ROSTER[name]
    # Topic mode fails closed: if creation raises, nothing is sent — a
    # capture must never silently land in a thread the user didn't pick.
    # (The capture is still written to captures.jsonl by the caller.)
    reply_to = (
        await create_topic(topic_title(message), target)
        if dials.topic_per_capture
        else None
    )
    # parse_mode=None: the transcript is a promise ("verbatim and in
    # full"), so *, _, ` and [] must arrive as characters, not formatting.
    # reply_to targets the topic's root service message, which threads the
    # send into that topic; None is Telethon's default (no threading).
    await tg_client.send_message(
        target,
        format_delivery_message(message, dials),
        parse_mode=None,
        reply_to=reply_to,
    )
    tg_health.mark_ok()
    return True


class Reply(str):
    """What `relay` hands back: the sentence the MCP tool returns, with the
    capture row attached as `.row`.

    A str subclass because the tool's return value IS that sentence and has to
    stay a plain string on the wire. The webhook route answers the phone in
    JSON instead and builds that answer from the row — the same row that
    reached captures.jsonl, so what the phone is told and what the disk
    records can never disagree.
    """

    row: dict

    def __new__(cls, text: str, row: dict) -> "Reply":
        reply = super().__new__(cls, text)
        reply.row = row
        return reply


async def relay(
    message: str | None,
    assistant: str,
    *,
    source: str = "mcp",
    extra: dict | None = None,
    dials: Dials | None = None,
    reason: str | None = None,
) -> Reply:
    """Deliver one capture and log it, whichever door it arrived through.

    Both routes end here — the MCP tool body and the webhook route — because
    everything that differs between them is data, not code.

    Parameters:
      message (str | None): the transcript, verbatim. None only alongside
        `reason`: a capture with nothing to say still gets a row.
      assistant (str): a roster name. An off-roster name is refused loudly and
        still logged; the user's words are never silently re-routed.
      source (str): which door — "mcp" or "webhook". Names the row's `source`,
        picks the log line's shape, and selects the dials.
      extra (dict | None): row fields only one door has (the webhook's
        trigger, audio size, engine timings, test and duplicate flags, and its
        own `received_at`, which predates the transcription this row waited
        on). Merged onto the row last.
      dials (Dials | None): the delivery settings to use; None resolves them
        from `source`. Set for the length of the call, so `deliver` and
        `format_delivery_message` need no argument.
      reason (str | None): set when the caller has already decided not to
        deliver — a test event, a duplicate, a transcription that failed.
        The row is written with the reason and nothing is sent.

    Returns: a Reply — the sentence for the MCP tool, `.row` for the webhook.
    """
    extra = extra or {}
    dial_token = _dials.set(dials or dials_for(source))
    # The operator line's prefix. The MCP door names the tool it answered (and
    # the assistant, when it is not the default); the webhook door names the
    # gesture that fired it, which is the only routing fact it has.
    if source == "webhook":
        label = routed = f"[webhook] {extra.get('trigger') or 'no-trigger'}"
    else:
        label = f"[mcp] {config.TOOL_NAME}"
        routed = label + (f" -> {assistant}" if assistant != config.DEFAULT_ASSISTANT else "")

    def record(**outcome) -> dict:
        """Write this capture's row and return it. The row is the record: the
        reply the caller sends back is a view of it, never a second opinion."""
        row = {
            "received_at": datetime.now().astimezone().isoformat(),
            "source": source,
            "transcription": message,
            "assistant": assistant,
            **outcome,
            **extra,
        }
        log_capture(row)
        return row

    try:
        # Server-side validation regardless of the schema enum: the schema is
        # advisory to a cloud LLM. An off-roster name fails loud with the valid
        # list — the user's words are never silently re-routed to a chat they
        # didn't address — and the transcript is still logged.
        if assistant not in config.ASSISTANT_ROSTER:
            row = record(forwarded=False, error=f"unknown assistant {assistant!r}")
            print(f"{label}: unknown assistant {assistant!r}", flush=True)
            return Reply(
                f"Unknown assistant {assistant!r} — valid: "
                f"{', '.join(config.ASSISTANT_ROSTER)}. Not delivered; retry with one of "
                "those, or omit the argument for the default.",
                row,
            )

        # The route decided this one is not going anywhere (a test event, a
        # duplicate, audio the engine could not read). Still a fact worth
        # keeping, and still an answer worth giving.
        if reason is not None:
            row = record(forwarded=False, reason=reason)
            err = row.get("error")
            print(
                f"{routed}: not delivered ({reason})" + (f" ERROR {err}" if err else ""),
                flush=True,
            )
            return Reply(f"Received and logged. Not delivered: {reason}.", row)

        # Probe guard: a DRYRUN-prefixed message exercises the whole path (auth,
        # dispatch, routing, logging) without putting anything in the real
        # assistant DM. Live probes are real messages to a real assistant —
        # never send them casually.
        if message.startswith(config.DRY_RUN_PREFIX):
            row = record(forwarded=False, dry_run=True)
            print(f"{routed}: DRY RUN — not delivered", flush=True)
            return Reply("Dry run: received, not delivered to Telegram.", row)

        started = time.monotonic()

        def finish(sent: bool, err: str | None) -> dict:
            send_ms = round((time.monotonic() - started) * 1000)
            timings = "".join(
                f" {name} {ms}ms"
                for name, ms in (
                    ("transcribe", extra.get("transcribe_ms")),
                    ("telegram", send_ms),
                )
                if ms is not None
            )
            print(f"{routed}:{timings}" + (f" ERROR {err}" if err else ""), flush=True)
            outcome = {"forwarded": sent, "telegram_ms": send_ms}
            if err:
                outcome["error"] = err
            return record(**outcome)

        err = None
        try:
            # Bounded: a Telegram stall must not hold the transcript hostage —
            # timeout lands in the except and the capture row still gets written.
            sent = await asyncio.wait_for(deliver(message, assistant), timeout=30)
        except asyncio.CancelledError as e:
            if asyncio.current_task().cancelling():
                # Cancellation (request dropped, server shutting down) is
                # BaseException, so the clause below never sees it — log the only
                # copy of the transcript first, then propagate it bare.
                finish(False, repr(e))
                raise
            # A cancelled future, not a cancelled request: Telethon cancels the
            # send's future when it tears the connection down mid-send. To the ring
            # that is a failed delivery like any other — logged, reported, and the
            # supervisor nudged. Same class as connection_supervisor's handler.
            sent, err = False, "ConnectionError: Telegram tore the connection down mid-send"
            request_recheck()
        except Exception as e:  # the transcript is the only copy — log it no matter what
            sent, err = False, repr(e)
            # Evidence about the link, arriving between polls: let the supervisor
            # confirm or clear it now instead of at the next tick.
            request_recheck()
        row = finish(sent, err)
        display = config.ASSISTANT_NAME if assistant == config.DEFAULT_ASSISTANT else assistant
        if sent:
            return Reply(f"Delivered. {display} will reply in Telegram.", row)
        if err:
            return Reply(f"Logged locally, but Telegram delivery failed: {err}", row)
        return Reply("Received and logged. (Telegram delivery not yet enabled.)", row)
    finally:
        _dials.reset(dial_token)
