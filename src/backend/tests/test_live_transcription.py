"""Offline worker tests using actual SDK realtime connections and Task events."""

import asyncio
import base64
import copy
import inspect
import io
import json
import struct
import wave
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mistralai.client.models import AudioFormat, RealtimeTranscriptionSession
from mistralai.extra.realtime import RealtimeConnection
from mistralai.workflows.core.sandbox import get_sandbox_restrictions
from temporalio import workflow
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from backend.config import Settings
from backend.storage import FileStore, now_iso
from backend.workflow_gateway import TaskStateReducer
from workers.activities import transcription
from workers.models import LiveTranscriptionInput, SegmentInput
from workers.worker import workflow_classes
from workers.workflows import live_segment, live_transcription


def wav(value=1000, seconds=0.1):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(struct.pack("<h", value) * int(seconds * 16000))
    return output.getvalue()


class Socket:
    def __init__(self):
        self.events = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.end = asyncio.Event()
        self.send_error = None

    async def send(self, message):
        payload = json.loads(message)
        if self.send_error and payload["type"] == "input_audio.append":
            raise self.send_error
        self.sent.append(payload)
        if payload["type"] == "input_audio.end":
            self.end.set()

    async def close(self, **kwargs):
        self.closed = True

    async def __aiter__(self):
        while True:
            event = await self.events.get()
            if event is None:
                return
            if isinstance(event, Exception):
                raise event
            yield json.dumps(event)

    def updated(self):
        self.events.put_nowait(
            {
                "type": "session.updated",
                "session": {
                    "request_id": "offline",
                    "model": "offline",
                    "audio_format": {"encoding": "pcm_s16le", "sample_rate": 16000},
                    "target_streaming_delay_ms": 240,
                },
            }
        )

    def delta(self, text):
        self.events.put_nowait({"type": "transcription.text.delta", "text": text})

    def done(self, text):
        self.events.put_nowait(
            {
                "type": "transcription.done",
                "model": "offline",
                "text": text,
                "usage": {},
                "language": None,
            }
        )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    store = FileStore(tmp_path)
    directory = "audio/input/offline"
    store.write_bytes(f"{directory}/00000.wav", wav())
    store.write_json(
        f"{directory}/control.json",
        {"count": 1, "stopped": False, "updated_at": now_iso()},
    )
    socket = Socket()
    socket.updated()
    connection = RealtimeConnection(
        socket,
        RealtimeTranscriptionSession(
            request_id="offline",
            model="offline",
            audio_format=AudioFormat(encoding="pcm_s16le", sample_rate=16000),
        ),
    )
    connect = AsyncMock(return_value=connection)

    class Client:
        def __init__(self, **kwargs):
            self.audio = SimpleNamespace(realtime=SimpleNamespace(connect=connect))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(transcription, "Mistral", Client)
    monkeypatch.setattr(
        transcription,
        "Settings",
        lambda: Settings(data_dir=tmp_path, api_key="test-secret-key"),
    )
    heartbeat = []
    monkeypatch.setattr(
        transcription.workflows, "activity_heartbeat", lambda: heartbeat.append(True)
    )
    task_module = inspect.getmodule(transcription.Task)
    emitted = []
    monkeypatch.setattr(task_module, "should_publish_event", lambda: True)
    monkeypatch.setattr(
        task_module,
        "create_base_event_fields",
        lambda: {
            "event_id": "offline",
            "root_workflow_exec_id": "offline",
            "workflow_exec_id": "offline",
            "workflow_run_id": "offline",
            "workflow_name": "offline",
        },
    )
    monkeypatch.setattr(
        task_module,
        "_publish_task_event",
        lambda event: emitted.append(copy.deepcopy(event.model_dump(mode="json"))),
    )

    def control(count=1, stopped=False, updated_at=None):
        store.write_json(
            f"{directory}/control.json",
            {
                "count": count,
                "stopped": stopped,
                "updated_at": updated_at or now_iso(),
            },
        )

    def states():
        reducer = TaskStateReducer()
        return [state for event in emitted if (state := reducer.consume(event))]

    def diagnostic():
        return store.read_json(f"{directory}/workflow_error.json")

    async def run(max_seconds=10):
        return await transcription.transcribe_live_audio.__original_func__(
            LiveTranscriptionInput(
                input_dir=directory,
                transcription_model="offline",
                target_delay_ms=240,
                max_seconds=max_seconds,
            )
        )

    return SimpleNamespace(
        store=store,
        directory=directory,
        socket=socket,
        control=control,
        states=states,
        emitted=emitted,
        heartbeat=heartbeat,
        run=run,
        connect=connect,
        diagnostic=diagnostic,
    )


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


