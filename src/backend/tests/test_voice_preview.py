"""Offline streamed preview generation, single-flight caching and failure boundaries."""

import asyncio
import base64
import io
import json
import struct
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mistralai.client.models import SpeechStreamAudioDelta, SpeechStreamDone

from backend.config import Settings
from backend.storage import FileStore
from backend.tests.test_app import wav
from backend.voice_preview import VoicePreviews
from workers.activities import speech
from workers.models import SpeechInput


@pytest.mark.parametrize("broken", [None, "missing-done", "partial-float", "nonfinite"])
def test_activity_streams_before_done_and_only_caches_complete_audio(
    tmp_path, monkeypatch, broken
):
    async def run():
        settings = Settings(data_dir=tmp_path, api_key="offline")
        store = FileStore(tmp_path)
        params = SpeechInput(
            text="Hello Europe",
            language="en",
            segment_index=0,
            output_path="audio/output/preview.wav",
            stream_dir="audio/output/chunks",
            model="offline",
            remote_voice_id="clone",
        )
        raw = struct.pack("<3f", 0.25, -0.5, 1)
        if broken == "nonfinite":
            raw = struct.pack("<f", float("nan"))
        elif broken == "partial-float":
            raw += b"x"

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def __aiter__(self):
                yield SimpleNamespace(
                    data=SpeechStreamAudioDelta(
                        audio_data=base64.b64encode(raw).decode()
                    )
                )
                if broken != "nonfinite":
                    assert store.path("audio/output/chunks/000000.pcm").exists()
                    assert not store.path(params.output_path).exists()
                if broken != "missing-done":
                    yield SimpleNamespace(data=SpeechStreamDone(usage={}))

        complete = AsyncMock(return_value=Stream())

        class Client:
            audio = SimpleNamespace(speech=SimpleNamespace(complete_async=complete))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

        monkeypatch.setattr(speech, "Settings", lambda: settings)
        monkeypatch.setattr(speech, "Mistral", lambda **_kwargs: Client())
        if broken:
            with pytest.raises(ValueError):
                await speech.generate_speech.__original_func__(params)
            assert not store.path(params.output_path).exists()
        else:
            result = await speech.generate_speech.__original_func__(params)
            with wave.open(
                io.BytesIO(store.path(params.output_path).read_bytes()), "rb"
            ) as audio:
                assert audio.getframerate() == 24000
                assert struct.unpack("<3h", audio.readframes(3)) == (
                    8192,
                    -16384,
                    32767,
                )
            assert result == {"audio_path": params.output_path}
        complete.assert_awaited_once_with(
            model="offline",
            input="Hello Europe",
            voice_id="clone",
            response_format="pcm",
            stream=True,
        )

    asyncio.run(run())


def test_preview_single_flight_early_chunks_persistent_cache_and_invalidation(tmp_path):
    async def run():
        settings = Settings(data_dir=tmp_path, api_key="offline")
        store = FileStore(tmp_path)
        voice = {"id": "a" * 32, "remote_voice_id": "clone"}
        release = asyncio.Event()

        async def execute(_name, params):
            store.write_bytes(
                f"{params['stream_dir']}/000000.pcm", struct.pack("<f", 0.2)
            )
            store.write_json(f"{params['stream_dir']}/control.json", {"chunks": 1})
            await release.wait()
            store.write_bytes(params["output_path"], wav(rate=24000))

        gateway = SimpleNamespace(execute=AsyncMock(side_effect=execute))
        previews = VoicePreviews(settings, store, gateway)
        first = previews.prepare(voice, "Hello")
        second = previews.prepare(voice, "Hello")
        assert first == second
        job = next(iter(previews.jobs.values()))
        await asyncio.sleep(0)
        stream = previews.events(job)
        assert json.loads(await anext(stream))["type"] == "audio"
        assert not job.task.done()
        await stream.aclose()
        assert not job.task.cancelled()
        release.set()
        await job.task
        cached = previews.prepare(voice, "Hello")
        assert cached["cached"]
        assert gateway.execute.await_count == 1
        restarted = VoicePreviews(settings, store, gateway)
        assert restarted.prepare(voice, "Hello") == cached
        store.write_bytes(job.output, wav(rate=24000)[:-20])
        assert not restarted.prepare(voice, "Hello")["cached"]
        await restarted.close()
        changed = previews.prepare(voice, "Changed text")
        assert not changed["cached"]
        other = previews.prepare({**voice, "remote_voice_id": "new-clone"}, "Hello")
        assert other["stream_url"] != first["stream_url"]
        model_changed = VoicePreviews(
            Settings(data_dir=tmp_path, speech_model="other"), store, gateway
        )
        assert not model_changed.prepare(voice, "Hello")["cached"]
        await model_changed.close()
        await previews.close()

    asyncio.run(run())


