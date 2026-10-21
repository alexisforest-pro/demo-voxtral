"""The API only talks to Workflows. Streaming snapshots and JSON patches are relayed."""

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import jsonpatch
from mistralai.client import Mistral
from mistralai.client.models import WorkflowStreamError
from mistralai.extra.workflows import (
    WorkflowEncodingConfig,
    configure_workflow_encoding,
)

from backend.config import Settings


class WorkflowGateway:
    def __init__(self, settings: Settings):
        self.settings = settings

    @asynccontextmanager
    async def client(self):
        if not self.settings.api_key:
            raise ValueError(
                "Set MISTRAL_API_KEY in .env, then start the workflow worker."
            )
        async with Mistral(
            api_key=self.settings.api_key, server_url=self.settings.server_url
        ) as client:
            await configure_workflow_encoding(WorkflowEncodingConfig(), client=client)
            yield client

    async def execute(self, name: str, params: dict) -> dict:
        async with self.client() as client:
            async with asyncio.timeout(self.settings.workflow_timeout):
                result = await client.workflows.execute_workflow_and_wait_async(
                    workflow_identifier=name,
                    deployment_name=self.settings.deployment_name,
                    input=params,
                    polling_interval=1,
                    max_attempts=self.settings.workflow_timeout,
                )
                return unwrap_workflow_result(result)

    async def stream(self, name: str, params: dict) -> AsyncIterator[dict]:
        timeout = (
            self.settings.live_timeout + self.settings.workflow_timeout
            if name == "voxbridge-live-transcription"
            else self.settings.workflow_timeout
        )
        async with self.client() as client:
            async with asyncio.timeout(timeout):
                execution = await client.workflows.execute_workflow_async(
                    workflow_identifier=name,
                    deployment_name=self.settings.deployment_name,
                    input=params,
                )
                yield {"type": "execution", "execution_id": execution.execution_id}
                reducer = TaskStateReducer()
                next_sequence = 0
                completed = False
                completion_result = None
                for attempt in range(3):
                    try:
                        events = await client.workflows.events.get_stream_events_async(
                            workflow_exec_id=execution.execution_id,
                            start_seq=next_sequence,
                        )
                        async with events:
                            async for event in events:
                                payload = event.data
                                if payload is None:
                                    continue
                                if isinstance(payload, WorkflowStreamError):
                                    raise ConnectionError(
                                        f"Workflow event stream error: {payload.error} ({payload.reason})"
                                    )
                                next_sequence = payload.broker_sequence + 1
                                data = payload.data.model_dump(mode="json")
                                update = reducer.consume(data)
                                if update:
                                    yield update
                                event_type = data.get("event_type", "")
                                if event_type == "WORKFLOW_EXECUTION_COMPLETED":
                                    completed = True
                                    payload_result = data.get("attributes", {}).get(
                                        "result", {}
                                    )
                                    if payload_result.get("type") == "json":
                                        completion_result = unwrap_workflow_result(
                                            payload_result.get("value")
                                        )
                                    break
                                if event_type in (
                                    "WORKFLOW_EXECUTION_FAILED",
                                    "WORKFLOW_EXECUTION_CANCELED",
                                ):
                                    raise RuntimeError(
                                        "Workflow failed. Inspect its execution in Mistral Studio."
                                    )
                        if completed:
                            break
                        raise ConnectionError(
                            "Workflow event stream ended before completion."
                        )
                    except (ConnectionError, OSError, httpx.TransportError):
                        if attempt == 2:
                            raise
                        await asyncio.sleep(0.5 * 2**attempt)
                if completion_result is None:
                    completion_result = await self.result(execution.execution_id)
                yield {
                    "type": "result",
                    "result": completion_result,
                    "execution_id": execution.execution_id,
                }

    async def result(self, execution_id: str) -> dict:
        """Read an existing completed execution without starting new inference."""
        async with self.client() as client:
            execution = await client.workflows.executions.get_workflow_execution_async(
                execution_id=execution_id
            )
            if execution.status != "COMPLETED":
                raise ValueError("Workflow result is not complete.")
            return unwrap_workflow_result(execution.result)


def unwrap_workflow_result(result: dict) -> dict:
    """Dict-returning workflow entrypoints use a single result field on the API."""
    if isinstance(result, dict) and set(result) == {"result"}:
        return result["result"]
    return result


class TaskStateReducer:
    """Task updates use JSON Patch plus Mistral's string-append extension."""

    def __init__(self):
        self.states: dict[str, dict] = {}

    def consume(self, event: dict) -> dict | None:
        if event.get("event_type") not in (
            "CUSTOM_TASK_STARTED",
            "CUSTOM_TASK_IN_PROGRESS",
            "CUSTOM_TASK_COMPLETED",
        ):
            return None
        attributes = event.get("attributes", {})
        stream = attributes.get("custom_task_type")
        if stream not in ("transcript", "translation"):
            return None
        task_id = attributes["custom_task_id"]
        payload = attributes.get("payload", {})
        value = payload.get("value")
        if payload.get("type") == "json_patch":
            state = copy.deepcopy(self.states.get(task_id, {}))
            for operation in value:
                if operation["op"] == "append":
                    current = jsonpatch.JsonPointer(operation["path"]).resolve(state)
                    suffix = operation["value"]
                    if not isinstance(current, str) or not isinstance(suffix, str):
                        raise ValueError(
                            "Workflow append patches require string values."
                        )
                    operation = {
                        **operation,
                        "op": "replace",
                        "value": current + suffix,
                    }
                state = jsonpatch.apply_patch(state, [operation], in_place=False)
        elif payload.get("type") == "json" and isinstance(value, dict):
            state = copy.deepcopy(value)
        else:
            raise ValueError(
                "Unsupported workflow stream payload. Check payload encoding configuration."
            )
        self.states[task_id] = state
        return {"type": stream, **state}