def test_ordered_audio_silence_stop_and_final_once(harness):
    async def run():
        harness.store.write_bytes(f"{harness.directory}/00001.wav", wav(0))
        harness.store.write_bytes(f"{harness.directory}/00002.wav", wav(2000))
        harness.control(count=3, stopped=True)
        task = asyncio.create_task(harness.run())
        await harness.socket.end.wait()
        harness.socket.delta(" Hello")
        harness.socket.delta(" Europe ")
        harness.socket.events.put_nowait(
            {
                "type": "transcription.segment",
                "text": "Hello Europe",
                "start": None,
                "end": None,
            }
        )
        harness.socket.done("Hello Europe")
        assert await task == "Hello Europe"

    asyncio.run(run())
    sent = harness.socket.sent
    assert [p["type"] for p in sent] == ["input_audio.append"] * 3 + [
        "input_audio.flush",
        "input_audio.end",
    ]
    assert b"".join(
        base64.b64decode(p["audio"]) for p in sent if "audio" in p
    ) == b"".join(struct.pack("<h", v) * 1600 for v in (1000, 0, 2000))
    finals = [s for s in harness.states() if s["final"]]
    assert finals == [
        {
            "type": "transcript",
            "text": "Hello Europe",
            "segment_index": 0,
            "final": True,
        }
    ]
    assert harness.socket.closed
    assert harness.heartbeat
    assert harness.connect.await_args.kwargs["target_streaming_delay_ms"] == 240


def test_no_pcm_or_end_before_session_updated(harness):
    acknowledgement = harness.socket.events.get_nowait()
    harness.control(stopped=True)

    async def run():
        task = asyncio.create_task(harness.run())
        await until(lambda: bool(harness.heartbeat))
        await asyncio.sleep(0.05)
        assert not harness.socket.sent
        assert not harness.socket.end.is_set()
        harness.socket.events.put_nowait(acknowledgement)
        await harness.socket.end.wait()
        harness.socket.done("")
        assert await task == ""

    asyncio.run(run())
    assert harness.socket.sent[0]["type"] == "input_audio.append"
    assert harness.socket.closed


def test_session_ack_timeout_is_supervised(harness, monkeypatch):
    harness.socket.events.get_nowait()
    monkeypatch.setattr(transcription, "SESSION_READY_SECONDS", 0.05)

    async def run():
        with pytest.raises(RuntimeError, match="waiting for realtime session.updated"):
            await asyncio.wait_for(harness.run(), 2)

    asyncio.run(run())
    assert not harness.socket.sent
    assert harness.socket.closed
    assert harness.heartbeat
    assert not harness.store.path(f"{harness.directory}/workflow_error.json").exists()


def test_stale_lease_interrupts_ack_wait(harness):
    harness.socket.events.get_nowait()

    async def run():
        task = asyncio.create_task(harness.run())
        await until(lambda: bool(harness.heartbeat))
        harness.control(
            updated_at=(datetime.now(UTC) - timedelta(seconds=16)).isoformat()
        )
        with pytest.raises(RuntimeError, match="heartbeat expired"):
            await asyncio.wait_for(task, 2)

    asyncio.run(run())
    assert not harness.socket.sent
    assert harness.socket.closed


@pytest.mark.parametrize(
    "message, readable, before_ack",
    [
        ("model start failed", "model start failed", True),
        (
            {"detail": "model start failed", "reason": "configuration"},
            "model start failed",
            False,
        ),
    ],
)
def test_voxtral_error_details_and_atomic_diagnostic(
    harness, message, readable, before_ack
):
    if before_ack:
        harness.socket.events.get_nowait()

    async def run():
        task = asyncio.create_task(harness.run())
        if not before_ack:
            await until(lambda: bool(harness.socket.sent))
            harness.socket.delta("Private transcript")
            await until(lambda: bool(harness.states()))
        harness.socket.events.put_nowait(
            {"type": "error", "error": {"code": 3804, "message": message}}
        )
        with pytest.raises(transcription.VoxtralStreamError) as caught:
            await asyncio.wait_for(task, 2)
        exc = caught.value
        assert exc.code == 3804
        assert exc.message == message
        assert exc.request_id == "offline"
        assert (
            str(exc)
            == f"Voxtral realtime transcription failed (code 3804, request_id offline): {readable}"
        )
        return exc

    exc = asyncio.run(run())
    diagnostic = harness.diagnostic()
    assert diagnostic.keys() == {
        "message",
        "code",
        "request_id",
        "timestamp",
        "error_message",
    }
    assert diagnostic["message"] == str(exc)
    assert diagnostic["error_message"] == message
    assert diagnostic["code"] == 3804
    assert diagnostic["request_id"] == "offline"
    assert datetime.fromisoformat(diagnostic["timestamp"]).tzinfo == UTC
    assert "Private transcript" not in json.dumps(diagnostic)
    assert not list(harness.store.path(harness.directory).glob("*.tmp"))
    assert harness.socket.closed
    assert not any(s["final"] for s in harness.states())
    if before_ack:
        assert not harness.socket.sent
    else:
        assert harness.emitted[-1]["event_type"] == "CUSTOM_TASK_FAILED"


