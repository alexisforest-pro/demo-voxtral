"""Stop-triggered intelligence is persisted without blocking the live queues."""

import asyncio
import threading

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.live_session import LiveSession
from backend.record_intelligence import RecordIntelligence, session_title
from backend.storage import FileStore, now_iso
from backend.tests.test_app import FakeGateway
from backend.tests.test_live_session import Socket


def test_stop_saves_tail_and_starts_intelligence_before_slow_translation(tmp_path):
    async def run():
        store = FileStore(tmp_path)
        socket = Socket()
        translation_started, release_translation, analysis_started, release_analysis = (
            asyncio.Event() for _ in range(4)
        )

        class Gateway(FakeGateway):
            async def stream(self, name, params):
                if name == "voxbridge-live-transcription":
                    yield {
                        "type": "transcript",
                        "segment_index": 0,
                        "text": "Initial phrase.",
                        "final": True,
                    }
                    await translation_started.wait()
                    await socket.messages.put(
                        {"type": "websocket.receive", "text": '{"type":"stop"}'}
                    )
                    while not store.read_json(f"{params['input_dir']}/control.json")[
                        "stopped"
                    ]:
                        await asyncio.sleep(0)
                    yield {
                        "type": "transcript",
                        "segment_index": 1,
                        "text": "Final budget is EUR 5 million.",
                        "final": True,
                    }
                    return
                if name == "voxbridge-record-intelligence":
                    self.calls.append((name, params))
                    text = store.read_json(params["transcript_path"])["text"]
                    assert text == "Initial phrase.\nFinal budget is EUR 5 million."
                    analysis_started.set()
                    await release_analysis.wait()
                    yield {"type": "execution", "execution_id": "analysis-execution"}
                    yield {
                        "type": "result",
                        "result": {
                            "kpis": [
                                {
                                    "name": "Budget",
                                    "value": "5 million",
                                    "unit": "EUR",
                                    "evidence": "Final budget is EUR 5 million.",
                                }
                            ]
                        },
                    }
                    return
                translation_started.set()
                await release_translation.wait()
                async for event in super().stream(name, params):
                    yield event

        gateway = Gateway(store)
        settings = Settings(data_dir=tmp_path, api_key="offline-test")
        manager = RecordIntelligence(settings, store, gateway)
        record = {
            "id": "a" * 32,
            "created_at": now_iso(),
            "latency": "fast",
            "source_language": "auto",
            "segments": [],
        }
        session = LiveSession(socket, record, settings, store, gateway, manager)
        session.index = 1
        session.input_available.set()
        live = asyncio.create_task(session.run())
        try:
            await asyncio.wait_for(analysis_started.wait(), 2)
            saved = store.session(record["id"])
            assert saved["transcript_saved_at"] and saved["status"] == "finishing"
            assert saved["finishing_phase"] == "translations"
            assert not release_translation.is_set()
            assert manager.start(session.record)["status"] == "running"
            assert (
                sum(
                    name == "voxbridge-record-intelligence" for name, _ in gateway.calls
                )
                == 1
            )
            release_translation.set()
            await asyncio.wait_for(live, 2)
            assert not release_analysis.is_set()
            assert any(event["type"] == "transcript_saved" for event in socket.events)
            saved = store.session(record["id"])
            assert saved["status"] == "completed"
            assert set(saved["stop_timings_ms"]) == {
                "transcription",
                "translations",
                "speech",
                "total",
            }
            assert all(segment["translations"] for segment in saved["segments"])
            release_analysis.set()
            await asyncio.wait_for(asyncio.gather(*manager.jobs.values()), 2)
            result = manager.read(record["id"])
            assert result["status"] == "completed"
            assert result["report"]["kpis"][0]["timeframe"] is None
            assert result["execution_id"] == "analysis-execution"
            assert store.session(record["id"]) == saved
        finally:
            live.cancel()
            await asyncio.gather(live, return_exceptions=True)
            await manager.close()

    asyncio.run(run())


