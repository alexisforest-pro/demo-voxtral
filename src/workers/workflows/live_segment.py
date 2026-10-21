"""Fan out four streaming translations of a supplied transcript segment."""

import asyncio

from mistralai import workflows

with workflows.workflow.unsafe.imports_passed_through():
    from workers.activities.translation import translate_text
    from workers.models import SegmentInput


@workflows.workflow.define(
    name="voxbridge-live-segment",
    workflow_display_name="VoxBridge · Translate segment",
    workflow_description="Four parallel Medium translations of a supplied transcript segment.",
)
class LiveSegmentWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, params: SegmentInput) -> dict:
        text = params.text
        if not text:
            return {"text": "", "translations": {}}
        translations = await asyncio.gather(
            *[
                translate_text(
                    text,
                    language,
                    params.segment_index,
                    params.previous_text,
                    params.translation_model,
                    params.source_language,
                )
                for language in ("de", "fr", "es", "it")
            ]
        )
        return {
            "text": text,
            "translations": {item["language"]: item["text"] for item in translations},
        }