@pytest.mark.parametrize(
    "message",
    [
        "Rejected test-secret-key and Bearer another-credential",
        {
            "detail": "Rejected test-secret-key",
            "nested": [
                {"headers": {"Authorization": "Bearer another-credential"}},
                {
                    "audio": "private-pcm",
                    "text": "private-transcript",
                    "api_key": "another-credential",
                },
            ],
        },
    ],
)
def test_voxtral_error_redaction_in_exception_and_diagnostic(harness, message):
    harness.socket.events.get_nowait()
    harness.socket.events.put_nowait(
        {"type": "error", "error": {"code": 400, "message": message}}
    )
    with pytest.raises(transcription.VoxtralStreamError) as caught:
        asyncio.run(harness.run())
    serialized = (
        json.dumps(harness.diagnostic())
        + str(caught.value)
        + json.dumps(caught.value.message)
    )
    for private in (
        "test-secret-key",
        "another-credential",
        "private-pcm",
        "private-transcript",
    ):
        assert private not in serialized
    assert "[redacted]" in serialized
    assert not harness.socket.sent


def test_diagnostic_write_failure_preserves_voxtral_error(harness, monkeypatch):
    harness.socket.events.get_nowait()
    harness.socket.events.put_nowait(
        {"type": "error", "error": {"code": 3804, "message": "model start failed"}}
    )

    def fail_write(*args):
        raise OSError("diagnostic storage unavailable")

    monkeypatch.setattr(FileStore, "write_json", fail_write)
    with pytest.raises(transcription.VoxtralStreamError, match="model start failed"):
        asyncio.run(harness.run())
    assert harness.socket.closed


@pytest.mark.parametrize("typed", [True, False])
def test_known_connect_error_diagnostic(harness, typed):
    from mistralai.client.models import RealtimeTranscriptionError
    from mistralai.extra.exceptions import RealtimeTranscriptionWSError

    payload = RealtimeTranscriptionError.model_validate(
        {
            "type": "error",
            "error": {"code": 400, "message": {"detail": "Rejected test-secret-key"}},
        }
    )
    harness.connect.side_effect = RealtimeTranscriptionWSError(
        "Unsafe Bearer private-credential",
        payload=payload if typed else None,
        raw={"headers": {"Authorization": "Bearer private-credential"}},
    )
    with pytest.raises(transcription.VoxtralStreamError) as caught:
        asyncio.run(harness.run())
    diagnostic = harness.diagnostic()
    assert diagnostic["request_id"] == ""
    assert diagnostic["code"] == (400 if typed else None)
    if typed:
        assert diagnostic["error_message"] == {"detail": "Rejected [redacted]"}
    else:
        assert "without a parsed error detail" in diagnostic["message"]
    assert "test-secret-key" not in json.dumps(diagnostic)
    assert "private-credential" not in json.dumps(diagnostic)
    assert caught.value.__suppress_context__
    assert not harness.socket.sent


@pytest.mark.parametrize("during_connect, close_code", [(True, None), (False, 1011)])
def test_websocket_close_diagnostic_omits_reason(harness, during_connect, close_code):
    from mistralai.extra.exceptions import RealtimeTranscriptionException
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    closed = ConnectionClosedError(
        Close(close_code, "test-secret-key private-transcript")
        if close_code is not None
        else None,
        None,
    )
    if during_connect:
        wrapped = RealtimeTranscriptionException("Unsafe test-secret-key")
        wrapped.__cause__ = closed
        harness.connect.side_effect = wrapped
    else:
        harness.socket.events.put_nowait(closed)
    with pytest.raises(transcription.VoxtralStreamError) as caught:
        asyncio.run(harness.run())
    diagnostic = harness.diagnostic()
    assert diagnostic["code"] == close_code
    assert diagnostic["request_id"] == ("" if during_connect else "offline")
    assert "closed unexpectedly" in diagnostic["message"]
    assert "test-secret-key" not in json.dumps(diagnostic)
    assert "private-transcript" not in json.dumps(diagnostic)
    assert caught.value.__suppress_context__
    if not during_connect:
        assert harness.socket.closed


