"""Persisted session intelligence, independent of translation and speech draining."""

import asyncio
import logging
import re
from uuid import uuid4

from backend.config import Settings
from backend.storage import FileStore, now_iso
from backend.workflow_gateway import WorkflowGateway
from workers.intelligence_schema import validate_report

logger = logging.getLogger(__name__)


def source_text(record: dict) -> str:
    return "\n".join(
        item.get("text", "").strip()
        for item in sorted(
            record.get("segments", []), key=lambda item: item["segment_index"]
        )
        if item.get("text", "").strip()
    )


def session_title(record: dict, report: dict | None = None) -> str:
    """Use the existing AI title, or a bounded source excerpt without new inference."""
    title = (report or {}).get("title")
    if isinstance(title, str) and title.strip():
        return " ".join(title.split())[:200]
    text = " ".join(source_text(record).split())
    if text:
        sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
        words = sentence.split()
        excerpt = " ".join(words[:12])
        if len(excerpt) > 90:
            excerpt = excerpt[:90].rsplit(" ", 1)[0] or excerpt[:90]
        return excerpt + ("…" if excerpt != sentence else "")
    created = record.get("created_at", "")
    return (
        f"Recording · {created[:16].replace('T', ' ')} UTC"
        if created
        else "Untitled recording"
    )


class RecordIntelligence:
    def __init__(self, settings: Settings, store: FileStore, gateway: WorkflowGateway):
        self.settings, self.store, self.gateway = settings, store, gateway
        self.jobs: dict[str, asyncio.Task] = {}

    def read(self, identifier: str) -> dict:
        self.store.validate_id(identifier)
        try:
            return self.store.read_json(f"intelligence/{identifier}.json")
        except FileNotFoundError:
            return {"schema_version": 2, "status": "not_started", "report": None}

    def save(self, identifier: str, value: dict):
        value["schema_version"] = 2
        self.store.write_json(f"intelligence/{identifier}.json", value)

    def waiting(self, record: dict):
        if self.read(record["id"])["status"] == "not_started":
            self.save(
                record["id"],
                {"status": "waiting", "requested_at": now_iso(), "report": None},
            )

    def start(self, record: dict, *, reanalyse: bool = False) -> dict:
        identifier = record["id"]
        existing = self.read(identifier)
        if (
            identifier in self.jobs
            or existing["status"] == "unavailable"
            or (existing["status"] == "completed" and not reanalyse)
        ):
            return existing
        text = source_text(record)
        value = {
            "status": "queued" if text else "unavailable",
            "requested_at": now_iso()
            if reanalyse
            else existing.get("requested_at", now_iso()),
            "transcript_complete": bool(record.get("transcript_saved_at"))
            or (
                record.get("status") == "completed"
                and all(item.get("final") for item in record.get("segments", []))
            ),
            "report": existing.get("report"),
        }
        if not text:
            value["message"] = (
                "No transcript is available. All extracted information is N/A."
            )
            self.save(identifier, value)
            return value
        snapshot = f"intelligence/{identifier}/{uuid4().hex}.json"
        self.store.write_json(snapshot, {"text": text})
        value["transcript_path"] = snapshot
        self.save(identifier, value)
        self.jobs[identifier] = asyncio.create_task(
            self.generate(identifier, text, value)
        )
        return value

    async def generate(self, identifier: str, text: str, value: dict):
        value = {**value, "status": "running", "started_at": now_iso()}
        replacement = None
        try:
            self.save(identifier, value)
            async for event in self.gateway.stream(
                "voxbridge-record-intelligence",
                {
                    "transcript_path": value["transcript_path"],
                    "model": self.settings.intelligence_model,
                },
            ):
                if event["type"] == "execution":
                    value["execution_id"] = event["execution_id"]
                    self.save(identifier, value)
                elif event["type"] == "result":
                    replacement = validate_report(event["result"], text)
            if replacement is None:
                raise ValueError("Intelligence workflow returned no report.")
            value.update(status="completed", report=replacement)
        except asyncio.CancelledError:
            value.update(
                status="failed",
                message="Analysis interrupted by backend shutdown. Retry to analyse the saved transcript.",
            )
            raise
        except Exception:
            logger.exception(
                "Record intelligence failed for %s", identifier, exc_info=False
            )
            value.update(
                status="failed",
                message="Analysis failed. Check the worker and model access, then retry. Missing information is N/A.",
            )
        finally:
            value["finished_at"] = now_iso()
            try:
                self.save(identifier, value)
            finally:
                self.jobs.pop(identifier, None)

    def recover_interrupted(self):
        for path in self.store.path("intelligence").glob("*.json"):
            value = self.store.read_json(str(path.relative_to(self.store.root)))
            if value.get("status") in ("waiting", "queued", "running"):
                value.update(
                    status="failed",
                    message="Analysis was interrupted. Retry to analyse the saved transcript.",
                )
                self.save(path.stem, value)

    async def close(self):
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
