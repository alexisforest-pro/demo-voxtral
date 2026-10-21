"""Durable orchestration only; file access and inference stay in the activity."""

from mistralai import workflows

with workflows.workflow.unsafe.imports_passed_through():
    from workers.activities.intelligence import generate_intelligence
    from workers.models import IntelligenceInput


@workflows.workflow.define(
    name="voxbridge-record-intelligence",
    workflow_display_name="VoxBridge · Record intelligence",
)
class RecordIntelligenceWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, params: IntelligenceInput) -> dict:
        return await generate_intelligence(params)