def test_unknown_connect_exception_not_persisted(harness):
    harness.connect.side_effect = RuntimeError(
        "Authorization: Bearer private-credential"
    )
    with pytest.raises(RuntimeError):
        asyncio.run(harness.run())
    assert not harness.store.path(f"{harness.directory}/workflow_error.json").exists()


def test_partial_and_independent_timer_preserve_trailing_word(harness, monkeypatch):
    monkeypatch.setattr(transcription, "BATCH_SECONDS", 0.1)

    async def run():
        task = asyncio.create_task(harness.run())
        harness.socket.delta("Hello wor")
        await until(
            lambda: any(
                s["text"] == "Hello wor" and not s["final"] for s in harness.states()
            )
        )
        assert not harness.socket.end.is_set()
        await until(lambda: any(s["final"] for s in harness.states()))
        assert [s["text"] for s in harness.states() if s["final"]] == ["Hello"]
        harness.socket.delta("ld ")
        await asyncio.sleep(0.06)
        assert len([s for s in harness.states() if s["final"]]) == 1
        await until(lambda: len([s for s in harness.states() if s["final"]]) == 2)
        harness.socket.delta("again")
        harness.control(stopped=True)
        await harness.socket.end.wait()
        harness.socket.done("Hello world again")
        assert await task == "Hello world again"

    asyncio.run(run())
    finals = [s for s in harness.states() if s["final"]]
    assert [s["segment_index"] for s in finals] == [0, 1, 2]
    assert " ".join(s["text"] for s in finals) == "Hello world again"


def test_new_files_are_polled_then_drained_on_stop(harness):
    async def run():
        task = asyncio.create_task(harness.run())
        await until(lambda: bool(harness.socket.sent))
        harness.store.write_bytes(
            f"{harness.directory}/00001.wav", wav(0, seconds=1 / 16000)
        )
        harness.control(count=2, stopped=True)
        await harness.socket.end.wait()
        harness.socket.done("")
        assert await task == ""

    asyncio.run(run())
    assert base64.b64decode(harness.socket.sent[1]["audio"]) == b"\x00\x00"
    assert not harness.states()


def test_decreasing_audio_count_fails(harness):
    async def run():
        task = asyncio.create_task(harness.run())
        await until(lambda: bool(harness.socket.sent))
        harness.control(count=0)
        with pytest.raises(ValueError, match="decreased"):
            await task

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload, message",
    [
        (None, "without transcription.done"),
        ({"type": "new.event"}, "Unknown or malformed"),
        (
            {"type": "transcription.done", "text": "missing required fields"},
            "Unknown or malformed",
        ),
    ],
)
def test_eof_error_and_malformed_fail(harness, payload, message):
    async def run():
        task = asyncio.create_task(harness.run())
        harness.socket.events.put_nowait(payload)
        with pytest.raises((RuntimeError, TypeError), match=message):
            await asyncio.wait_for(task, 2)

    asyncio.run(run())
    assert harness.socket.closed
    assert not any(s["final"] for s in harness.states())


def test_upstream_error_wins_over_concurrent_sender_failure(harness):
    harness.socket.send_error = RuntimeError("Connection is closed")
    harness.socket.events.put_nowait(
        {"type": "error", "error": {"code": 3804, "message": "upstream failure"}}
    )
    harness.socket.events.put_nowait(None)
    with pytest.raises(transcription.VoxtralStreamError, match="upstream failure"):
        asyncio.run(harness.run())
    assert harness.diagnostic()["code"] == 3804
    assert harness.socket.closed


def test_mismatched_done_reports_failure_without_final(harness):
    async def run():
        harness.control(stopped=True)
        task = asyncio.create_task(harness.run())
        await harness.socket.end.wait()
        harness.socket.delta("Hello")
        harness.socket.done("Changed")
        with pytest.raises(RuntimeError, match="differs from streamed deltas"):
            await task

    asyncio.run(run())
    assert not any(s["final"] for s in harness.states())
    assert harness.emitted[-1]["event_type"] == "CUSTOM_TASK_FAILED"


def test_premature_done_fails(harness):
    async def run():
        task = asyncio.create_task(harness.run())
        harness.socket.done("")
        with pytest.raises(RuntimeError, match="before audio end"):
            await task

    asyncio.run(run())


