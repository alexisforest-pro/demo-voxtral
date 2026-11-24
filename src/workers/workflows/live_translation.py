"""Keep one translation workflow open per live session; segments arrive as job files."""

from mistralai import workflows

with workflows.workflow.unsafe.imports_passed_through():
    from workers.activities.translation import translate_live_jobs
    from workers.models import LiveTranslationInput


@workflows.workflow.define(
    name="voxbridge-live-translation",
    workflow_display_name="VoxBridge · Live translation",
    workflow_description="Session-long streaming translation of transcript segments into four languages.",
)
class LiveTranslationWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, params: LiveTranslationInput) -> dict:
        return await translate_live_jobs(params)
