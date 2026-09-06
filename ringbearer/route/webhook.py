"""The fast door: the phone posts the capture straight here.

No cloud agent, no tool call, no LLM between the ring and the assistant — the
Pebble app's "Webhook only" gesture POSTs its multipart to this route, the
bridge makes the text (or takes the phone's, if the payload carries one) and
hands it to `telegram.relay`, the same function the MCP door calls. Everything
this route knows and that one does not — the gesture, the audio size, the
engine timings, whether the capture was a test or a duplicate — travels as row
fields, not as a second copy of the delivery code.

Two things this door deliberately does NOT do. It never reads the words to
choose a destination: every capture goes to `config.WEBHOOK_ASSISTANT` (A10).
And it never redirects — the app follows 301/302 on a POST and re-sends the
whole body, so `/webhook` and `/webhook/` are both registered and both answer.

The wire contract it accepts is the app's own, verified from its source: parts
`audio` (m4a, filename `<recordingId>.m4a`), `transcription`, `test`,
`recordedAt` and `client`, plus `X-Index-Trigger` and `X-Index-Test` headers.
`recordedAt` and `client` are accepted and ignored — the row's clock is this
machine's, and there is only one kind of client.
"""

import asyncio
from collections import deque
from datetime import datetime

from fastapi import APIRouter, File, Form, Header, UploadFile

from .. import config, telegram, transcribe

router = APIRouter()

WEBHOOK_PATH = f"{config.MCP_MOUNT}/webhook"

# Recently accepted audio filenames — the phone's `<recordingId>.m4a`, which
# without request signing is the only stable id it offers. Bounded and in
# memory on purpose: the sole duplicate the app can produce is a queued retry
# across an app restart mid-flight, and a file on disk would insure against
# something rarer than the bug it adds. A payload with no audio part therefore
# has no stable id at all and is never deduped.
SEEN_LIMIT = 256
_seen: deque = deque(maxlen=SEEN_LIMIT)

NOTHING_TO_SAY = "no transcription and no audio in payload"


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


@router.post(WEBHOOK_PATH)
@router.post(f"{WEBHOOK_PATH}/")
async def webhook(
    audio: UploadFile | None = File(default=None),
    transcription: str | None = Form(default=None),
    test: str | None = Form(default=None),
    trigger: str | None = Header(default=None, alias="X-Index-Trigger"),
    test_header: str | None = Header(default=None, alias="X-Index-Test"),
):
    """Take one ring capture off the phone and deliver it.

    Returns: a small JSON body describing what happened, built from the capture
    row `relay` wrote. Any 2xx satisfies the app, which never reads a
    successful body — the body is for curl and for a human reading `Recent
    runs` after a non-2xx. Every outcome short of an unhandled crash is a 200:
    a payload with nothing in it is a fact to log, not a server error, and
    there is no retry on the phone for a 5xx to earn.
    """
    data = await audio.read() if audio is not None else b""
    filename = audio.filename if audio is not None else None
    # A `transcription` part wins outright — the phone already did the work, so
    # the engine never runs (C63). Blank counts as absent: an empty part with
    # audio beside it means the phone's transcriber came back with nothing, and
    # relaying silence when the audio is right there would be the wrong call.
    text = transcription if (transcription or "").strip() else None
    is_test = _is_true(test) or _is_true(test_header)
    duplicate = bool(filename) and filename in _seen

    # This route's own row fields. `received_at` is the moment the capture
    # arrived, which is not the moment the row is written: transcription
    # happens in between, and the clock the ring cares about is this one.
    extra = {
        "received_at": datetime.now().astimezone().isoformat(),
        "trigger": trigger,
        "test": is_test,
        "duplicate": duplicate,
    }
    if audio is not None:
        # A11: how much audio arrived, never the audio itself.
        extra["audio_bytes"] = len(data)

    transcribe_ms = None
    reason = None
    if is_test:
        # A12: the app's Send test event button proves the endpoint, the token
        # and the headers. It must never reach a real assistant.
        reason = "test event"
    elif duplicate:
        reason = "duplicate capture"
    elif text is None and not data:
        reason = NOTHING_TO_SAY
    else:
        if filename:
            _seen.append(filename)
        if text is None:
            try:
                text, transcribe_ms = await transcribe.run(data, filename)
            except asyncio.CancelledError:
                # The request went away (or the server is going down) while the
                # engine was decoding. There is no transcript yet to lose, but
                # the capture did happen — log that much on the way out.
                # relay's reason branch awaits nothing, so it completes even
                # inside a cancelled task.
                await telegram.relay(
                    text,
                    config.WEBHOOK_ASSISTANT,
                    source="webhook",
                    extra=extra,
                    reason="cancelled during transcription",
                )
                raise
            except Exception as e:
                extra["error"] = repr(e)
                reason = "transcription failed"
        if reason is None and not (text or "").strip():
            reason = "transcription produced no text"
    if transcribe_ms is not None:
        extra["transcribe_ms"] = transcribe_ms
        extra["engine"] = transcribe.ENGINE
        extra["model"] = config.TRANSCRIBE_MODEL

    # One relay, both doors (design item 1). The fixed target is configuration
    # and nothing else: this route never reads the words to choose it (A10).
    row = (
        await telegram.relay(
            text,
            config.WEBHOOK_ASSISTANT,
            source="webhook",
            extra=extra,
            reason=reason,
        )
    ).row

    body = {
        "ok": True,
        "forwarded": row["forwarded"],
        "assistant": config.WEBHOOK_ASSISTANT,
        "test": is_test,
        "duplicate": duplicate,
    }
    if transcribe_ms is not None:
        body["transcribe_ms"] = transcribe_ms
    if row.get("dry_run"):
        body["dry_run"] = True
    if reason:
        body["reason"] = reason
    if row.get("error"):
        body["error"] = row["error"]
    return body
