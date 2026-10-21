"""Local demo HTTP entrypoint. Run with uv run uvicorn backend.app:app."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.config import (
    LANGUAGE_CODES,
    LANGUAGES,
    LATENCY_PRESETS,
    PROJECT_ROOT,
    Settings,
)
from backend.live_session import LiveSession
from backend.record_intelligence import RecordIntelligence, session_title
from backend.storage import FileStore, now_iso, read_wav
from backend.voice_preview import VoicePreviews
from backend.workflow_gateway import WorkflowGateway

logger = logging.getLogger(__name__)

# Fictional demonstration copy, not an official announcement.
VOICE_PREVIEW_TEXT = (
    "EU Countries have reached a unique agreement to deploy an exceptional budget "
    "to train “Le Gros Chaton” with Mistral AI. "
    "The three-year programme will coordinate open datasets, public demonstrations, "
    "and multilingual workshops across every member state."
)


class SessionInput(BaseModel):
    latency: Literal["fast", "balanced", "accurate"] = "balanced"
    source_language: Literal["auto", "en", "de", "fr", "es", "it"] = "auto"
    speech_language: Literal["de", "fr", "es", "it"] | None = None
    voice_id: str | None = None


class PlaybackInput(BaseModel):
    language: Literal["de", "fr", "es", "it"]
    voice_id: str | None = None


class RecordedSpeechInput(BaseModel):
    language: Literal["de", "fr", "es", "it"]
    segment_index: int = Field(ge=0)
    voice_id: str | None = None


def create_app(
    settings: Settings | None = None, gateway: WorkflowGateway | None = None
) -> FastAPI:
    settings = settings or Settings()
    store = FileStore(settings.data_dir)
    gateway = gateway or WorkflowGateway(settings)
    previews = VoicePreviews(settings, store, gateway)
    playback = VoicePreviews(settings, store, gateway)
    recovery_locks: dict[str, asyncio.Lock] = {}
    intelligence = RecordIntelligence(settings, store, gateway)

    @asynccontextmanager
    async def lifespan(_app):
        intelligence.recover_interrupted()
        try:
            yield
        finally:
            await intelligence.close()
            await previews.close()
            await playback.close()

    app = FastAPI(title="VoxBridge local prototype", lifespan=lifespan)
    active_sessions: set[str] = set()

    def get_voice(identifier: str):
        try:
            return store.voice(identifier)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "Voice not found.") from exc

    def get_session(identifier: str):
        try:
            return store.session(identifier)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "Session not found.") from exc

    async def execute(name: str, params: dict):
        try:
            return await gateway.execute(name, params)
        except Exception as exc:
            logger.exception("Workflow execution failed")
            raise HTTPException(
                502,
                "Workflow failed. Start the worker and check API permissions and worker logs.",
            ) from exc

    @app.get("/api/config")
    def config():
        return {
            "api_key_configured": bool(settings.api_key),
            "deployment_name": settings.deployment_name,
            "languages": LANGUAGES,
            "latency_presets": LATENCY_PRESETS,
            "voice_preview_text": VOICE_PREVIEW_TEXT,
            "models": {
                "transcription": settings.transcription_model,
                "translation": settings.translation_model,
                "speech": settings.speech_model,
            },
        }

    @app.get("/api/voices")
    def voices():
        return {"voices": store.list_records("voices")}

    @app.post("/api/voices", status_code=201)
    async def create_voice(
        name: Annotated[str, Form()],
        languages: Annotated[str, Form()],
        sample: Annotated[UploadFile, File()],
    ):
        if not name.strip() or len(name) > 80:
            raise HTTPException(422, "Use a voice name between 1 and 80 characters.")
        try:
            scopes = json.loads(languages)
            if (
                not isinstance(scopes, list)
                or not scopes
                or any(not isinstance(scope, str) for scope in scopes)
            ):
                raise ValueError("Select at least one language.")
            if set(scopes) != {"all"} and not set(scopes) <= LANGUAGE_CODES:
                raise ValueError("Unsupported language scope.")
            content = await sample.read(1_000_001)
            if len(content) > 1_000_000:
                raise ValueError("Voice sample is too large (30 seconds maximum).")
            read_wav(content, max_seconds=30, min_seconds=3)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        identifier = uuid4().hex
        sample_path = f"voices/{identifier}/sample.wav"
        store.write_bytes(sample_path, content)
        try:
            result = await execute(
                "voxbridge-voice-profile",
                {
                    "action": "create",
                    "name": name.strip(),
                    "sample_path": sample_path,
                    "languages": sorted(LANGUAGE_CODES)
                    if scopes == ["all"]
                    else scopes,
                },
            )
        except HTTPException:
            store.path(sample_path).unlink(missing_ok=True)
            raise
        record = {
            "id": identifier,
            "name": name.strip(),
            "languages": scopes,
            "created_at": now_iso(),
            "remote_voice_id": result["remote_voice_id"],
            "sample_path": sample_path,
            "sample_url": f"/api/voices/{identifier}/sample",
        }
        store.write_json(f"voices/{identifier}.json", record)
        return record

    @app.get("/api/voices/{identifier}/sample")
    def voice_sample(identifier: str):
        return FileResponse(
            store.path(get_voice(identifier)["sample_path"]), media_type="audio/wav"
        )

    @app.post("/api/voices/{identifier}/preview")
    async def preview_voice(identifier: str):
        return previews.prepare(get_voice(identifier), VOICE_PREVIEW_TEXT)

    @app.get("/api/voices/{identifier}/preview/{key}/stream")
    async def preview_stream(identifier: str, key: str):
        get_voice(identifier)
        job = previews.jobs.get((identifier, key))
        if job is None:
            raise HTTPException(404, "Preview not found. Click Read in English again.")
        return StreamingResponse(
            previews.events(job),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.delete("/api/voices/{identifier}", status_code=204)
    async def delete_voice(identifier: str):
        voice = get_voice(identifier)
        for session_id in active_sessions:
            if store.session(session_id).get("voice_id") == identifier:
                raise HTTPException(
                    409, "Turn off this voice in the active session before deleting it."
                )
        await execute(
            "voxbridge-voice-profile",
            {"action": "delete", "remote_voice_id": voice["remote_voice_id"]},
        )
        store.delete_voice(identifier)

    @app.post("/api/sessions", status_code=201)
    def create_session(params: SessionInput):
        if not settings.api_key:
            raise HTTPException(
                503, "Configure the API key in the server settings before recording."
            )
        if params.voice_id:
            voice = get_voice(params.voice_id)
            if not params.speech_language or not (
                {"all", params.speech_language} & set(voice["languages"])
            ):
                raise HTTPException(
                    422, "Voice must match the selected spoken language."
                )
        record = {
            "id": uuid4().hex,
            "created_at": now_iso(),
            "status": "ready",
            "segments": [],
            **params.model_dump(),
        }
        store.write_json(f"sessions/{record['id']}.json", record)
        return {"id": record["id"]}

    def session_metadata(record: dict, state: dict):
        return {
            **{k: v for k, v in record.items() if k != "segments"},
            "title": session_title(record, state.get("report")),
        }

    @app.get("/api/sessions")
    def sessions():
        rows = []
        for record in store.list_records("sessions"):
            state = intelligence.read(record["id"])
            rows.append(
                {
                    **session_metadata(record, state),
                    "intelligence": {
                        k: v for k, v in state.items() if k != "transcript_path"
                    },
                }
            )
        return {"sessions": rows}

    @app.delete("/api/sessions")
    async def delete_sessions():
        deleted, retained = [], []
        for record in store.list_records("sessions"):
            identifier = record["id"]
            lock = recovery_locks.get(identifier)
            busy_playback = any(
                key[0] == identifier and job.task and not job.task.done()
                for key, job in playback.jobs.items()
            )
            if (
                identifier in active_sessions
                or identifier in intelligence.jobs
                or (lock and lock.locked())
                or busy_playback
            ):
                retained.append(
                    {
                        "id": identifier,
                        "message": "Recording, analysis or playback is still processing. Retry after it finishes.",
                    }
                )
                continue
            try:
                store.delete_session(identifier)
            except (OSError, ValueError):
                logger.warning("Could not delete local session %s", identifier)
                retained.append(
                    {
                        "id": identifier,
                        "message": "Local record deletion failed. Check file permissions and retry.",
                    }
                )
                continue
            deleted.append(identifier)
            recovery_locks.pop(identifier, None)
            for key in [key for key in playback.jobs if key[0] == identifier]:
                playback.jobs.pop(key)
        return {"deleted": deleted, "retained": retained}

    @app.get("/api/sessions/{identifier}/intelligence")
    def session_intelligence(identifier: str):
        record = get_session(identifier)
        state = intelligence.read(identifier)
        return {
            "session": session_metadata(record, state),
            "intelligence": {k: v for k, v in state.items() if k != "transcript_path"},
        }

    @app.post("/api/sessions/{identifier}/intelligence", status_code=202)
    async def analyse_session(identifier: str, reanalyse: bool = False):
        record = get_session(identifier)
        if identifier in active_sessions or record["status"] not in (
            "completed",
            "failed",
            "disconnected",
        ):
            raise HTTPException(
                409, "Stop recording and wait for the transcript first."
            )
        return intelligence.start(record, reanalyse=reanalyse)

    @app.post("/api/sessions/{identifier}/playback")
    async def full_playback(identifier: str, params: PlaybackInput):
        record = get_session(identifier)
        if record["status"] not in ("completed", "failed", "disconnected"):
            raise HTTPException(
                409, "Stop recording and wait for final processing first."
            )
        remote_id = None
        if params.voice_id:
            voice = get_voice(params.voice_id)
            if not ({"all", params.language} & set(voice["languages"])):
                raise HTTPException(
                    422, "Voice must match the selected spoken language."
                )
            remote_id = voice["remote_voice_id"]
        async with recovery_locks.setdefault(identifier, asyncio.Lock()):
            record = get_session(identifier)
            segments = sorted(
                (item for item in record["segments"] if item.get("final")),
                key=lambda item: item["segment_index"],
            )
            if not segments:
                raise HTTPException(422, "No completed transcript is available.")
            changed = False
            for segment in segments:
                translations = segment.get("translations", {})
                if translations.get(params.language, "").strip() and not segment.get(
                    "translation_error"
                ):
                    continue
                execution_id = segment.get("execution_id")
                if execution_id:
                    try:
                        result = await gateway.result(execution_id)
                        recovered = result.get("translations", {})
                        if (
                            isinstance(recovered, dict)
                            and recovered.get(params.language, "").strip()
                        ):
                            segment.setdefault("translations", {}).update(recovered)
                            segment.pop("translation_error", None)
                            changed = True
                    except Exception:
                        logger.exception(
                            "Could not recover completed translations for %s",
                            execution_id,
                        )
            if changed:
                store.write_json(f"sessions/{identifier}.json", record)
            missing = [
                str(item["segment_index"] + 1)
                for item in segments
                if item.get("translation_error")
                or not item.get("translations", {}).get(params.language, "").strip()
            ]
            if missing:
                raise HTTPException(
                    409,
                    f"The full translation is not available for phrases {', '.join(missing)}. Playback was not started to avoid reading only an extract.",
                )
            text = " ".join(
                item["translations"][params.language].strip() for item in segments
            )
        if len(text.split()) > 5000:
            raise HTTPException(
                422,
                "Recorded playback supports up to 5,000 words. Export this session to split a longer transcript.",
            )
        return playback.prepare(
            {"id": identifier, "remote_voice_id": remote_id},
            text,
            language=params.language,
            directory="recorded-playback",
            route=f"/api/sessions/{identifier}/playback",
            max_seconds=1800,
        )

    @app.get("/api/sessions/{identifier}/playback/{key}/stream")
    async def full_playback_stream(identifier: str, key: str):
        get_session(identifier)
        job = playback.jobs.get((identifier, key))
        if job is None:
            raise HTTPException(404, "Recorded playback not found. Click Play again.")
        return StreamingResponse(
            playback.events(job),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/sessions/{identifier}/speech")
    async def recorded_speech(identifier: str, params: RecordedSpeechInput):
        record = get_session(identifier)
        if record["status"] not in ("completed", "failed", "disconnected"):
            raise HTTPException(
                409, "Stop recording and wait for final processing first."
            )
        segment = next(
            (
                item
                for item in record["segments"]
                if item["segment_index"] == params.segment_index
            ),
            None,
        )
        if not segment or not segment.get("final") or segment.get("translation_error"):
            raise HTTPException(422, "No completed translation for this phrase.")
        text = segment.get("translations", {}).get(params.language, "")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(422, "No completed translation for this language.")
        remote_id = None
        if params.voice_id:
            voice = get_voice(params.voice_id)
            if not ({"all", params.language} & set(voice["languages"])):
                raise HTTPException(
                    422, "Voice must match the selected spoken language."
                )
            remote_id = voice["remote_voice_id"]
        output = f"audio/output/{identifier}/replay-{uuid4().hex}.mp3"
        await execute(
            "voxbridge-spoken-translation",
            {
                "text": text,
                "language": params.language,
                "segment_index": params.segment_index,
                "output_path": output,
                "model": settings.speech_model,
                "remote_voice_id": remote_id,
            },
        )
        return {"url": f"/api/audio/{output}"}

    @app.get("/api/sessions/{identifier}/export")
    def export_session(identifier: str):
        get_session(identifier)
        return FileResponse(
            store.path(f"sessions/{identifier}.json"),
            media_type="application/json",
            filename=f"voxbridge-{identifier}.json",
        )

    @app.get("/api/audio/{relative:path}")
    def audio(relative: str):
        try:
            path = store.path(relative)
            if (
                not path.is_relative_to(store.path("audio/output"))
                or not path.is_file()
                or path.suffix not in (".mp3", ".wav")
            ):
                raise ValueError("Audio not found.")
        except ValueError as exc:
            raise HTTPException(404, "Audio not found.") from exc
        return FileResponse(
            path, media_type="audio/wav" if path.suffix == ".wav" else "audio/mpeg"
        )

    @app.websocket("/api/sessions/{identifier}/live")
    async def live(socket: WebSocket, identifier: str):
        # Reject cross-origin browser connections to this unauthenticated localhost demo.
        origin = socket.headers.get("origin")
        if origin and origin not in (
            f"http://{socket.headers.get('host')}",
            f"https://{socket.headers.get('host')}",
        ):
            await socket.close(code=1008)
            return
        try:
            record = store.session(identifier)
        except (ValueError, FileNotFoundError):
            await socket.close(code=1008)
            return
        if identifier in active_sessions or record["status"] != "ready":
            await socket.close(code=1008)
            return
        active_sessions.add(identifier)
        try:
            await socket.accept()
            await LiveSession(
                socket, record, settings, store, gateway, intelligence
            ).run()
        finally:
            active_sessions.discard(identifier)

    app.mount(
        "/",
        StaticFiles(directory=PROJECT_ROOT / "frontend", html=True),
        name="frontend",
    )
    return app


app = create_app()
