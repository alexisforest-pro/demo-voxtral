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


def new_session(tmp_path, gateway, identifier="a", **record):
    store = gateway.store
    session = LiveSession(
        Socket(),
        {"id": identifier * 32, "source_language": "en", "segments": [], **record},
        Settings(data_dir=tmp_path),
        store,
        gateway,
    )
    session.index = 1
    session.publish_input()
    session.input_available.set()
    return session


async def transcript(session, index, text, sentence_end=True):
    await session.accept_transcript(
        {
            "type": "transcript",
            "segment_index": index,
            "text": text,
            "final": True,
            "sentence_end": sentence_end,
        }
    )


async def finish(session, task):
    await asyncio.wait_for(session.drain_translations(), timeout=2)
    await asyncio.wait_for(task, timeout=2)


def test_one_translation_workflow_per_session_and_transcript_never_waits(tmp_path):
    async def run():
        store = FileStore(tmp_path)
        release = asyncio.Event()

        class Gateway(FakeGateway):
            async def translate(self, job):
                await release.wait()
                return await super().translate(job)

        gateway = Gateway(store)
        session = new_session(tmp_path, gateway)
        task = asyncio.create_task(session.process_translations())
        await transcript(session, 0, "First phrase.")
        await transcript(session, 1, "Next phrase is already live.")
        # Replay must not create a duplicate job.
        await transcript(session, 0, "First phrase.")
        assert len(session.record["segments"]) == 2
        assert session.pending_jobs() == 2
        assert sum(e["type"] == "transcript" for e in session.socket.events) == 2
        release.set()
        await finish(session, task)
        names = [name for name, _ in gateway.calls]
        assert names == ["voxbridge-live-translation"]
        params = gateway.calls[0][1]
        assert params["jobs_dir"] == f"translation/{'a' * 32}"
        assert [job["text"] for job in gateway.jobs] == [
            "First phrase.",
            "Next phrase is already live.",
        ]
        assert gateway.jobs[1]["previous_text"] == " First phrase."
        # Job 1 was sent while job 0 was still translating: no waiting for context.
        assert gateway.jobs[1]["previous_translations"] == {}
        assert all(s["translations"] for s in session.record["segments"])
        assert session.record["translation_execution_id"] == "translation-execution"
        assert store.read_json(f"translation/{'a' * 32}/control.json")["stopped"]

    asyncio.run(run())


def test_translation_backlog_preserves_original_text(tmp_path):
    async def run():
        store = FileStore(tmp_path)
        session = new_session(tmp_path, FakeGateway(store), "b")
        for index in range(12):
            await transcript(session, index, f"Phrase {index}.")
        await asyncio.sleep(0)
        assert len(session.record["segments"]) == 12
        assert session.pending_jobs() == 8
        assert sum(e["type"] == "transcript" for e in session.socket.events) == 12
        assert "translation_error" in session.record["segments"][-1]
        assert any(e["type"] == "error" for e in session.socket.events)

    asyncio.run(run())


def test_fragments_are_provisional_then_replaced_by_whole_sentence(tmp_path):
    async def run():
        store = FileStore(tmp_path)

        class Gateway(FakeGateway):
            async def translate(self, job):
                return {"fr": f"FR[{job['text']}]"}

        gateway = Gateway(store)
        session = new_session(tmp_path, gateway, "d")
        session.speech_language = "fr"
        task = asyncio.create_task(session.process_translations())
        events = [
            ("Hello all.", True),
            ("From next year on, we will once again take", False),
            ("part in Erasmus+, in other", False),
            ("words, we will see students.", True),
        ]
        for index, (text, sentence_end) in enumerate(events):
            await transcript(session, index, text, sentence_end)
            # Let each fragment finish so the next one continues its translation.
            await asyncio.wait_for(
                _until(lambda: session.fragment_job is None), timeout=2
            )
        await finish(session, task)
        jobs = gateway.jobs
        assert [(j["text"], j["partial_sentence"]) for j in jobs] == [
            ("Hello all.", False),
            ("From next year on, we will once again take", True),
            ("part in Erasmus+, in other", True),
            (" ".join(text for text, _ in events[1:]), False),
        ]
        assert jobs[2]["previous_translations"] == {
            "fr": "FR[Hello all.] FR[From next year on, we will once again take]"
        }
        # The whole sentence sees committed sentences only, never fragment drafts.
        assert jobs[3]["previous_translations"] == {"fr": "FR[Hello all.]"}
        assert jobs[3]["previous_text"] == jobs[1]["previous_text"] == " Hello all."
        segments = session.record["segments"]
        assert [s.get("merged_into") for s in segments] == [None, 3, 3, None]
        assert segments[1]["translations"] == segments[2]["translations"] == {}
        final = [
            e
            for e in session.socket.events
            if e["type"] == "translation" and e.get("replaces") and e["final"]
        ]
        assert final == [
            {
                "type": "translation",
                "language": "fr",
                "text": jobs[3]["text"].join(["FR[", "]"]),
                "segment_index": 3,
                "final": True,
                "replaces": [1, 2],
            }
        ]
        spoken = [session.speech_queue.get_nowait()[0] for _ in range(2)]
        assert spoken == [0, 3]
        assert session.speech_queue.empty()

    asyncio.run(run())


