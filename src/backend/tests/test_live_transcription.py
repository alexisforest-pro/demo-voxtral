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
from workers.activities import transcription, translation
from workers.models import LiveTranscriptionInput, LiveTranslationInput
from workers.worker import workflow_classes
from workers.workflows import live_transcription, live_translation


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
                    "target_streaming_delay_ms": 200,
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
                target_delay_ms=200,
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
            "sentence_end": True,
        }
    ]
    assert harness.socket.closed
    assert harness.heartbeat
    assert harness.connect.await_args.kwargs["target_streaming_delay_ms"] == 200


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


def test_sentence_end_commits_before_batch_timer(harness, monkeypatch):
    monkeypatch.setattr(transcription, "BATCH_SECONDS", 60)
    monkeypatch.setattr(transcription, "SENTENCE_SETTLE_SECONDS", 0.05)

    def finals():
        return [s["text"] for s in harness.states() if s["final"]]

    async def run():
        task = asyncio.create_task(harness.run())
        harness.socket.delta("Mr. Smith met Dr. Jones")
        harness.socket.delta(" at 3.5 percent. Our people")
        await until(lambda: len(finals()) == 1)
        assert finals() == ["Mr. Smith met Dr. Jones at 3.5 percent."]
        harness.socket.delta(" share ties!")
        await until(lambda: len(finals()) == 2)
        assert finals()[1] == "Our people share ties!"
        harness.socket.delta(" And the U.S.")
        await asyncio.sleep(0.2)
        assert len(finals()) == 2
        harness.control(stopped=True)
        await harness.socket.end.wait()
        text = "Mr. Smith met Dr. Jones at 3.5 percent. Our people share ties! And the U.S."
        harness.socket.done(text)
        assert await task == text

    asyncio.run(run())
    assert finals()[2] == "And the U.S."
    assert all(s["sentence_end"] for s in harness.states() if s["final"])


def test_fallback_cut_prefers_clause_and_is_marked_partial(harness, monkeypatch):
    monkeypatch.setattr(transcription, "BATCH_SECONDS", 0.1)

    async def run():
        task = asyncio.create_task(harness.run())
        harness.socket.delta("in other words, we will see")
        await until(lambda: any(s["final"] for s in harness.states()))
        harness.control(stopped=True)
        await harness.socket.end.wait()
        harness.socket.done("in other words, we will see")
        await task

    asyncio.run(run())
    finals = [s for s in harness.states() if s["final"]]
    assert [(s["text"], s["sentence_end"]) for s in finals] == [
        ("in other words,", False),
        ("we will see", True),
    ]


@pytest.mark.parametrize(
    "text, boundary",
    [
        ("Done. Next", len("Done. ")),
        ('He said "stop." Then', len('He said "stop." ')),
        ("One. Two? Three", len("One. Two? ")),
        ("Wait... and", len("Wait... ")),
        ("Ask Mr. Smith", 0),
        ("The U.K. team", 0),
        ("Version 3.5 shipped", 0),
        ("No end yet", 0),
    ],
)
def test_sentence_boundary(text, boundary):
    assert transcription._sentence_boundary(text) == boundary


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


