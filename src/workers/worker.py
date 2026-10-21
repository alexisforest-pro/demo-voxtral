"""Explicit registration makes the demo workflow list easy to inspect."""

import asyncio
import contextlib
import signal

from backend.config import Settings


def workflow_classes():
    from workers.workflows.live_segment import LiveSegmentWorkflow
    from workers.workflows.live_transcription import LiveTranscriptionWorkflow
    from workers.workflows.record_intelligence import RecordIntelligenceWorkflow
    from workers.workflows.spoken_translation import SpokenTranslationWorkflow
    from workers.workflows.voice_profile import VoiceProfileWorkflow

    return [
        LiveSegmentWorkflow,
        LiveTranscriptionWorkflow,
        VoiceProfileWorkflow,
        SpokenTranslationWorkflow,
        RecordIntelligenceWorkflow,
    ]


async def main() -> None:
    settings = Settings()
    if not settings.api_key:
        raise SystemExit("Set MISTRAL_API_KEY in .env before starting the worker.")
    from mistralai import workflows

    print(f"Registering VoxBridge workflows in deployment {settings.deployment_name}")
    task = asyncio.create_task(workflows.run_worker(workflow_classes()))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await task


if __name__ == "__main__":
    asyncio.run(main())