def test_intelligence_api_history_retry_empty_and_restart(tmp_path):
    store = FileStore(tmp_path)
    settings = Settings(data_dir=tmp_path, api_key="offline-test")

    class Gateway(FakeGateway):
        fail = True
        title = "Budget"
        no_result = False
        invalid = False
        fail_after_result = False
        release = None

        async def stream(self, name, params):
            self.calls.append((name, params))
            assert name == "voxbridge-record-intelligence"
            if self.release is not None:
                while not self.release.is_set():
                    await asyncio.sleep(0.001)
            if self.fail:
                raise RuntimeError("secret-service-diagnostic")
            if self.no_result:
                return
            if self.invalid:
                yield {"type": "result", "result": {"topics": "invalid shape"}}
                return
            yield {
                "type": "result",
                "result": {
                    "title": self.title,
                    "kpis": [
                        {
                            "name": "Investment",
                            "value": "10",
                            "unit": "EUR",
                            "evidence": "EUR 10",
                        }
                    ],
                },
            }
            if self.fail_after_result:
                raise RuntimeError("stream failed after result")

    gateway = Gateway(store)
    identifier = "b" * 32
    record = {
        "id": identifier,
        "created_at": now_iso(),
        "status": "recording",
        "segments": [{"segment_index": 1, "text": "EUR 10", "final": True}],
    }
    store.write_json(f"sessions/{identifier}.json", record)

    def wait_status(client, expected):
        for _ in range(100):
            value = client.get(f"/api/sessions/{identifier}/intelligence").json()[
                "intelligence"
            ]
            if value["status"] == expected:
                return value
        raise AssertionError(f"Expected {expected}, got {value}")

    with TestClient(create_app(settings, gateway)) as client:
        assert client.get("/api/sessions/invalid/intelligence").status_code == 404
        assert (
            client.post(f"/api/sessions/{identifier}/intelligence").status_code == 409
        )
        assert (
            client.post(
                f"/api/sessions/{identifier}/intelligence?reanalyse=true"
            ).status_code
            == 409
        )
        record["status"] = "completed"
        store.write_json(f"sessions/{identifier}.json", record)
        assert (
            client.get("/api/sessions").json()["sessions"][0]["intelligence"]["status"]
            == "not_started"
        )
        row = client.get("/api/sessions").json()["sessions"][0]
        assert row["title"] == "EUR 10"
        assert (
            client.get(f"/api/sessions/{identifier}/intelligence").json()["session"][
                "title"
            ]
            == row["title"]
        )
        assert "Session " not in session_title(
            {"created_at": "2026-10-16T10:00:00Z", "segments": []}
        )
        long_text = "Économie européenne " * 30
        generated = session_title(
            {"segments": [{"segment_index": 0, "text": long_text}]}
        )
        assert generated.startswith("Économie européenne") and len(generated) <= 91
        assert not gateway.calls
        assert (
            client.post(f"/api/sessions/{identifier}/intelligence").status_code == 202
        )
        failed = wait_status(client, "failed")
        assert "secret" not in str(failed)
        gateway.fail = False
        assert (
            client.post(f"/api/sessions/{identifier}/intelligence").status_code == 202
        )
        completed = wait_status(client, "completed")
        assert completed["report"]["summary"] is None
        assert (
            client.get(f"/api/sessions/{identifier}/intelligence").json()["session"][
                "title"
            ]
            == "Budget"
        )
        assert completed["schema_version"] == 2
        calls = len(gateway.calls)
        client.post(f"/api/sessions/{identifier}/intelligence")
        client.post(f"/api/sessions/{identifier}/intelligence?reanalyse=false")
        assert len(gateway.calls) == calls
        gateway.title = "Updated budget"
        gateway.release = threading.Event()
        queued = client.post(
            f"/api/sessions/{identifier}/intelligence?reanalyse=true"
        ).json()
        assert queued["status"] == "queued" and queued["schema_version"] == 2
        assert queued["report"] == completed["report"]
        running = wait_status(client, "running")
        assert running["report"] == completed["report"]
        assert (
            client.post(
                f"/api/sessions/{identifier}/intelligence?reanalyse=true"
            ).json()["status"]
            == "running"
        )
        assert (
            client.post(f"/api/sessions/{identifier}/intelligence").json()["status"]
            == "running"
        )
        assert len(gateway.calls) == calls + 1
        gateway.release.set()
        completed = wait_status(client, "completed")
        assert completed["report"]["title"] == "Updated budget"
        assert completed["report"]["summary"] is None
        assert completed["report"]["kpis"][0]["timeframe"] is None
        gateway.release = None
        calls = len(gateway.calls)
        client.post(f"/api/sessions/{identifier}/intelligence")
        assert len(gateway.calls) == calls
        for failure in ("fail", "no_result", "invalid", "fail_after_result"):
            setattr(gateway, failure, True)
            response = client.post(
                f"/api/sessions/{identifier}/intelligence?reanalyse=true"
            )
            assert response.status_code == 202
            failed = wait_status(client, "failed")
            assert failed["report"] == completed["report"]
            assert failed["schema_version"] == 2
            assert "secret" not in str(failed)
            setattr(gateway, failure, False)
        client.post(f"/api/sessions/{identifier}/intelligence")
        completed = wait_status(client, "completed")
        calls = len(gateway.calls)
        assert store.session(identifier) == record
        empty = {**record, "id": "c" * 32, "segments": []}
        store.write_json(f"sessions/{empty['id']}.json", empty)
        assert (
            client.post(f"/api/sessions/{empty['id']}/intelligence").json()["status"]
            == "unavailable"
        )
        assert (
            client.post(
                f"/api/sessions/{empty['id']}/intelligence?reanalyse=true"
            ).json()["status"]
            == "unavailable"
        )
        assert len(gateway.calls) == calls
    store.write_json(
        "intelligence/" + "d" * 32 + ".json", {"status": "running", "report": None}
    )
    with TestClient(create_app(settings, gateway)) as client:
        assert (
            store.read_json("intelligence/" + "d" * 32 + ".json")["status"] == "failed"
        )
        assert wait_status(client, "completed")["report"] == completed["report"]
        assert len(client.get("/api/sessions").json()["sessions"]) == 2
        assert len(gateway.calls) == calls
