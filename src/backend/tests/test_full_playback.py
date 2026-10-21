"""Full recorded translation playback refuses extracts and recovers completed remote results."""

import json
from unittest.mock import AsyncMock

import pytest

pytest_plugins = ["backend.tests.test_app"]


@pytest.mark.parametrize("language", ["de", "fr", "es", "it"])
def test_full_playback_recovers_every_phrase_in_order_and_caches(environment, language):
    client, store, gateway = environment
    identifier = client.post("/api/sessions", json={}).json()["id"]
    phrases = [
        {
            "segment_index": index,
            "text": f"Source {index}",
            "final": True,
            "translations": {language: f"Phrase {index}."} if index == 1 else {},
            "execution_id": f"translation-{index}",
        }
        for index in range(14)
    ]
    record = store.session(identifier)
    record.update(status="completed", segments=list(reversed(phrases)))
    store.write_json(f"sessions/{identifier}.json", record)

    async def recover(execution_id):
        index = int(execution_id.split("-")[-1])
        return {"translations": {language: f"Phrase {index}."}}

    gateway.result = AsyncMock(side_effect=recover)
    response = client.post(
        f"/api/sessions/{identifier}/playback", json={"language": language}
    )
    assert response.status_code == 200
    result = response.json()
    assert not result["cached"]
    events = [
        json.loads(line) for line in client.get(result["stream_url"]).text.splitlines()
    ]
    assert events[-1]["type"] == "done"
    params = gateway.calls[-1][1]
    assert params["text"] == " ".join(f"Phrase {index}." for index in range(14))
    assert params["language"] == language
    assert params["max_audio_seconds"] == 1800
    assert params["stream_dir"]
    assert gateway.result.await_count == 13
    second = client.post(
        f"/api/sessions/{identifier}/playback", json={"language": language}
    ).json()
    assert second["cached"]
    assert second["url"] == events[-1]["url"]
    assert len(gateway.calls) == 1
    assert gateway.result.await_count == 13
    assert all(
        segment["translations"] for segment in store.session(identifier)["segments"]
    )


def test_missing_full_translation_is_not_silently_skipped(completed_session):
    client, store, gateway, identifier, record = completed_session
    record["segments"].append(
        {"segment_index": 1, "text": "Untranslated", "final": True, "translations": {}}
    )
    store.write_json(f"sessions/{identifier}.json", record)
    response = client.post(
        f"/api/sessions/{identifier}/playback", json={"language": "fr"}
    )
    assert response.status_code == 409
    assert "phrases 2" in response.json()["detail"]
    assert not gateway.calls


def test_full_playback_validates_session_voice_and_text_limits(environment):
    from backend.tests.test_app import add_voice

    client, store, gateway = environment
    identifier = client.post("/api/sessions", json={}).json()["id"]
    payload = {"language": "fr"}
    assert (
        client.post(f"/api/sessions/{identifier}/playback", json=payload).status_code
        == 409
    )
    voice = add_voice(client, "de").json()
    record = store.session(identifier)
    record.update(
        status="completed",
        segments=[
            {"segment_index": 0, "final": True, "translations": {"fr": "Bonjour"}}
        ],
    )
    store.write_json(f"sessions/{identifier}.json", record)
    assert (
        client.post(
            f"/api/sessions/{identifier}/playback",
            json={**payload, "voice_id": voice["id"]},
        ).status_code
        == 422
    )
    record["segments"][0]["translations"]["fr"] = "word " * 5001
    store.write_json(f"sessions/{identifier}.json", record)
    assert (
        client.post(f"/api/sessions/{identifier}/playback", json=payload).status_code
        == 422
    )
    assert len(gateway.calls) == 1
