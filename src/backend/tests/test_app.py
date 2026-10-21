"""Offline acceptance tests: model calls are replaced at the Workflows boundary."""

import asyncio
import io
import json
import struct
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import VOICE_PREVIEW_TEXT, create_app
from backend.config import PROJECT_ROOT, Settings
from backend.storage import FileStore, read_wav
from workers.models import SpeechInput


def wav(seconds=3, rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(struct.pack("<h", 1200) * int(seconds * rate * channels))
    return output.getvalue()


class FakeGateway:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.fail_delete = False
        self.fail_speech = False

    async def execute(self, name, params):
        self.calls.append((name, params))
        if name == "voxbridge-voice-profile":
            if params["action"] == "delete" and self.fail_delete:
                raise RuntimeError("Remote deletion failed")
            return {"remote_voice_id": "remote-voice", "deleted": True}
        SpeechInput.model_validate(params)
        if self.fail_speech:
            raise RuntimeError("Speech workflow unavailable")
        if params.get("stream_dir"):
            self.store.write_bytes(
                f"{params['stream_dir']}/000000.pcm", struct.pack("<f", 0.25) * 2400
            )
            self.store.write_json(f"{params['stream_dir']}/control.json", {"chunks": 1})
            self.store.write_bytes(params["output_path"], wav(rate=24000))
        else:
            self.store.write_bytes(params["output_path"], b"test-audio")
        return {"audio_path": params["output_path"]}

    async def stream(self, name, params):
        self.calls.append((name, params))
        if name == "voxbridge-live-transcription":
            yield {"type": "execution", "execution_id": "live-execution"}
            yield {
                "type": "transcript",
                "text": "Hello",
                "segment_index": 0,
                "final": False,
            }
            yield {
                "type": "transcript",
                "text": "Hello Europe",
                "segment_index": 0,
                "final": True,
            }
            while not self.store.read_json(f"{params['input_dir']}/control.json")[
                "stopped"
            ]:
                await asyncio.sleep(0.01)
            yield {"type": "result", "result": {"text": "Hello Europe"}}
            return
        index = params["segment_index"]
        yield {"type": "execution", "execution_id": f"execution-{index}"}
        translations = {
            "de": "Hallo Europa",
            "fr": "Bonjour Europe",
            "es": "Hola Europa",
            "it": "Ciao Europa",
        }
        for language, text in translations.items():
            yield {
                "type": "translation",
                "language": language,
                "text": text,
                "segment_index": index,
                "final": False,
            }
        yield {
            "type": "result",
            "result": {"text": "Hello Europe", "translations": translations},
        }


@pytest.fixture
def environment(tmp_path):
    settings = Settings(data_dir=tmp_path, api_key="offline-test")
    store = FileStore(tmp_path)
    gateway = FakeGateway(store)
    with TestClient(create_app(settings, gateway)) as client:
        yield client, store, gateway


def add_voice(client, scope="all", sample=None):
    return client.post(
        "/api/voices",
        data={"name": "My voice", "languages": f'["{scope}"]'},
        files={
            "sample": ("sample.wav", wav() if sample is None else sample, "audio/wav")
        },
    )


def test_src_layout_paths(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert PROJECT_ROOT == Path(__file__).resolve().parents[3]
    assert (PROJECT_ROOT / "frontend" / "index.html").is_file()
    assert (PROJECT_ROOT / "pyproject.toml").is_file()
    assert Settings().data_dir == PROJECT_ROOT / "data"


def test_frontend_and_config(environment):
    client, _, _ = environment
    assert "VoxBridge" in client.get("/").text
    assert client.get("/app.js").status_code == 200
    config = client.get("/api/config").json()
    assert [preset["chunk_seconds"] for preset in config["latency_presets"]] == [
        0.2,
        1,
        2,
    ]
    assert [preset["target_delay_ms"] for preset in config["latency_presets"]] == [
        240,
        960,
        1920,
    ]
    assert [item["code"] for item in config["languages"]] == ["de", "fr", "es", "it"]
    assert "api_key" not in config


def test_voice_lifecycle_and_scopes(environment):
    client, store, gateway = environment
    french = add_voice(client, "fr").json()
    german = add_voice(client, "de").json()
    assert french["id"] != german["id"]
    assert "consent_at" not in french
    assert "consent_at" not in store.voice(french["id"])
    assert len(client.get("/api/voices").json()["voices"]) == 2
    assert client.get(f"/api/voices/{french['id']}/sample").content == wav()
    assert gateway.calls[0][0] == "voxbridge-voice-profile"
    assert (
        client.post(
            "/api/sessions", json={"speech_language": "de", "voice_id": french["id"]}
        ).status_code
        == 422
    )
    assert client.delete(f"/api/voices/{french['id']}").status_code == 204
    assert not store.path(french["sample_path"]).exists()
    assert client.get(f"/api/voices/{french['id']}/sample").status_code == 404


def test_reject_invalid_voice_samples(environment):
    for kwargs in [
        {"scope": "xx"},
        {"sample": b"invalid"},
        {"sample": wav(1)},
        {"sample": wav(rate=8000)},
        {"sample": wav(channels=2)},
    ]:
        client, _, gateway = environment
        assert add_voice(client, **kwargs).status_code == 422
        assert not gateway.calls


def test_delete_failure_preserves_local_voice(environment):
    client, store, gateway = environment
    voice = add_voice(client).json()
    gateway.fail_delete = True
    assert client.delete(f"/api/voices/{voice['id']}").status_code == 502
    assert store.voice(voice["id"])
    assert store.path(voice["sample_path"]).exists()


def test_english_voice_preview_uses_saved_voice_workflow(environment):
    client, store, gateway = environment
    scope = "fr"
    voice = add_voice(client, scope).json()
    response = client.post(f"/api/voices/{voice['id']}/preview")
    assert response.status_code == 200
    preview = response.json()
    assert preview["voice_id"] == voice["id"]
    assert not preview["cached"]
    events = [
        json.loads(line) for line in client.get(preview["stream_url"]).text.splitlines()
    ]
    assert events[0]["type"] == "audio"
    assert events[0]["sample_rate"] == 24000
    preview["url"] = events[-1]["url"]
    assert client.get(preview["url"]).content == wav(rate=24000)
    assert client.get(preview["url"]).headers["content-type"] == "audio/wav"
    name, params = gateway.calls[-1]
    assert name == "voxbridge-spoken-translation"
    assert params == {
        "text": VOICE_PREVIEW_TEXT,
        "language": "en",
        "segment_index": 0,
        "output_path": preview["url"].removeprefix("/api/audio/"),
        "model": Settings().speech_model,
        "remote_voice_id": voice["remote_voice_id"],
        "stream_dir": params["stream_dir"],
    }
    assert client.get("/api/config").json()["voice_preview_text"] == params["text"]
    assert params["text"] == (
        "EU Countries have reached a unique agreement to deploy an exceptional budget "
        "to train “Le Gros Chaton” with Mistral AI. "
        "The three-year programme will coordinate open datasets, public demonstrations, "
        "and multilingual workshops across every member state."
    )
    assert store.voice(voice["id"])["languages"] == [scope]
    assert not store.list_records("sessions")
    second = client.post(f"/api/voices/{voice['id']}/preview").json()
    assert second["url"] == preview["url"]
    assert second["cached"]
    assert len(gateway.calls) == 2


def test_voice_preview_rejects_missing_voice(environment):
    identifier = "a" * 32
    client, _, gateway = environment
    assert client.post(f"/api/voices/{identifier}/preview").status_code == 404
    assert not gateway.calls


@pytest.fixture
def completed_session(environment):
    client, store, gateway = environment
    identifier = client.post("/api/sessions", json={}).json()["id"]
    record = store.session(identifier)
    record.update(
        status="completed",
        segments=[
            {
                "segment_index": 0,
                "text": "Hello Europe",
                "final": True,
                "translations": {"fr": "Bonjour Europe"},
            }
        ],
    )
    store.write_json(f"sessions/{identifier}.json", record)
    return client, store, gateway, identifier, record


def test_recorded_translation_rejects_unfinished_sessions(completed_session):
    status = "recording"
    client, store, gateway, identifier, record = completed_session
    record["status"] = status
    store.write_json(f"sessions/{identifier}.json", record)
    assert (
        client.post(
            f"/api/sessions/{identifier}/speech",
            json={
                "language": "fr",
                "segment_index": 0,
            },
        ).status_code
        == 409
    )
    assert not gateway.calls


def test_recorded_translation_rejects_missing_or_invalid_phrases(completed_session):
    for payload in [
        {"language": "en", "segment_index": 0},
        {"language": "fr", "segment_index": -1},
        {"language": "fr", "segment_index": 99},
        {"language": "de", "segment_index": 0},
    ]:
        client, _, gateway, identifier, _ = completed_session
        assert (
            client.post(f"/api/sessions/{identifier}/speech", json=payload).status_code
            == 422
        )
        assert not gateway.calls


def test_recorded_translation_does_not_speak_partial_or_failed_translation(
    completed_session,
):
    for changes in [{"final": False}, {"translation_error": "Failed"}]:
        client, store, gateway, identifier, record = completed_session
        record["segments"][0].update(changes)
        store.write_json(f"sessions/{identifier}.json", record)
        assert (
            client.post(
                f"/api/sessions/{identifier}/speech",
                json={
                    "language": "fr",
                    "segment_index": 0,
                },
            ).status_code
            == 422
        )
        assert not gateway.calls


def test_recorded_translation_voice_scope_default_and_retry(completed_session):
    client, store, gateway, identifier, record = completed_session
    german = add_voice(client, "de").json()
    payload = {"language": "fr", "segment_index": 0, "voice_id": german["id"]}
    calls = len(gateway.calls)
    assert (
        client.post(f"/api/sessions/{identifier}/speech", json=payload).status_code
        == 422
    )
    assert len(gateway.calls) == calls
    payload["voice_id"] = "a" * 32
    assert (
        client.post(f"/api/sessions/{identifier}/speech", json=payload).status_code
        == 404
    )
    payload["voice_id"] = None
    gateway.fail_speech = True
    assert (
        client.post(f"/api/sessions/{identifier}/speech", json=payload).status_code
        == 502
    )
    assert store.session(identifier) == record
    gateway.fail_speech = False
    assert (
        client.post(f"/api/sessions/{identifier}/speech", json=payload).status_code
        == 200
    )
    assert gateway.calls[-1][1]["remote_voice_id"] is None
    assert (
        client.post("/api/sessions/" + "b" * 32 + "/speech", json=payload).status_code
        == 404
    )


def test_store_rejects_path_traversal(environment):
    _, store, _ = environment
    with pytest.raises(ValueError):
        store.path("../../.env")
    with pytest.raises(ValueError):
        store.voice("../secret")


def test_wav_rejects_truncation():
    with pytest.raises(ValueError):
        read_wav(wav()[:-10], max_seconds=30)
