"""Single-flight streamed speech with shared PCM chunks and a persistent WAV cache."""

import asyncio
import base64
import hashlib
import json
import logging
import wave
from dataclasses import dataclass
from uuid import uuid4

from backend.config import Settings
from backend.storage import FileStore


@dataclass
class PreviewJob:
    output: str
    stream_dir: str
    language: str = "en"
    max_audio_seconds: int = 120
    task: asyncio.Task | None = None
    error: str | None = None


class VoicePreviews:
    def __init__(self, settings: Settings, store: FileStore, gateway):
        self.settings = settings
        self.store = store
        self.gateway = gateway
        self.jobs: dict[tuple[str, str], PreviewJob] = {}

    def ready(self, output: str, max_seconds: int = 120) -> bool:
        try:
            with wave.open(str(self.store.path(output)), "rb") as audio:
                return (
                    audio.getframerate() == 24000
                    and audio.getnchannels() == 1
                    and audio.getsampwidth() == 2
                    and 0 < audio.getnframes() <= 24000 * max_seconds
                    and len(audio.readframes(audio.getnframes()))
                    == audio.getnframes() * 2
                )
        except (OSError, EOFError, wave.Error):
            return False

    def prepare(
        self,
        voice: dict,
        text: str,
        *,
        language: str = "en",
        directory: str = "voice-previews",
        route: str | None = None,
        max_seconds: int = 120,
    ) -> dict:
        key = hashlib.sha256(
            json.dumps(
                [
                    voice["remote_voice_id"],
                    self.settings.speech_model,
                    text,
                    "pcm24k-v2",
                    language,
                    self.settings.default_voice_id
                    if not voice["remote_voice_id"]
                    else None,
                ]
            ).encode()
        ).hexdigest()
        identifier = voice["id"]
        output = f"audio/output/{directory}/{identifier}/{key}.wav"
        if self.ready(output, max_seconds):
            return {
                "url": f"/api/audio/{output}",
                "voice_id": identifier,
                "cached": True,
            }
        job = self.jobs.get((identifier, key))
        if job is None or (job.task and job.task.done()):
            job = PreviewJob(
                output,
                f"audio/output/{directory}/{identifier}/streams/{uuid4().hex}",
                language=language,
                max_audio_seconds=max_seconds,
            )
            job.task = asyncio.create_task(self.generate(job, voice, text))
            self.jobs[(identifier, key)] = job
        route = route or f"/api/voices/{identifier}/preview"
        return {
            "stream_url": f"{route}/{key}/stream",
            "voice_id": identifier,
            "cached": False,
        }

    async def generate(self, job: PreviewJob, voice: dict, text: str):
        try:
            async with asyncio.timeout(self.settings.workflow_timeout):
                await self.gateway.execute(
                    "voxbridge-spoken-translation",
                    {
                        "text": text,
                        "language": job.language,
                        "segment_index": 0,
                        "output_path": job.output,
                        "stream_dir": job.stream_dir,
                        "model": self.settings.speech_model,
                        "remote_voice_id": voice["remote_voice_id"],
                        **(
                            {"max_audio_seconds": job.max_audio_seconds}
                            if job.max_audio_seconds != 120
                            else {}
                        ),
                    },
                )
            if not self.ready(job.output, job.max_audio_seconds):
                raise ValueError("Preview workflow did not produce a complete WAV.")
        except asyncio.CancelledError:
            job.error = "Preview interrupted. Click Read in English to retry."
            raise
        except Exception:
            logging.getLogger(__name__).exception("Voice preview workflow failed")
            job.error = (
                "Speech generation failed. Check worker logs and retry playback."
            )

    async def events(self, job: PreviewJob):
        index = 0
        try:
            async with asyncio.timeout(self.settings.workflow_timeout + 5):
                while True:
                    if job.error:
                        yield json.dumps({"type": "error", "message": job.error}) + "\n"
                        return
                    finished = job.task.done()
                    manifest = self.store.path(f"{job.stream_dir}/control.json")
                    count = (
                        self.store.read_json(f"{job.stream_dir}/control.json")["chunks"]
                        if manifest.is_file()
                        else 0
                    )
                    while index < count:
                        chunk = self.store.path(
                            f"{job.stream_dir}/{index:06d}.pcm"
                        ).read_bytes()
                        yield (
                            json.dumps(
                                {
                                    "type": "audio",
                                    "sample_rate": 24000,
                                    "audio_data": base64.b64encode(chunk).decode(),
                                }
                            )
                            + "\n"
                        )
                        index += 1
                    if finished:
                        if job.error:
                            continue
                        yield (
                            json.dumps(
                                {"type": "done", "url": f"/api/audio/{job.output}"}
                            )
                            + "\n"
                        )
                        return
                    await asyncio.sleep(0.05)
        except TimeoutError:
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "message": "Preview timed out. Click Read in English to retry.",
                    }
                )
                + "\n"
            )

    async def close(self):
        tasks = [
            job.task for job in self.jobs.values() if job.task and not job.task.done()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
