"""Separate speech workflow keeps TTS from blocking the next transcription."""

from mistralai import workflows

with workflows.workflow.unsafe.imports_passed_through():
    from workers.activities.speech import generate_speech
    from workers.models import SpeechInput


@workflows.workflow.define(
    name="voxbridge-spoken-translation",
    workflow_display_name="VoxBridge · Speak translation",
)
class SpokenTranslationWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, params: SpeechInput) -> dict:
        return await generate_speech(params)