@pytest.fixture
def translation_harness(tmp_path, monkeypatch):
    """Run the session-long activity offline with a scripted chat stream."""
    store = FileStore(tmp_path)
    directory = "translation/offline"
    requests, warmups = [], []
    gates: dict[str, asyncio.Event] = {}

    class Stream:
        def __init__(self, text):
            self.tokens = [text[:3], text[3:]]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def __aiter__(self):
            for token in self.tokens:
                yield SimpleNamespace(
                    data=SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=token))]
                    )
                )

    async def stream_async(**kwargs):
        content = kwargs["messages"][1]["content"]
        segment = content.rsplit("Current segment:\n", 1)[1]
        language = kwargs["messages"][0]["content"].split(" into ", 1)[1].split(".")[0]
        requests.append((segment, language, kwargs["messages"]))
        if segment in gates:
            await gates[segment].wait()
        if segment == "boom" and language == "French":
            raise RuntimeError("private speech must not be logged")
        return Stream(f"{language}:{segment}")

    async def complete_async(**kwargs):
        warmups.append(kwargs)

    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(
                stream_async=stream_async, complete_async=complete_async
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(translation, "Mistral", Client)
    monkeypatch.setattr(translation, "POLL_SECONDS", 0.005)
    monkeypatch.setattr(
        translation, "Settings", lambda: Settings(data_dir=tmp_path, api_key="k")
    )
    monkeypatch.setattr(translation.workflows, "activity_heartbeat", lambda: None)
    task_module = inspect.getmodule(translation.Task)
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
    jobs = []

    def control(stopped=False):
        store.write_json(
            f"{directory}/control.json",
            {"count": len(jobs), "stopped": stopped, "updated_at": now_iso()},
        )

    def add(text, partial=False, segment_index=None):
        job = {
            "job_id": len(jobs),
            "segment_index": len(jobs) if segment_index is None else segment_index,
            "text": text,
            "previous_text": "Earlier.",
            "previous_translations": {"fr": "Avant."},
            "partial_sentence": partial,
        }
        store.write_json(f"{directory}/{len(jobs):05d}.json", job)
        jobs.append(job)
        control()

    def events():
        reducer = TaskStateReducer()
        return [state for event in emitted if (state := reducer.consume(event))]

    async def run():
        return await translation.translate_live_jobs.__original_func__(
            LiveTranslationInput(jobs_dir=directory, translation_model="offline")
        )

    control()
    return SimpleNamespace(
        store=store,
        directory=directory,
        add=add,
        control=control,
        events=events,
        run=run,
        requests=requests,
        warmups=warmups,
        gates=gates,
    )


def test_live_translation_runs_jobs_concurrently_and_writes_results(
    translation_harness,
):
    h = translation_harness

    async def run():
        h.gates["Slow sentence."] = asyncio.Event()
        task = asyncio.create_task(h.run())
        h.add("Slow sentence.")
        h.add("Fast sentence.")
        # The second job finishes while the first is still blocked: no serialization.
        await until(lambda: h.store.path(f"{h.directory}/00001.result.json").exists())
        assert not h.store.path(f"{h.directory}/00000.result.json").exists()
        h.gates["Slow sentence."].set()
        h.control(stopped=True)
        assert await task == {"jobs": 2}

    asyncio.run(run())
    assert len(h.warmups) == 1 and h.warmups[0]["max_tokens"] == 1
    first = h.store.read_json(f"{h.directory}/00000.result.json")
    assert first == {
        "job_id": 0,
        "segment_index": 0,
        "translations": {
            "de": "German:Slow sentence.",
            "fr": "French:Slow sentence.",
            "es": "Spanish:Slow sentence.",
            "it": "Italian:Slow sentence.",
        },
        "error": False,
    }
    results = [e for e in h.events() if e["type"] == "translation_result"]
    # Started and completed snapshots both carry the result; the backend dedupes.
    assert [r["job_id"] for r in results] == [1, 1, 0, 0]
    assert results[0] == results[1]
    partial = [e for e in h.events() if e["type"] == "translation" and not e["final"]]
    assert {e["job_id"] for e in partial} == {0, 1}
    messages = next(m for s, lang, m in h.requests if lang == "French")
    assert "Earlier.\n\nEarlier French translation:\nAvant." in messages[1]["content"]
    assert "complete sentences" in messages[0]["content"]


def test_live_translation_fragment_prompt_and_partial_failure(
    translation_harness, caplog
):
    h = translation_harness

    async def run():
        task = asyncio.create_task(h.run())
        h.add("we will once again", partial=True)
        h.add("boom")
        h.control(stopped=True)
        await task

    asyncio.run(run())
    fragment_prompt = next(m for s, _, m in h.requests if s == "we will once again")
    assert "unfinished piece of a sentence" in fragment_prompt[0]["content"]
    failed = h.store.read_json(f"{h.directory}/00001.result.json")
    assert failed["error"] is True
    assert set(failed["translations"]) == {"de", "es", "it"}
    assert "private speech" not in caplog.text


def test_live_translation_stops_when_producer_heartbeat_expires(translation_harness):
    h = translation_harness
    h.store.write_json(
        f"{h.directory}/control.json",
        {"count": 0, "stopped": False, "updated_at": "2020-01-01T00:00:00+00:00"},
    )
    with pytest.raises(RuntimeError, match="heartbeat expired"):
        asyncio.run(h.run())


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
    translate = AsyncMock(return_value={"jobs": 3})
    monkeypatch.setattr(live_translation, "translate_live_jobs", translate)
    translation_params = LiveTranslationInput(
        jobs_dir="translation/offline", translation_model="offline"
    )
    assert asyncio.run(
        live_translation.LiveTranslationWorkflow().run(translation_params)
    ) == {"result": {"jobs": 3}}
    translate.assert_awaited_once_with(translation_params)

    async def validate():
        classes = workflow_classes()
        assert len(classes) == 5
        runner = SandboxedWorkflowRunner(restrictions=get_sandbox_restrictions())
        definitions = [workflow._Definition.must_from_class(cls) for cls in classes]
        for definition in definitions:
            runner.prepare_workflow(definition)
        assert {"voxbridge-live-transcription", "voxbridge-live-translation"} <= {
            d.name for d in definitions
        }

    asyncio.run(validate())