def test_stale_fragments_skipped_and_sentences_spoken_in_order(tmp_path):
    async def run():
        store = FileStore(tmp_path)
        gates = {text: asyncio.Event() for text in ("slow piece", "First.")}

        class Gateway(FakeGateway):
            async def live_translation(self, params):
                # Process jobs concurrently, like the real activity.
                yield {"type": "execution", "execution_id": "translation-execution"}
                directory, index, results = params["jobs_dir"], 0, asyncio.Queue()

                async def run_job(job_id):
                    job = self.store.read_json(f"{directory}/{job_id:05d}.json")
                    self.jobs.append(job)
                    if job["text"] in gates:
                        await gates[job["text"]].wait()
                    await results.put(
                        {
                            "type": "translation_result",
                            "job_id": job_id,
                            "segment_index": job["segment_index"],
                            "translations": {"fr": f"FR[{job['text']}]"},
                            "error": False,
                        }
                    )

                running = set()
                while True:
                    control = self.store.read_json(f"{directory}/control.json")
                    while index < control["count"]:
                        running.add(asyncio.create_task(run_job(index)))
                        index += 1
                    while not results.empty():
                        yield results.get_nowait()
                    running = {t for t in running if not t.done()}
                    if control["stopped"] and not running and results.empty():
                        return
                    await asyncio.sleep(0.005)

        gateway = Gateway(store)
        session = new_session(tmp_path, gateway, "e")
        session.speech_language = "fr"
        task = asyncio.create_task(session.process_translations())
        await transcript(session, 0, "First.")
        await transcript(session, 1, "slow piece", sentence_end=False)
        await transcript(session, 2, "waiting piece", sentence_end=False)
        await transcript(session, 3, "and the end.")
        await transcript(session, 4, "Second.")
        # Sentences 3 and 4 finish while the first sentence and fragment are blocked.
        await asyncio.wait_for(
            _until(
                lambda: (
                    session.segments[3]["translations"]
                    and session.segments[4]["translations"]
                )
            ),
            2,
        )
        assert session.segments[3]["translations"] == {
            "fr": "FR[slow piece waiting piece and the end.]"
        }
        # Speech waits for sentence 0 so audio keeps the spoken order.
        assert session.speech_queue.empty()
        gates["First."].set()
        gates["slow piece"].set()
        await finish(session, task)
        texts = [job["text"] for job in gateway.jobs]
        assert "waiting piece" not in texts
        assert session.segments[2]["translation_skipped"] is True
        assert session.segments[2]["merged_into"] == 3
        spoken = []
        while not session.speech_queue.empty():
            spoken.append(session.speech_queue.get_nowait()[0])
        assert spoken == [0, 3, 4]
        late = [
            e
            for e in session.socket.events
            if e["type"] == "translation" and e["segment_index"] == 1
        ]
        assert late == []

    asyncio.run(run())


async def _until(predicate):
    while not predicate():
        await asyncio.sleep(0.005)


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
            if name == "voxbridge-live-translation":
                async for event in super().stream(name, params):
                    yield event
                return
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
