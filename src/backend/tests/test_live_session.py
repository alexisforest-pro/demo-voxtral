"""Continuous STT stays independent of slow translation and speech workflows."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.live_session import LiveSession
from backend.storage import FileStore
from backend.tests.test_app import FakeGateway, wav


class Socket:
    def __init__(self):
        self.events = []
        self.messages = asyncio.Queue()

    async def send_json(self, event):
        self.events.append(event.copy())

    async def receive(self):
        return await self.messages.get()


def test_transcript_updates_while_translation_is_blocked(tmp_path):
    async def run():
        store = FileStore(tmp_path)
        socket = Socket()
        first_translation = asyncio.Event()
        release_translation = asyncio.Event()
        second_phrase = asyncio.Event()

        class Gateway(FakeGateway):
            async def stream(self, name, params):
                if name == "voxbridge-live-transcription":
                    self.calls.append((name, params))
                    yield {
                        "type": "transcript",
                        "text": "First phrase.",
                        "segment_index": 0,
                        "final": True,
                    }
                    await first_translation.wait()
                    yield {
                        "type": "transcript",
                        "text": "Next phrase is already live",
                        "segment_index": 1,
                        "final": False,
                    }
                    yield {
                        "type": "transcript",
                        "text": "Next phrase is already live.",
                        "segment_index": 1,
                        "final": True,
                    }
                    # Replay must not enqueue a duplicate translation.
                    yield {
                        "type": "transcript",
                        "text": "First phrase.",
                        "segment_index": 0,
                        "final": True,
                    }
                    second_phrase.set()
                    return
                first_translation.set()
                await release_translation.wait()
                async for event in super().stream(name, params):
                    yield event

        gateway = Gateway(store)
        session = LiveSession(
            socket,
            {
                "id": "a" * 32,
                "latency": "fast",
                "source_language": "auto",
                "segments": [],
            },
            Settings(data_dir=tmp_path),
            store,
            gateway,
        )
        session.index = 1
        session.publish_input()
        session.input_available.set()
        translation_task = asyncio.create_task(session.process_translations())
        try:
            await asyncio.wait_for(session.process_transcript(), timeout=2)
            assert second_phrase.is_set()
            assert not release_translation.is_set()
            assert any(
                event.get("segment_index") == 1 and event["type"] == "transcript"
                for event in socket.events
            )
            assert len(session.record["segments"]) == 2
            assert session.translation_queue.qsize() == 1
            release_translation.set()
            await asyncio.wait_for(session.translation_queue.join(), timeout=2)
            calls = [
                params
                for name, params in gateway.calls
                if name == "voxbridge-live-segment"
            ]
            assert len(calls) == 2
            assert calls[1]["previous_text"] == " First phrase."
        finally:
            translation_task.cancel()
            await asyncio.gather(translation_task, return_exceptions=True)

    asyncio.run(run())


def test_translation_backlog_preserves_original_text(tmp_path):
    async def run():
        store = FileStore(tmp_path)
        socket = Socket()
        session = LiveSession(
            socket,
            {"id": "b" * 32, "segments": []},
            Settings(data_dir=tmp_path),
            store,
            FakeGateway(store),
        )
        for index in range(12):
            await session.accept_transcript(
                {
                    "type": "transcript",
                    "segment_index": index,
                    "text": f"Phrase {index}",
                    "final": True,
                }
            )
        assert len(session.record["segments"]) == 12
        assert session.translation_queue.qsize() == 8
        assert sum(event["type"] == "transcript" for event in socket.events) == 12
        assert "translation_error" in session.record["segments"][-1]

    asyncio.run(run())


@pytest.mark.parametrize("seconds", [1 / 16000, 3])
def test_audio_input_limit_and_silence_preserved(tmp_path, seconds):
    async def run():
        store = FileStore(tmp_path)
        socket = Socket()
        session = LiveSession(
            socket,
            {"id": "c" * 32, "segments": []},
            Settings(data_dir=tmp_path),
            store,
            FakeGateway(store),
        )
        silent_wav = wav(seconds)[:44] + b"\0" * (int(seconds * 16000) * 2)
        await socket.messages.put({"type": "websocket.receive", "bytes": silent_wav})
        await socket.messages.put(
            {"type": "websocket.receive", "text": '{"type":"stop"}'}
        )
        await session.receive()
        assert store.path(f"{session.input_dir}/00000.wav").read_bytes() == silent_wav
        control = store.read_json(f"{session.input_dir}/control.json")
        assert control["count"] == 1 and control["stopped"]

    asyncio.run(run())


def test_audio_above_three_seconds_is_rejected(tmp_path):
    settings = Settings(data_dir=tmp_path, api_key="offline-test")
    store = FileStore(tmp_path)
    with TestClient(create_app(settings, FakeGateway(store))) as client:
        identifier = client.post("/api/sessions", json={}).json()["id"]
        with client.websocket_connect(f"/api/sessions/{identifier}/live") as socket:
            socket.send_bytes(wav(3.01))
            while True:
                event = socket.receive_json()
                if event["type"] == "error":
                    assert "maximum is 3 seconds" in event["message"]
                    break
        assert store.session(identifier)["status"] == "failed"
        assert not store.path(f"audio/input/{identifier}/00000.wav").exists()


def test_stop_without_audio_does_not_launch_workflows(tmp_path):
    settings = Settings(data_dir=tmp_path, api_key="offline-test")
    store = FileStore(tmp_path)
    gateway = FakeGateway(store)
    with TestClient(create_app(settings, gateway)) as client:
        identifier = client.post("/api/sessions", json={}).json()["id"]
        with client.websocket_connect(f"/api/sessions/{identifier}/live") as socket:
            socket.send_json({"type": "stop"})
            while socket.receive_json()["type"] != "stopped":
                pass
        assert store.session(identifier)["status"] == "completed"
        assert not gateway.calls


@pytest.mark.parametrize("diagnostic_available", [True, False])
def test_worker_failure_is_fatal_and_saved_with_execution(
    tmp_path, diagnostic_available
):
    settings = Settings(data_dir=tmp_path, api_key="offline-test")
    store = FileStore(tmp_path)
    diagnostic = {
        "message": "Voxtral rejected the live audio stream (code 3804): Response timeout.",
        "code": 3804,
        "request_id": "offline-request",
    }

    class Gateway(FakeGateway):
        async def stream(self, name, params):
            yield {"type": "execution", "execution_id": "failed-execution"}
            if diagnostic_available:
                self.store.write_json(
                    f"{params['input_dir']}/workflow_error.json", diagnostic
                )
            raise RuntimeError(
                "Workflow failed. Inspect its execution in Mistral Studio."
            )

    with TestClient(create_app(settings, Gateway(store))) as client:
        identifier = client.post("/api/sessions", json={}).json()["id"]
        with client.websocket_connect(f"/api/sessions/{identifier}/live") as socket:
            socket.send_bytes(wav(0.2))
            while True:
                event = socket.receive_json()
                if event["type"] == "error":
                    assert event["fatal"] is True
                    assert "failed-execution" in event["message"]
                    if diagnostic_available:
                        assert diagnostic["message"] in event["message"]
                    else:
                        assert "worker logs" in event["message"]
                    break
        record = store.session(identifier)
        assert record["status"] == "failed"
        assert record["error"] == event["message"]
        assert record["transcription_error"] == (
            diagnostic if diagnostic_available else {}
        )
        assert store.read_json(f"audio/input/{identifier}/control.json")["stopped"]
