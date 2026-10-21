"""Durable voice lifecycle entrypoint."""

from mistralai import workflows

with workflows.workflow.unsafe.imports_passed_through():
    from workers.activities.voices import manage_voice
    from workers.models import VoiceInput


@workflows.workflow.define(
    name="voxbridge-voice-profile", workflow_display_name="VoxBridge · Voice profile"
)
class VoiceProfileWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, params: VoiceInput) -> dict:
        return await manage_voice(params)
