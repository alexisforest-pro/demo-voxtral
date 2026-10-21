"""Default playback uses speech-v1 aliases, not incompatible catalogue UUIDs."""

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from mistralai.client import Mistral

from backend.app import create_app
from backend.config import Settings
from backend.storage import FileStore
from backend.tests.test_app import wav
from workers.activities import speech


@pytest.mark.parametrize(
    "language, explicit, expected",
    [
        ("fr", None, "fr_marie_neutral"),
        ("it", None, "gb_jane_neutral"),
        ("it", "custom-clone", "custom-clone"),
        ("it", "configured-preset", "configured-preset"),
    ],
)
def test_recorded_playback_calls_worker_with_compatible_voice(
    tmp_path, monkeypatch, language, explicit, expected
):
    store = FileStore(tmp_path)
    settings = Settings(
        data_dir=tmp_path,
        api_key="offline",
        default_voice_id=explicit if explicit == "configured-preset" else "",
    )
    identifier = "a" * 32
    voice_id = "b" * 32
    store.write_json(
        f"sessions/{identifier}.json",
        {
            "id": identifier,
            "status": "completed",
            "segments": [
                {
                    "segment_index": 0,
                    "final": True,
                    "translations": {language: "Hello Europe"},
                }
            ],
        },
    )
    if explicit == "custom-clone":
        store.write_json(
            f"voices/{voice_id}.json",
            {
                "id": voice_id,
                "languages": ["all"],
                "remote_voice_id": explicit,
            },
        )
    calls = []

    def voice(name, slug, codes):
        return {
            "id": f"uuid-{slug}",
            "name": name,
            "slug": slug,
            "languages": codes,
            "type": "preset",
            "user_id": None,
            "created_at": "2026-10-01T00:00:00Z",
        }

    def transport(request):
        calls.append(request)
        if request.url.path == "/v1/audio/voices":
            # New single-language presets sort first and are rejected by speech v1.
            items = [
                voice("New voice", "voice_482", [language]),
                voice("Jane", "gb_jane_neutral", ["en_gb"]),
                voice("Marie sad", "fr_marie_sad", ["fr_fr"]),
                voice("Marie neutral", "fr_marie_neutral", ["fr-FR"]),
            ]
            return httpx.Response(
                200,
                json={
                    "items": items,
                    "total": len(items),
                    "page": 0,
                    "page_size": 100,
                    "total_pages": 1,
                },
            )
        assert request.url.path == "/v1/audio/speech"
        payload = json.loads(request.content)
        if payload["voice_id"] == "uuid-voice_482":
            return httpx.Response(
                400,
                json={
                    "message": "voice is not supported for speech v1",
                    "code": "3051",
                },
            )
        assert payload["voice_id"] == expected
        assert payload["response_format"] == "mp3"
        return httpx.Response(
            200, json={"audio_data": base64.b64encode(wav()).decode()}
        )

    def client(**kwargs):
        return Mistral(
            **kwargs,
            async_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        )

    monkeypatch.setattr(speech, "Mistral", client)
    monkeypatch.setattr(speech, "Settings", lambda: settings)

    class Gateway:
        async def execute(self, name, params):
            from workers.models import SpeechInput

            assert name == "voxbridge-spoken-translation"
            return await speech.generate_speech.__original_func__(
                SpeechInput.model_validate(params)
            )

    with TestClient(create_app(settings, Gateway())) as api:
        response = api.post(
            f"/api/sessions/{identifier}/speech",
            json={
                "language": language,
                "segment_index": 0,
                "voice_id": voice_id if explicit == "custom-clone" else None,
            },
        )
        assert response.status_code == 200
        assert api.get(response.json()["url"]).content == wav()
    assert len(calls) == (1 if explicit else 2)


def test_no_compatible_preset_fails_before_speech_request():
    from types import SimpleNamespace

    with pytest.raises(RuntimeError, match="No compatible preset"):
        speech.select_default_voice(
            [
                SimpleNamespace(id="uuid", slug="voice_482", languages=["it"]),
            ],
            "it",
        )
