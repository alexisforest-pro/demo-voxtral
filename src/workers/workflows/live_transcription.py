"""Keep a single Voxtral realtime connection open for a shared-file recording."""

from mistralai import workflows

with workflows.workflow.unsafe.imports_passed_through():
    from workers.activities.transcription import transcribe_live_audio
    from workers.models import LiveTranscriptionInput


@workflows.workflow.define(
    name="voxbridge-live-transcription",
    workflow_display_name="VoxBridge · Live transcription",
    workflow_description="Persistent Voxtral STT with independent transcript batching.",
)
class LiveTranscriptionWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, params: LiveTranscriptionInput) -> dict:
        return {"text": await transcribe_live_audio(params)}