def test_stale_lease_rejected_before_connect(harness):
    harness.control(updated_at=(datetime.now(UTC) - timedelta(seconds=16)).isoformat())
    with pytest.raises(RuntimeError, match="heartbeat expired"):
        asyncio.run(harness.run())
    harness.connect.assert_not_awaited()


def test_watchdog_detects_stale_lease_while_waiting_for_model(harness):
    async def run():
        task = asyncio.create_task(harness.run())
        await until(lambda: bool(harness.socket.sent))
        harness.control(
            updated_at=(datetime.now(UTC) - timedelta(seconds=16)).isoformat()
        )
        with pytest.raises(RuntimeError, match="heartbeat expired"):
            await asyncio.wait_for(task, 2)

    asyncio.run(run())
    assert harness.socket.closed


@pytest.mark.parametrize("mode", ["missing", "oversize", "send"])
def test_sender_failure_interrupts_waiting_receiver(harness, mode):
    if mode == "missing":
        harness.control(count=2, stopped=True)
    elif mode == "oversize":
        harness.store.write_bytes(f"{harness.directory}/00000.wav", wav(seconds=3.01))
    else:
        harness.socket.send_error = OSError("offline send failed")

    async def run():
        with pytest.raises((FileNotFoundError, ValueError, OSError)):
            await asyncio.wait_for(harness.run(), 2)

    asyncio.run(run())
    assert harness.socket.closed


def test_end_watchdog_and_overall_timeout(harness, monkeypatch):
    monkeypatch.setattr(transcription, "LEASE_SECONDS", 0.1)
    harness.control(stopped=True)

    async def run():
        with pytest.raises(RuntimeError, match="Timed out waiting"):
            await asyncio.wait_for(harness.run(), 2)

    asyncio.run(run())


def test_explicit_cancellation_closes_connection(harness):
    async def run():
        task = asyncio.create_task(harness.run())
        harness.socket.delta("Pending")
        await until(lambda: bool(harness.states()))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert harness.socket.closed
        assert not any(s["final"] for s in harness.states())
        assert not harness.store.path(
            f"{harness.directory}/workflow_error.json"
        ).exists()

    asyncio.run(run())


def test_segment_supplied_text_fans_out_translations(monkeypatch):
    translate = AsyncMock(
        side_effect=lambda text, language, *args: {
            "language": language,
            "text": f"{language}: {text}",
        }
    )
    monkeypatch.setattr(live_segment, "translate_text", translate)
    params = SegmentInput(
        text="Ready text",
        segment_index=2,
        source_language="en",
        previous_text="Earlier context",
        translation_model="offline",
    )
    result = asyncio.run(live_segment.LiveSegmentWorkflow().run(params))["result"]
    assert result == {
        "text": params.text,
        "translations": {
            language: f"{language}: {params.text}"
            for language in ("de", "fr", "es", "it")
        },
    }
    assert translate.await_count == 4
    for language in ("de", "fr", "es", "it"):
        translate.assert_any_await(
            params.text,
            language,
            params.segment_index,
            params.previous_text,
            params.translation_model,
            params.source_language,
        )


def test_segment_empty_text_skips_translations(monkeypatch):
    translate = AsyncMock()
    monkeypatch.setattr(live_segment, "translate_text", translate)
    params = SegmentInput(text="", segment_index=0, translation_model="offline")
    assert asyncio.run(live_segment.LiveSegmentWorkflow().run(params)) == {
        "result": {"text": "", "translations": {}}
    }
    translate.assert_not_awaited()


def test_live_workflow_and_five_registered_sandbox_classes(monkeypatch):
    activity = AsyncMock(return_value="Full text")
    monkeypatch.setattr(live_transcription, "transcribe_live_audio", activity)
    params = LiveTranscriptionInput(
        input_dir="audio/input/offline", transcription_model="offline"
    )
    assert asyncio.run(live_transcription.LiveTranscriptionWorkflow().run(params)) == {
        "result": {"text": "Full text"}
    }
    activity.assert_awaited_once_with(params)

    async def validate():
        classes = workflow_classes()
        assert len(classes) == 5
        runner = SandboxedWorkflowRunner(restrictions=get_sandbox_restrictions())
        definitions = [workflow._Definition.must_from_class(cls) for cls in classes]
        for definition in definitions:
            runner.prepare_workflow(definition)
        assert "voxbridge-live-transcription" in {d.name for d in definitions}

    asyncio.run(validate())
