"""Voxtral realtime transcription inside a workflow activity."""

import asyncio
import json
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mistralai import workflows
from mistralai.client import Mistral
from mistralai.client.models import (
    AudioFormat,
    RealtimeTranscriptionError,
    RealtimeTranscriptionSessionCreated,
    RealtimeTranscriptionSessionUpdated,
    TranscriptionStreamDone,
    TranscriptionStreamLanguage,
    TranscriptionStreamSegmentDelta,
    TranscriptionStreamTextDelta,
)
from mistralai.extra.exceptions import (
    RealtimeTranscriptionException,
    RealtimeTranscriptionWSError,
)
from mistralai.extra.realtime import RealtimeConnection, UnknownRealtimeEvent
from mistralai.workflows.core.task import Task
from websockets.exceptions import ConnectionClosedError

from backend.config import Settings
from backend.storage import FileStore, read_wav
from workers.models import LiveTranscriptionInput

POLL_SECONDS = 0.05
LEASE_SECONDS = 15
BATCH_SECONDS = 3
SESSION_READY_SECONDS = 10


def _sanitize_error(value, api_key: str):
    if isinstance(value, str):
        if api_key:
            value = value.replace(api_key, "[redacted]")
        return re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", value)
    if isinstance(value, dict):
        sensitive = {
            "audio",
            "pcm",
            "text",
            "transcript",
            "transcription",
            "input",
            "headers",
            "authorization",
            "api_key",
            "apikey",
            "token",
            "access_token",
            "password",
            "secret",
            "credentials",
        }
        return {
            _sanitize_error(key, api_key): (
                "[redacted]"
                if key.lower().replace("-", "_") in sensitive
                else _sanitize_error(item, api_key)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_error(item, api_key) for item in value]
    return value


class VoxtralStreamError(RuntimeError):
    """A parsed server error with sanitized details, never the raw event."""

    def __init__(
        self,
        code: int | None,
        message: str | dict,
        request_id: str,
        *,
        api_key: str = "",
    ):
        self.code = code
        self.message = _sanitize_error(message, api_key)
        self.request_id = _sanitize_error(request_id, api_key)
        detail = (
            self.message.get("detail", self.message)
            if isinstance(self.message, dict)
            else self.message
        )
        readable = (
            detail
            if isinstance(detail, str)
            else json.dumps(detail, ensure_ascii=False)
        )
        super().__init__(
            f"Voxtral realtime transcription failed (code {code}, request_id {self.request_id}): {readable}"
        )


def _read_control(directory: Path) -> dict:
    control = json.loads((directory / "control.json").read_text())
    if (
        not isinstance(control, dict)
        or type(control.get("count")) is not int
        or control["count"] < 0
        or type(control.get("stopped")) is not bool
    ):
        raise ValueError("Invalid live transcription control file.")
    updated = datetime.fromisoformat(control["updated_at"])
    if updated.tzinfo is None:
        raise ValueError("Live transcription heartbeat must include a timezone.")
    age = (datetime.now(UTC) - updated).total_seconds()
    if age > LEASE_SECONDS or age < -LEASE_SECONDS:
        raise RuntimeError("Live transcription producer heartbeat expired.")
    return control


class _TranscriptBatches:
    """Commit whitespace-delimited text without losing a trailing partial word."""

    def __init__(self):
        self.full_text = ""
        self.pending = ""
        self.index = 0
        self.first_text_at: float | None = None
        self.task: Task | None = None
        self.lock = asyncio.Lock()

    async def _partial(self):
        if not self.pending.strip():
            return
        state = {"text": self.pending, "segment_index": self.index, "final": False}
        if self.task is None:
            self.task = Task(type="transcript", state=state)
            await self.task.__aenter__()
        else:
            await self.task.update_state(state)

    async def _commit(self, end: int):
        text = self.pending[:end].strip()
        if not text:
            return
        await self._partial()
        task, self.task = self.task, None
        assert task is not None and isinstance(task.state, dict)
        # Completed carries the only final snapshot; an update would emit it twice.
        task.state.update(text=text, final=True)
        await task.__aexit__(None, None, None)
        self.pending = self.pending[end:]
        self.index += 1
        self.first_text_at = (
            asyncio.get_running_loop().time() if self.pending.strip() else None
        )
        await self._partial()

    async def _commit_due(self):
        if (
            self.first_text_at is not None
            and asyncio.get_running_loop().time() - self.first_text_at >= BATCH_SECONDS
        ):
            boundary = max(
                (i + 1 for i, char in enumerate(self.pending) if char.isspace()),
                default=0,
            )
            await self._commit(boundary)

    async def append(self, text: str):
        async with self.lock:
            self.full_text += text
            self.pending += text
            if self.first_text_at is None and self.pending.strip():
                self.first_text_at = asyncio.get_running_loop().time()
            await self._partial()
            await self._commit_due()

    async def tick(self):
        async with self.lock:
            await self._commit_due()

    async def finish(self, text: str):
        async with self.lock:
            if text.strip() != self.full_text.strip():
                raise RuntimeError(
                    "Realtime final text differs from streamed deltas; translations may need correction."
                )
            await self._commit(len(self.pending))

    async def fail(self, error: BaseException):
        if self.task is not None:
            task, self.task = self.task, None
            await task.__aexit__(type(error), error, error.__traceback__)


async def _send_live_audio(
    connection: RealtimeConnection,
    directory: Path,
    input_ended: asyncio.Event,
    session_ready: asyncio.Event,
):
    try:
        await asyncio.wait_for(session_ready.wait(), timeout=SESSION_READY_SECONDS)
    except TimeoutError as exc:
        raise RuntimeError("Timed out waiting for realtime session.updated.") from exc
    index = 0
    count = 0
    stopped_count = None
    while True:
        control = _read_control(directory)
        if control["count"] < count or (
            stopped_count is not None
            and (not control["stopped"] or control["count"] != stopped_count)
        ):
            raise ValueError(
                "Live transcription audio count changed after stop or decreased."
            )
        count = control["count"]
        if control["stopped"]:
            stopped_count = count
        if index < count:
            pcm, _ = read_wav(
                (directory / f"{index:05d}.wav").read_bytes(),
                max_seconds=3,
                min_seconds=1 / 16000,
            )
            # Preserve silence and send PCM only, never individual WAV headers.
            for offset in range(0, len(pcm), 3200):
                await connection.send_audio(pcm[offset : offset + 3200])
            index += 1
        elif control["stopped"]:
            await connection.flush_audio()
            input_ended.set()
            await connection.end_audio()
            return
        else:
            await asyncio.sleep(POLL_SECONDS)


async def _receive_live_text(
    connection: RealtimeConnection, events: asyncio.Queue, api_key: str
):
    async for event in connection:
        if isinstance(event, RealtimeTranscriptionError):
            raise VoxtralStreamError(
                event.error.code,
                event.error.message,
                connection.request_id,
                api_key=api_key,
            )
        await events.put(("event", event))
        if isinstance(event, TranscriptionStreamDone):
            return
    raise RuntimeError("Realtime stream ended without transcription.done.")


async def _consume_live_text(
    events: asyncio.Queue,
    batches: _TranscriptBatches,
    input_ended: asyncio.Event,
    session_ready: asyncio.Event,
):
    try:
        while True:
            kind, event = await events.get()
            if kind == "tick":
                await batches.tick()
                continue
            if isinstance(event, UnknownRealtimeEvent):
                raise TypeError("Unknown or malformed realtime transcription event.")
            if isinstance(event, RealtimeTranscriptionSessionUpdated):
                session_ready.set()
            elif isinstance(event, TranscriptionStreamTextDelta):
                await batches.append(event.text)
            elif isinstance(event, TranscriptionStreamDone):
                if not input_ended.is_set():
                    raise RuntimeError(
                        "Realtime transcription completed before audio end."
                    )
                await batches.finish(event.text)
                return
            elif not isinstance(
                event,
                (
                    RealtimeTranscriptionSessionCreated,
                    RealtimeTranscriptionSessionUpdated,
                    TranscriptionStreamLanguage,
                    TranscriptionStreamSegmentDelta,
                ),
            ):
                raise TypeError("Unsupported realtime transcription event.")
    except BaseException as exc:
        await batches.fail(exc)
        raise


async def _watch_live_audio(
    directory: Path, events: asyncio.Queue, input_ended: asyncio.Event
):
    last_heartbeat = 0.0
    ended_at = None
    while True:
        now = asyncio.get_running_loop().time()
        if input_ended.is_set():
            if ended_at is None:
                ended_at = now
            elif now - ended_at > LEASE_SECONDS:
                raise RuntimeError(
                    "Timed out waiting for transcription.done after audio end."
                )
        else:
            _read_control(directory)
        if now - last_heartbeat >= 1:
            workflows.activity_heartbeat()
            last_heartbeat = now
        await events.put(("tick", None))
        await asyncio.sleep(POLL_SECONDS)


@workflows.activity(
    start_to_close_timeout=timedelta(seconds=3600),
    heartbeat_timeout=timedelta(seconds=10),
    retry_policy_max_attempts=1,
)
async def transcribe_live_audio(params: LiveTranscriptionInput) -> str:
    settings = Settings()
    store = FileStore(settings.data_dir)
    directory = store.path(params.input_dir)
    batches = _TranscriptBatches()
    input_ended = asyncio.Event()
    session_ready = asyncio.Event()
    events = asyncio.Queue(maxsize=128)
    tasks = []
    connection = None
    try:
        async with asyncio.timeout(params.max_seconds):
            _read_control(directory)
            async with (
                Mistral(
                    api_key=settings.api_key, server_url=settings.server_url
                ) as client,
                await client.audio.realtime.connect(
                    model=params.transcription_model,
                    audio_format=AudioFormat(encoding="pcm_s16le", sample_rate=16000),
                    target_streaming_delay_ms=params.target_delay_ms,
                    timeout_ms=10_000,
                ) as connection,
            ):
                tasks = [
                    asyncio.create_task(
                        _send_live_audio(
                            connection, directory, input_ended, session_ready
                        )
                    ),
                    asyncio.create_task(
                        _consume_live_text(
                            events,
                            batches,
                            input_ended,
                            session_ready,
                        )
                    ),
                    asyncio.create_task(
                        _receive_live_text(connection, events, settings.api_key)
                    ),
                    asyncio.create_task(
                        _watch_live_audio(directory, events, input_ended)
                    ),
                ]
                try:
                    # Sender and watchdog errors interrupt reception immediately.
                    pending = set(tasks)
                    while tasks[1] in pending:
                        completed, pending = await asyncio.wait(
                            pending, return_when=asyncio.FIRST_COMPLETED
                        )
                        # Prefer the upstream failure over a sender's closed socket.
                        for task in (tasks[1], tasks[2], tasks[0], tasks[3]):
                            if task in completed:
                                task.result()
                    return batches.full_text.strip()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        diagnostic_error = exc if isinstance(exc, VoxtralStreamError) else None
        request_id = connection.request_id if connection is not None else ""
        if isinstance(exc, RealtimeTranscriptionWSError):
            payload = exc.payload_typed
            diagnostic_error = VoxtralStreamError(
                payload.error.code if payload is not None else exc.code,
                payload.error.message
                if payload is not None
                else "Realtime WebSocket handshake rejected without a parsed error detail.",
                request_id,
                api_key=settings.api_key,
            )
        closed = (
            exc.__cause__ if isinstance(exc, RealtimeTranscriptionException) else exc
        )
        if isinstance(closed, ConnectionClosedError):
            diagnostic_error = VoxtralStreamError(
                closed.rcvd.code if closed.rcvd is not None else None,
                "Realtime WebSocket closed unexpectedly; close reason omitted for privacy.",
                request_id,
                api_key=settings.api_key,
            )
        if diagnostic_error is not None:
            # A diagnostic write failure must not replace the server failure.
            with suppress(OSError):
                store.write_json(
                    f"{params.input_dir}/workflow_error.json",
                    {
                        "message": str(diagnostic_error),
                        "code": diagnostic_error.code,
                        "request_id": diagnostic_error.request_id,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "error_message": diagnostic_error.message,
                    },
                )
            await batches.fail(diagnostic_error)
            if diagnostic_error is not exc:
                raise diagnostic_error from None
        else:
            await batches.fail(exc)
        raise
    except BaseException as exc:
        await batches.fail(exc)
        raise
