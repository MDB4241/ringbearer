"""Native webhook voice delivery through the existing guarded Telegram client."""
import asyncio
import io
import logging
import time
import tempfile
import shutil
from pathlib import Path
from fastapi.responses import JSONResponse
from . import config, telegram

MAX_AUDIO_BYTES = 16 * 1024 * 1024


async def encode_opus(data: bytes) -> bytes:
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise ValueError('Audio must be nonempty and at most 16 MiB')
    # M4A can require seeking to a trailing moov atom. A stdin pipe can
    # silently produce an empty Ogg even when ffmpeg exits successfully.
    folder = config.STATE_DIR / 'tmp'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    with tempfile.NamedTemporaryFile(dir=folder, suffix='.audio', delete=False) as source:
        source.write(data)
        source_path = Path(source.name)
    proc = None
    try:
        ffmpeg = shutil.which('ffmpeg')
        if ffmpeg is None:
            raise RuntimeError('ffmpeg executable not found on PATH')
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-i', str(source_path), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '48000',
            '-c:a', 'libopus', '-b:a', '32k', '-f', 'ogg', 'pipe:1',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, error = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode or not output.startswith(b'OggS') or b'OpusHead' not in output:
            raise ValueError('Audio conversion failed: ' + error.decode(errors='replace')[-300:])
        return output
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.communicate()
        source_path.unlink(missing_ok=True)


def caption() -> str:
    if telegram.dials_for('webhook').delivery_context == 'one_shot':
        return (
            '[RING CAPTURE: ONE-SHOT]\n'
            'The attached voice note is a ring capture. The user may not see follow-up questions. '
            'Act on clear requests using low-risk defaults; leave a concise blocker if essential '
            'information is missing or the action is unsafe. Report the result briefly. '
            'Do not repeat an action because speech synthesis fails.'
        )
    return '[RING CAPTURE]\nThe attached voice note is from the user\'s ring.'


async def relay_voice(data: bytes, extra: dict):
    row = dict(extra, source='webhook', assistant=config.WEBHOOK_ASSISTANT,
               delivery_format='voice', transcription=None, forwarded=False)
    status = 200
    attempted = False
    started = time.monotonic()
    try:
        # Never upload or even convert when delivery is disabled.
        if not config.TELEGRAM_ENABLED:
            row['reason'] = 'Telegram delivery disabled'
        elif telegram.dials_for('webhook').topic_per_capture:
            raise ValueError('Native voice mode currently requires same-conversation delivery')
        else:
            encoded = await encode_opus(data)
            row['conversion_ms'] = round((time.monotonic() - started) * 1000)
            async with telegram._client_lock():
                if telegram.tg_client is None or telegram.tg_health.fatal:
                    raise ConnectionError('Telegram client unavailable')
                target = telegram.assistant_entities.get(config.WEBHOOK_ASSISTANT)
                if target is None:
                    raise ConnectionError('Webhook assistant has not been resolved')
                audio = io.BytesIO(encoded)
                audio.name = 'ring-capture.ogg'
                attempted = True
                sent = await asyncio.wait_for(telegram.tg_client.send_file(
                    target, audio, voice_note=True, mime_type='audio/ogg',
                    caption=caption(), parse_mode=None), timeout=30)
                row.update(forwarded=True, telegram_message_id=sent.id)
    except asyncio.CancelledError:
        row.update(reason='native voice request cancelled', outcome_unknown=attempted)
        raise
    except Exception as exc:
        row.update(error=repr(exc), reason='native voice delivery failed', outcome_unknown=attempted)
        status = 200 if attempted else 503
        if attempted:
            telegram.request_recheck()
    finally:
        row['total_ms'] = round((time.monotonic() - started) * 1000)
        try:
            telegram.log_capture(row)
        except Exception:
            # A logging failure after upload must not invite another delivery.
            logging.getLogger(__name__).exception('Could not log native voice capture')
    body = {k: v for k, v in row.items() if k not in {'transcription'}}
    body['ok'] = 'error' not in row
    if row.get('outcome_unknown'):
        body['retry_safe'] = False
    return JSONResponse(body, status_code=status)
