"""Exercise the real gateway with installed SDK SSE models, not flattened events."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from mistralai.client.models.get_stream_events_v1_workflows_events_stream_getop import (
    GetStreamEventsV1WorkflowsEventsStreamGetResponseBody as SseEvent,
)

from backend.app import create_app
from backend.config import Settings
from backend.storage import FileStore
from backend.tests.test_app import wav
from backend.workflow_gateway import TaskStateReducer, WorkflowGateway

EXECUTION_ID = "test-execution"
TRANSLATIONS = {
    "de": "Hallo Europa",
    "fr": "Bonjour Europe",
    "es": "Hola Europa",
    "it": "Ciao Europa",
}
RESULT = {"text": "Hello Europe", "translations": TRANSLATIONS}


def sdk_event(sequence, event_type, attributes):
    return SseEvent.model_validate(
        {
            "event": "message",
            "id": str(sequence),
            "data": {
                "stream": "translation",
                "broker_sequence": sequence,
                "workflow_context": {
                    "namespace": "test",
                    "workflow_name": "voxbridge-live-segment",
                    "workflow_exec_id": EXECUTION_ID,
                },
                "data": {
                    "event_type": event_type,
                    "event_id": f"event-{sequence}",
                    "event_timestamp": 0,
                    "root_workflow_exec_id": EXECUTION_ID,
                    "parent_workflow_exec_id": None,
                    "continued_run_id": None,
                    "first_execution_run_id": None,
                    "chain_run_id": None,
                    "schedule_id": None,
                    "workflow_exec_id": EXECUTION_ID,
                    "workflow_run_id": "test-run",
                    "workflow_name": "voxbridge-live-segment",
                    "attributes": attributes,
                },
            },
        }
    )


def task_event(sequence, topic, task_id, state, *, patch=False):
    return sdk_event(
        sequence,
        "CUSTOM_TASK_IN_PROGRESS" if patch else "CUSTOM_TASK_STARTED",
        {
            "custom_task_id": task_id,
            "custom_task_type": topic,
            "payload": {"type": "json_patch" if patch else "json", "value": state},
        },
    )


def completed_event(sequence=99):
    return sdk_event(
        sequence,
        "WORKFLOW_EXECUTION_COMPLETED",
        {"task_id": "task", "result": {"type": "json", "value": {"result": RESULT}}},
    )


def stream_events():
    events = [
        SseEvent(),
        task_event(
            10,
            "transcript",
            "source",
            {"text": "Hello", "segment_index": 0, "final": False},
        ),
    ]
    events.append(
        task_event(
            11,
            "transcript",
            "source",
            {"text": "Hello Europe", "segment_index": 0, "final": True},
        )
    )
    for index, (language, text) in enumerate(TRANSLATIONS.items()):
        events.append(
            task_event(
                12 + index,
                "translation",
                language,
                {
                    "text": text,
                    "language": language,
                    "segment_index": 0,
                    "final": False,
                },
            )
        )
    events.append(
        task_event(
            20,
            "translation",
            "fr",
            [{"op": "append", "path": "/text", "value": "!"}],
            patch=True,
        )
    )
    events.append(completed_event())
    return events


class EventStream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def __aiter__(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


@pytest.fixture
def sdk_gateway(tmp_path, monkeypatch):
    settings = Settings(api_key="offline-test", data_dir=tmp_path)
    gateway = WorkflowGateway(settings)
    events = EventStream(stream_events())
    client = SimpleNamespace(
        workflows=SimpleNamespace(
            execute_workflow_async=AsyncMock(
                return_value=SimpleNamespace(execution_id=EXECUTION_ID)
            ),
            execute_workflow_and_wait_async=AsyncMock(
                return_value={"result": {"remote_voice_id": "remote-voice"}}
            ),
            events=SimpleNamespace(
                get_stream_events_async=AsyncMock(return_value=events)
            ),
            executions=SimpleNamespace(
                get_workflow_execution_async=AsyncMock(
                    return_value=SimpleNamespace(result={"result": RESULT})
                )
            ),
        )
    )

    @asynccontextmanager
    async def use_client():
        yield client

    monkeypatch.setattr(gateway, "client", use_client)
    return gateway, client, events


def collect(gateway):
    async def run():
        return [event async for event in gateway.stream("voxbridge-live-segment", {})]

    return asyncio.run(run())


def test_append_patch_applies_in_order_without_mutating_previous_state():
    reducer = TaskStateReducer()
    initial = task_event(1, "translation", "fr", {"text": "", "a/b~c": "value"})
    reducer.consume(initial.data.data.model_dump(mode="json"))
    previous = reducer.states["fr"]
    operations = [
        {"op": "append", "path": "/text", "value": "Bonjour"},
        {"op": "append", "path": "/text", "value": " Europe"},
        {"op": "append", "path": "/a~1b~0c", "value": "!"},
        {"op": "add", "path": "/final", "value": True},
    ]
    event = task_event(2, "translation", "fr", operations, patch=True)
    result = reducer.consume(event.data.data.model_dump(mode="json"))
    assert result["text"] == "Bonjour Europe"
    assert result["a/b~c"] == "value!"
    assert result["final"] is True
    assert previous["text"] == ""
    assert operations[0]["op"] == "append"


@pytest.mark.parametrize(
    "error",
    [
        SseEvent.model_validate(
            {"event": "error", "data": {"error": "tail_failed", "reason": "retry"}}
        ),
        httpx.RemoteProtocolError("Connection dropped"),
    ],
)
def test_resume_nested_sequence_without_restarting_workflow(
    sdk_gateway, error, monkeypatch
):
    original_sleep = asyncio.sleep
    delays = []

    async def skip_backoff(seconds):
        delays.append(seconds)
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", skip_backoff)
    gateway, client, _ = sdk_gateway
    first = EventStream(
        [
            task_event(
                41,
                "transcript",
                "source",
                {"text": "Hello", "segment_index": 0, "final": False},
            ),
            error,
        ]
    )
    second = EventStream(
        [
            task_event(
                42,
                "transcript",
                "source",
                [{"op": "append", "path": "/text", "value": " Europe"}],
                patch=True,
            ),
            completed_event(43),
        ]
    )
    client.workflows.events.get_stream_events_async.side_effect = [first, second]
    events = collect(gateway)
    assert [event["text"] for event in events if event["type"] == "transcript"] == [
        "Hello",
        "Hello Europe",
    ]
    calls = client.workflows.events.get_stream_events_async.await_args_list
    assert [call.kwargs["start_seq"] for call in calls] == [0, 42]
    client.workflows.execute_workflow_async.assert_awaited_once()
    assert first.closed and second.closed
    assert delays == [0.5]


def test_unwrap_voice_and_speech_workflow_outputs(sdk_gateway):
    gateway, client, _ = sdk_gateway
    result = asyncio.run(gateway.execute("voxbridge-voice-profile", {}))
    assert result == {"remote_voice_id": "remote-voice"}
    client.workflows.execute_workflow_and_wait_async.return_value = {
        "result": {"audio_path": "audio/output/test.mp3"}
    }
    assert asyncio.run(gateway.execute("voxbridge-spoken-translation", {})) == {
        "audio_path": "audio/output/test.mp3"
    }


def test_real_gateway_streams_to_websocket_and_routes_speech(sdk_gateway):
    gateway, client, _ = sdk_gateway
    client.workflows.executions.get_workflow_execution_async.return_value = (
        SimpleNamespace(result={"result": {}})
    )
    store = FileStore(gateway.settings.data_dir)

    async def speech_result(**kwargs):
        params = kwargs["input"]
        store.write_bytes(params["output_path"], b"test-audio")
        return {"result": {"audio_path": params["output_path"]}}

    client.workflows.execute_workflow_and_wait_async.side_effect = speech_result
    recorded = stream_events()
    client.workflows.events.get_stream_events_async.side_effect = [
        EventStream([recorded[1], recorded[2], completed_event()]),
        EventStream(recorded[3:]),
        EventStream(
            [
                sdk_event(
                    100,
                    "WORKFLOW_EXECUTION_COMPLETED",
                    {
                        "task_id": "analysis",
                        "result": {
                            "type": "json",
                            "value": {"result": {"title": "European greeting"}},
                        },
                    },
                )
            ]
        ),
    ]
    with TestClient(create_app(gateway.settings, gateway)) as app:
        identifier = app.post("/api/sessions", json={"speech_language": "fr"}).json()[
            "id"
        ]
        with app.websocket_connect(f"/api/sessions/{identifier}/live") as socket:
            socket.send_bytes(wav(1))
            socket.send_json({"type": "stop"})
            events = []
            for _ in range(30):
                event = socket.receive_json()
                events.append(event)
                if event["type"] == "stopped":
                    break
        assert events[-1]["type"] == "stopped"
        assert not [event for event in events if event["type"] == "error"]
        assert {
            event["language"] for event in events if event["type"] == "translation"
        } == set(TRANSLATIONS)
        audio = next(event for event in events if event["type"] == "audio")
        assert app.get(audio["url"]).content == b"test-audio"
        assert (
            app.get(f"/api/sessions/{identifier}/export").json()["status"]
            == "completed"
        )
        client.workflows.executions.get_workflow_execution_async.assert_not_awaited()
        segment = store.session(identifier)["segments"][0]
        assert segment["text"] == RESULT["text"]
        assert segment["translations"] == TRANSLATIONS
        speech = client.workflows.execute_workflow_and_wait_async.await_args.kwargs
        assert speech["workflow_identifier"] == "voxbridge-spoken-translation"
        assert speech["input"]["text"] == TRANSLATIONS["fr"]