def test_http_preview_stream_relays_audio_while_workflow_is_blocked(tmp_path):
    import httpx

    from backend.app import create_app

    async def run():
        store = FileStore(tmp_path)
        voice = {"id": "a" * 32, "remote_voice_id": "clone"}
        store.write_json(f"voices/{voice['id']}.json", voice)
        release = asyncio.Event()

        async def execute(_name, params):
            store.write_bytes(
                f"{params['stream_dir']}/000000.pcm", struct.pack("<f", 0.25)
            )
            store.write_json(f"{params['stream_dir']}/control.json", {"chunks": 1})
            await release.wait()
            store.write_bytes(params["output_path"], wav(rate=24000))

        gateway = SimpleNamespace(execute=AsyncMock(side_effect=execute))
        app = create_app(Settings(data_dir=tmp_path, api_key="offline"), gateway)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client,
        ):
            first = (await client.post(f"/api/voices/{voice['id']}/preview")).json()
            duplicate = (await client.post(f"/api/voices/{voice['id']}/preview")).json()
            assert duplicate == first
            messages = []

            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body" and message.get("body"):
                    event = json.loads(message["body"])
                    if event["type"] == "audio":
                        assert not release.is_set()
                        release.set()

            async def receive():
                await asyncio.Event().wait()

            async with asyncio.timeout(2):
                await app(
                    {
                        "type": "http",
                        "asgi": {"version": "3.0", "spec_version": "2.3"},
                        "http_version": "1.1",
                        "method": "GET",
                        "scheme": "http",
                        "path": first["stream_url"],
                        "raw_path": first["stream_url"].encode(),
                        "query_string": b"",
                        "headers": [],
                        "server": ("test", 80),
                        "client": ("test", 1),
                    },
                    receive,
                    send,
                )
            events = [
                json.loads(message["body"])
                for message in messages
                if message.get("body")
            ]
            assert [event["type"] for event in events] == ["audio", "done"]
            cached = (await client.post(f"/api/voices/{voice['id']}/preview")).json()
            assert cached["cached"]
            assert gateway.execute.await_count == 1
            assert (await client.get(cached["url"])).content == wav(rate=24000)
            assert (
                await client.get(first["stream_url"].replace(voice["id"], "b" * 32))
            ).status_code == 404

    asyncio.run(run())


def test_long_stream_generates_every_block_and_caches_combined_audio(tmp_path):
    async def run():
        settings = Settings(data_dir=tmp_path)
        store = FileStore(tmp_path)
        text = " ".join(f"word{index}" for index in range(610))
        inputs = []

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def __aiter__(self):
                yield SimpleNamespace(
                    data=SpeechStreamAudioDelta(
                        audio_data=base64.b64encode(
                            struct.pack("<f", 0.25) * 120
                        ).decode()
                    )
                )
                yield SimpleNamespace(data=SpeechStreamDone(usage={}))

        async def complete(**kwargs):
            inputs.append(kwargs["input"])
            return Stream()

        client = SimpleNamespace(
            audio=SimpleNamespace(speech=SimpleNamespace(complete_async=complete))
        )
        params = SpeechInput(
            text=text,
            language="de",
            segment_index=0,
            output_path="audio/output/full.wav",
            stream_dir="audio/output/full-stream",
            model="offline",
            max_audio_seconds=1800,
        )
        await speech.stream_speech(client, params, "clone", settings)
        assert " ".join(inputs).split() == text.split()
        assert len(inputs) == 3
        assert store.read_json(f"{params.stream_dir}/control.json")["chunks"] == 3
        with wave.open(str(store.path(params.output_path)), "rb") as audio:
            assert audio.getnframes() == 360

    asyncio.run(run())


def test_failed_preview_is_not_cached_and_next_request_retries(tmp_path):
    async def run():
        store = FileStore(tmp_path)
        gateway = SimpleNamespace(
            execute=AsyncMock(side_effect=RuntimeError("secret upstream error"))
        )
        previews = VoicePreviews(Settings(data_dir=tmp_path), store, gateway)
        voice = {"id": "a" * 32, "remote_voice_id": "clone"}
        first = previews.prepare(voice, "Hello")
        job = next(iter(previews.jobs.values()))
        await job.task
        events = [json.loads(event) async for event in previews.events(job)]
        assert events[0]["type"] == "error"
        assert "secret" not in events[0]["message"]
        assert not store.path(job.output).exists()
        retry = previews.prepare(voice, "Hello")
        assert retry == first
        assert next(iter(previews.jobs.values())).stream_dir != job.stream_dir
        await previews.close()

    asyncio.run(run())
