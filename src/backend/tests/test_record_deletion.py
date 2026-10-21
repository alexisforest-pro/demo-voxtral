"""Bulk deletion removes local records, retains busy/failing rows and preserves audio."""

import asyncio

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.storage import FileStore, now_iso
from backend.tests.test_app import FakeGateway


def test_delete_records_busy_failure_retry_and_preserved_files(tmp_path, monkeypatch):
    store = FileStore(tmp_path)
    settings = Settings(data_dir=tmp_path, api_key="offline-test")
    identifiers = [letter * 32 for letter in "abc"]
    for identifier in identifiers:
        store.write_json(
            f"sessions/{identifier}.json",
            {
                "id": identifier,
                "created_at": now_iso(),
                "status": "completed",
                "segments": [{"segment_index": 0, "text": "Climate investment."}],
            },
        )
        store.write_json(
            f"intelligence/{identifier}.json", {"status": "not_started", "report": None}
        )
        store.write_json(
            f"intelligence/{identifier}/snapshot.json", {"text": "Climate investment."}
        )
        store.write_bytes(f"audio/input/{identifier}/00000.wav", b"recording")
        store.write_bytes(
            f"audio/output/recorded-playback/{identifier}/cached.wav", b"speech"
        )
    store.write_json(
        "voices/" + "d" * 32 + ".json",
        {"id": "d" * 32, "created_at": now_iso(), "name": "My voice"},
    )
    release = asyncio.Event()
    started = asyncio.Event()

    class Gateway(FakeGateway):
        async def stream(self, name, params):
            self.calls.append((name, params))
            started.set()
            await release.wait()
            yield {"type": "result", "result": {"title": "Climate investment"}}

    gateway = Gateway(store)
    delete = FileStore.delete_session
    fail = True

    def fail_one(self, identifier):
        if fail and identifier == identifiers[1]:
            raise PermissionError("private filesystem detail")
        delete(self, identifier)

    monkeypatch.setattr(FileStore, "delete_session", fail_one)
    with TestClient(create_app(settings, gateway)) as client:
        client.post(f"/api/sessions/{identifiers[0]}/intelligence")
        client.portal.call(started.wait)
        result = client.delete("/api/sessions").json()
        assert result["deleted"] == [identifiers[2]]
        assert {item["id"] for item in result["retained"]} == set(identifiers[:2])
        assert "private filesystem" not in str(result)
        assert not store.path(f"sessions/{identifiers[2]}.json").exists()
        assert not store.path(f"intelligence/{identifiers[2]}").exists()
        assert not store.path(f"intelligence/{identifiers[2]}.json").exists()
        assert (
            client.get(f"/api/sessions/{identifiers[2]}/intelligence").status_code
            == 404
        )
        assert client.get(f"/api/sessions/{identifiers[2]}/export").status_code == 404
        assert len(client.get("/api/sessions").json()["sessions"]) == 2
        client.portal.call(release.set)
        for _ in range(100):
            state = client.get(f"/api/sessions/{identifiers[0]}/intelligence").json()[
                "intelligence"
            ]
            if state["status"] == "completed":
                break
        assert state["status"] == "completed"
        fail = False
        result = client.delete("/api/sessions").json()
        assert set(result["deleted"]) == set(identifiers[:2]) and not result["retained"]
        assert client.get("/api/sessions").json()["sessions"] == []
        assert client.delete("/api/sessions").json() == {"deleted": [], "retained": []}
        identifier = client.post("/api/sessions", json={}).json()["id"]
        with client.websocket_connect(f"/api/sessions/{identifier}/live") as socket:
            socket.receive_json()
            result = client.delete("/api/sessions").json()
            assert not result["deleted"] and result["retained"][0]["id"] == identifier
            socket.send_json({"type": "stop"})
            while socket.receive_json()["type"] != "stopped":
                pass
        assert client.delete("/api/sessions").json()["deleted"] == [identifier]
        assert len(gateway.calls) == 1
        assert client.get("/api/voices").json()["voices"][0]["name"] == "My voice"
    for identifier in identifiers:
        assert (
            store.path(f"audio/input/{identifier}/00000.wav").read_bytes()
            == b"recording"
        )
        assert (
            store.path(
                f"audio/output/recorded-playback/{identifier}/cached.wav"
            ).read_bytes()
            == b"speech"
        )
    assert not list(store.path("sessions").glob("*.json"))
