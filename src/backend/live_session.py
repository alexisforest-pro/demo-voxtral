"""Continuous workflow-backed STT with independent translation and speech queues."""

import asyncio
import contextlib
import json
import logging
from time import monotonic
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from backend.config import LATENCY_PRESETS, Settings
from backend.storage import FileStore, now_iso, read_wav
from backend.workflow_gateway import WorkflowGateway

logger = logging.getLogger(__name__)
MAX_PENDING = 8
TRANSLATION_CONTEXT_CHARS = 600
TRANSLATION_WORKFLOW = "voxbridge-live-translation"
TRANSLATION_FAILED = (
    "Translation workflow failed; inspect worker logs and execution in Studio."
)


class LiveSession:
    def __init__(
        self,
        socket: WebSocket,
        record: dict,
        settings: Settings,
        store: FileStore,
        gateway: WorkflowGateway,
        intelligence=None,
    ):
        self.socket, self.record, self.settings, self.store, self.gateway = (
            socket,
            record,
            settings,
            store,
            gateway,
        )
        self.intelligence = intelligence
        self.stop_requested = False
        self.stop_clock = None
        self.speech_queue = asyncio.Queue(maxsize=MAX_PENDING)
        self.send_lock = asyncio.Lock()
        self.input_available = asyncio.Event()
        self.speech_language = record.get("speech_language")
        self.voice_id = record.get("voice_id")
        self.selection_version = 0
        self.index = 0
        self.previous_text = ""
        # Completed-sentence translations, and provisional fragments of the open one.
        self.committed_translations: dict[str, str] = {}
        self.provisional_translations: dict[str, str] = {}
        self.contexts: dict[int, str] = {}
        self.open_fragments: list[dict] = []
        self.waiting_fragments: list[dict] = []
        self.fragment_job: int | None = None
        # One session-long translation workflow consumes job files from this folder.
        self.translation_dir = f"translation/{self.record['id']}"
        self.translation_started = False
        self.translation_stopped = False
        self.translation_failed = False
        self.translations_done = asyncio.Event()
        self.jobs: dict[int, dict] = {}
        self.speech_order: list[int] = []
        self.input_dir = f"audio/input/{self.record['id']}"
        self.input_stopped = False
        self.committed: set[int] = set()
        self.segments: dict[int, dict] = {}

    async def send(self, event: dict):
        async with self.send_lock:
            await self.socket.send_json(event)

    def save(self):
        self.store.write_json(f"sessions/{self.record['id']}.json", self.record)

    def publish_input(self):
        self.store.write_json(
            f"{self.input_dir}/control.json",
            {
                "count": self.index,
                "stopped": self.input_stopped,
                "updated_at": now_iso(),
            },
        )

    def publish_translations(self):
        self.store.write_json(
            f"{self.translation_dir}/control.json",
            {
                "count": len(self.jobs),
                "stopped": self.translation_stopped,
                "updated_at": now_iso(),
            },
        )

    def pending_jobs(self) -> int:
        return sum(not job["done"] for job in self.jobs.values())

    async def heartbeat(self):
        while True:
            self.publish_input()
            if self.translation_started:
                self.publish_translations()
            await asyncio.sleep(1)

    def configure(self, language: str | None, voice_id: str | None):
        if language not in (None, "de", "fr", "es", "it"):
            raise ValueError("Choose only one supported spoken language.")
        if voice_id:
            voice = self.store.voice(voice_id)
            if language is None or not ({"all", language} & set(voice["languages"])):
                raise ValueError("This voice is not assigned to the selected language.")
        self.speech_language, self.voice_id = language, voice_id
        self.selection_version += 1
        self.record.update(speech_language=language, voice_id=voice_id)
        self.save()

    async def receive(self):
        while True:
            message = await self.socket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            if message.get("bytes") is not None:
                content = message["bytes"]
                if len(content) > 96_044:
                    raise ValueError("Audio chunk is too large; maximum is 3 seconds.")
                read_wav(content, max_seconds=3, min_seconds=1 / 16000)
                # Keep silence: the persistent model stream needs continuous audio.
                path = f"{self.input_dir}/{self.index:05d}.wav"
                self.store.write_bytes(path, content)
                self.index += 1
                self.publish_input()
                self.input_available.set()
            elif message.get("text"):
                data = json.loads(message["text"])
                if data.get("type") == "stop":
                    self.stop_requested = True
                    self.stop_clock = monotonic()
                    self.record["stop_requested_at"] = now_iso()
                    self.input_stopped = True
                    self.publish_input()
                    self.input_available.set()
                    if self.intelligence:
                        self.intelligence.waiting(self.record)
                    return
                if data.get("type") == "configure":
                    try:
                        self.configure(
                            data.get("speech_language"), data.get("voice_id")
                        )
                    except (ValueError, FileNotFoundError) as exc:
                        await self.send({"type": "error", "message": str(exc)})

    async def accept_transcript(self, event: dict):
        index = event["segment_index"]
        text = event.get("text", "")
        if not text.strip() or index in self.committed:
            return
        segment = self.segments.get(index)
        if segment is None:
            segment = {"segment_index": index, "translations": {}}
            self.segments[index] = segment
            self.record["segments"].append(segment)
            self.record["segments"].sort(key=lambda item: item["segment_index"])
        segment["text"] = text
        await self.send(event)
        if event.get("final"):
            self.committed.add(index)
            segment["final"] = True
            self.contexts[index] = self.previous_text[-2000:]
            self.previous_text = (self.previous_text + " " + text)[-2000:]
            if event.get("sentence_end", True) is False:
                # Provisional: shown live, then replaced by the whole-sentence translation.
                self.open_fragments.append(segment)
                self.waiting_fragments.append(segment)
                self.dispatch_fragment()
            else:
                fragments, self.open_fragments = self.open_fragments, []
                # Fragments still waiting are superseded by the whole sentence below.
                for item in self.waiting_fragments:
                    item["translation_skipped"] = True
                self.waiting_fragments = []
                self.submit(
                    segment,
                    " ".join([*(item["text"] for item in fragments), text]),
                    self.contexts[
                        fragments[0]["segment_index"] if fragments else index
                    ],
                    fragment=False,
                    revises=[item["segment_index"] for item in fragments],
                )
            self.save()

    def dispatch_fragment(self):
        """Send the next waiting fragment once the previous one has finished.

        Each fragment continues the previous fragment translation, so fragments of
        the same sentence run one at a time; whole sentences never wait.
        """
        if self.fragment_job is not None or not self.waiting_fragments:
            return
        segment = self.waiting_fragments.pop(0)
        index = segment["segment_index"]
        self.fragment_job = self.submit(
            segment, segment["text"], self.contexts[index], fragment=True, revises=[]
        )

    def submit(
        self, segment: dict, text: str, context: str, *, fragment: bool, revises: list
    ) -> int | None:
        if self.translation_failed:
            segment["translation_error"] = TRANSLATION_FAILED
            return None
        if self.pending_jobs() >= MAX_PENDING:
            segment["translation_error"] = (
                "Translation backlog is full; phrase kept as original text."
            )
            asyncio.get_running_loop().create_task(
                self.send({"type": "error", "message": segment["translation_error"]})
            )
            return None
        job_id = len(self.jobs)
        self.store.write_json(
            f"{self.translation_dir}/{job_id:05d}.json",
            {
                "job_id": job_id,
                "segment_index": segment["segment_index"],
                "text": text,
                "previous_text": context,
                "previous_translations": self.translation_context(fragment),
                "partial_sentence": fragment,
            },
        )
        self.jobs[job_id] = {
            "segment": segment,
            "fragment": fragment,
            "revises": revises,
            "done": False,
        }
        segment["translation_job"] = job_id
        if not fragment:
            self.speech_order.append(segment["segment_index"])
            # The sentence ended: its fragment drafts stop being context.
            self.provisional_translations = {}
        self.publish_translations()
        return job_id

    async def process_transcript(self):
        await self.input_available.wait()
        if not self.index:
            return
        preset = next(p for p in LATENCY_PRESETS if p["id"] == self.record["latency"])
        params = {
            "input_dir": self.input_dir,
            "target_delay_ms": preset["target_delay_ms"],
            "transcription_model": self.settings.transcription_model,
            "max_seconds": self.settings.live_timeout,
        }
        try:
            async for event in self.gateway.stream(
                "voxbridge-live-transcription", params
            ):
                if event["type"] == "execution":
                    self.record["execution_id"] = event["execution_id"]
                    await self.send({**event, "stage": "transcription"})
                elif event["type"] == "transcript":
                    await self.accept_transcript(event)
        except WebSocketDisconnect:
            raise
        except Exception as exc:
            try:
                diagnostic = self.store.read_json(
                    f"{self.input_dir}/workflow_error.json"
                )
            except (FileNotFoundError, ValueError, OSError):
                diagnostic = {}
            if not isinstance(diagnostic, dict):
                diagnostic = {}
            message = diagnostic.get("message")
            if not isinstance(message, str) or not message.strip():
                message = "Live transcription failed. Check the worker logs for the speech recognition error."
            execution_id = self.record.get("execution_id")
            if execution_id:
                message += f" Workflow execution: {execution_id}."
            self.record["transcription_error"] = diagnostic
            raise RuntimeError(message) from exc

    def translation_context(self, fragment: bool) -> dict[str, str]:
        languages = set(self.committed_translations) | set(
            self.provisional_translations
        )
        return {
            language: " ".join(
                (
                    self.committed_translations.get(language, "")
                    + " "
                    + (
                        self.provisional_translations.get(language, "")
                        if fragment
                        else ""
                    )
                ).split()
            )[-TRANSLATION_CONTEXT_CHARS:]
            for language in languages
        }

    def remember_translations(self, completed: dict, segment: dict, fragment: bool):
        if fragment and segment not in self.open_fragments:
            return  # Its sentence already ended; this draft is no longer context.
        target = (
            self.provisional_translations if fragment else self.committed_translations
        )
        for language, text in completed.items():
            if isinstance(text, str) and text.strip():
                target[language] = (target.get(language, "") + " " + text.strip())[
                    -TRANSLATION_CONTEXT_CHARS:
                ]

    async def process_translations(self):
        """Run one translation workflow for the whole session and relay its events.

        It starts with the first audio chunk, so workflow start-up and the model
        warm-up overlap with speech instead of delaying the first phrase.
        """
        await self.input_available.wait()
        if not self.index:
            self.translations_done.set()
            return
        self.translation_started = True
        self.publish_translations()
        params = {
            "jobs_dir": self.translation_dir,
            "translation_model": self.settings.translation_model,
            "source_language": self.record.get("source_language", "auto"),
            "max_seconds": self.settings.live_timeout,
        }
        try:
            async for event in self.gateway.stream(TRANSLATION_WORKFLOW, params):
                if event["type"] == "execution":
                    self.record["translation_execution_id"] = event["execution_id"]
                    await self.send({**event, "stage": "translation"})
                elif event["type"] == "translation":
                    await self.relay_partial(event)
                elif event["type"] == "translation_result":
                    await self.complete_job(event)
        except WebSocketDisconnect:
            raise
        except Exception:
            logger.exception("Live translation workflow failed")
            self.translation_failed = True
            for job in self.jobs.values():
                if not job["done"]:
                    job["done"] = True
                    job["segment"]["translation_error"] = TRANSLATION_FAILED
            self.save()
            await self.send({"type": "error", "message": TRANSLATION_FAILED})
        finally:
            self.translations_done.set()

    async def relay_partial(self, event: dict):
        job = self.jobs.get(event.get("job_id"))
        if job is None or job["done"] or event.get("final"):
            return
        segment = job["segment"]
        if "merged_into" in segment:
            return
        await self.send(
            {
                "type": "translation",
                "language": event["language"],
                "text": event["text"],
                "segment_index": segment["segment_index"],
                "final": False,
                **({"replaces": job["revises"]} if job["revises"] else {}),
            }
        )

    async def complete_job(self, event: dict):
        job = self.jobs.get(event.get("job_id"))
        if job is None or job["done"]:
            return
        job["done"] = True
        segment, fragment = job["segment"], job["fragment"]
        index = segment["segment_index"]
        completed = event.get("translations")
        completed = completed if isinstance(completed, dict) else {}
        revises = [i for i in job["revises"] if i in self.segments]
        superseded = "merged_into" in segment
        if not superseded:
            segment["translations"].update(completed)
            if event.get("error") or not completed:
                segment["translation_error"] = TRANSLATION_FAILED
                await self.send({"type": "error", "message": TRANSLATION_FAILED})
            else:
                segment.pop("translation_error", None)
        self.remember_translations(completed, segment, fragment)
        for revised in revises:
            # The whole-sentence translation now covers this fragment.
            self.segments[revised]["translations"] = {}
            self.segments[revised]["merged_into"] = index
            self.segments[revised].pop("translation_error", None)
        for language, text in ({} if superseded else segment["translations"]).items():
            await self.send(
                {
                    "type": "translation",
                    "language": language,
                    "text": text,
                    "segment_index": index,
                    "final": True,
                    **({"replaces": revises} if revises else {}),
                }
            )
        if fragment:
            self.fragment_job = None
            self.dispatch_fragment()
        await self.queue_ready_speech()
        self.save()
        await self.send({"type": "queue", "pending": self.pending_jobs()})

    async def queue_ready_speech(self):
        """Speak whole sentences in spoken order even when translations finish early."""
        while self.speech_order:
            segment = self.segments[self.speech_order[0]]
            job = self.jobs.get(segment.get("translation_job"))
            if job is not None and not job["done"]:
                return
            index = self.speech_order.pop(0)
            language = self.speech_language
            text = segment["translations"].get(language, "") if language else ""
            if not text:
                continue
            if self.speech_queue.full():
                await self.send(
                    {
                        "type": "error",
                        "message": "Speech backlog is full; this phrase will remain text-only.",
                    }
                )
                continue
            await self.speech_queue.put(
                (index, language, text, self.voice_id, self.selection_version)
            )

    async def drain_translations(self):
        """After Stop: flush waiting fragments, end the job feed, await the workflow."""
        while self.waiting_fragments or self.fragment_job is not None:
            if self.translations_done.is_set():
                break
            await asyncio.sleep(0.05)
        self.translation_stopped = True
        if self.translation_started:
            self.publish_translations()
        await self.translations_done.wait()

    async def process_speech(self):
        while True:
            index, language, text, voice_id, version = await self.speech_queue.get()
            try:
                if version != self.selection_version:
                    continue
                remote_id = (
                    self.store.voice(voice_id)["remote_voice_id"] if voice_id else None
                )
                output = (
                    f"audio/output/{self.record['id']}/{index:05d}-{uuid4().hex}.mp3"
                )
                result = await self.gateway.execute(
                    "voxbridge-spoken-translation",
                    {
                        "text": text,
                        "language": language,
                        "segment_index": index,
                        "output_path": output,
                        "model": self.settings.speech_model,
                        "remote_voice_id": remote_id,
                    },
                )
                if version == self.selection_version:
                    await self.send(
                        {
                            "type": "audio",
                            "url": f"/api/audio/{result['audio_path']}",
                            "language": language,
                            "segment_index": index,
                            "voice_id": voice_id,
                        }
                    )
            except Exception:
                logger.exception("Speech workflow failed")
                await self.send(
                    {
                        "type": "error",
                        "message": "Speech generation failed. Check TTS access and worker logs.",
                    }
                )
            finally:
                self.speech_queue.task_done()

    async def finishing(self, stage: str):
        self.record["finishing_phase"] = stage
        self.save()
        await self.send({"type": "finishing", "stage": stage})

    async def run(self):
        self.record["status"] = "recording"
        self.save()
        self.publish_input()
        receiver = asyncio.create_task(self.receive())
        transcript = asyncio.create_task(self.process_transcript())
        translations = asyncio.create_task(self.process_translations())
        speech = asyncio.create_task(self.process_speech())
        heartbeat = asyncio.create_task(self.heartbeat())
        tasks = (receiver, transcript, translations, speech, heartbeat)
        try:
            await self.send(
                {
                    "type": "status",
                    "message": "Connected. Continuous audio is processed through workflows.",
                }
            )
            # Translation failures are reported per phrase and never end the session.
            done, _ = await asyncio.wait(
                (receiver, transcript, speech, heartbeat),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
            if receiver not in done:
                raise RuntimeError(
                    "Live processing ended unexpectedly. Start a new session."
                )
            self.record["status"] = "finishing"
            timings = self.record.setdefault("stop_timings_ms", {})
            stage_started = monotonic()
            await self.finishing("transcription")
            await transcript
            timings["transcription"] = round((monotonic() - stage_started) * 1000)
            self.record["transcript_saved_at"] = now_iso()
            self.save()
            if self.intelligence:
                self.intelligence.start(self.record)
            await self.send({"type": "transcript_saved"})
            stage_started = monotonic()
            await self.finishing("translations")
            await self.drain_translations()
            timings["translations"] = round((monotonic() - stage_started) * 1000)
            stage_started = monotonic()
            await self.finishing("speech")
            await self.speech_queue.join()
            timings["speech"] = round((monotonic() - stage_started) * 1000)
            timings["total"] = round(
                (monotonic() - (self.stop_clock or stage_started)) * 1000
            )
            logger.info("Session %s stop timings (ms): %s", self.record["id"], timings)
            self.record["status"] = "completed"
            self.record.pop("finishing_phase", None)
            self.save()
            await self.send({"type": "stopped"})
        except WebSocketDisconnect:
            self.record["status"] = "disconnected"
        except Exception as exc:
            logger.exception("Live session failed")
            self.record["status"] = "failed"
            message = (
                str(exc)
                if isinstance(exc, (ValueError, RuntimeError))
                else "Session failed. Check backend logs."
            )
            self.record["error"] = message
            with contextlib.suppress(Exception):
                await self.send({"type": "error", "message": message, "fatal": True})
        finally:
            self.input_stopped = True
            self.publish_input()
            if self.translation_started:
                self.translation_stopped = True
                with contextlib.suppress(OSError):
                    self.publish_translations()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.record["ended_at"] = now_iso()
            self.record.pop("finishing_phase", None)
            self.save()
            if (
                self.stop_requested
                and self.intelligence
                and self.intelligence.read(self.record["id"])["status"]
                in ("waiting", "not_started")
            ):
                self.intelligence.start(self.record)
            with contextlib.suppress(Exception):
                await self.socket.close()
