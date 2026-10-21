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
        self.translation_queue = asyncio.Queue(maxsize=MAX_PENDING)
        self.speech_queue = asyncio.Queue(maxsize=MAX_PENDING)
        self.send_lock = asyncio.Lock()
        self.input_available = asyncio.Event()
        self.speech_language = record.get("speech_language")
        self.voice_id = record.get("voice_id")
        self.selection_version = 0
        self.index = 0
        self.previous_text = ""
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

    async def heartbeat(self):
        while True:
            self.publish_input()
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
            context = self.previous_text[-2000:]
            self.previous_text = (self.previous_text + " " + text)[-2000:]
            if self.translation_queue.full():
                segment["translation_error"] = (
                    "Translation backlog is full; phrase kept as original text."
                )
                await self.send(
                    {"type": "error", "message": segment["translation_error"]}
                )
            else:
                self.translation_queue.put_nowait((segment, context))
            self.save()
            await self.send(
                {"type": "queue", "pending": self.translation_queue.qsize()}
            )

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

    async def process_translations(self):
        while True:
            segment, context = await self.translation_queue.get()
            index = segment["segment_index"]
            try:
                params = {
                    "text": segment["text"],
                    "segment_index": index,
                    "source_language": self.record["source_language"],
                    "previous_text": context,
                    "translation_model": self.settings.translation_model,
                }
                async for event in self.gateway.stream(
                    "voxbridge-live-segment", params
                ):
                    if event["type"] == "execution":
                        segment["execution_id"] = event["execution_id"]
                        event.update(stage="translation", segment_index=index)
                    elif event["type"] == "translation":
                        segment["translations"][event["language"]] = event["text"]
                    elif event["type"] == "result":
                        result = event["result"] or {}
                        completed = result.get("translations", {})
                        if not isinstance(completed, dict) or not completed:
                            raise ValueError(
                                "Translation workflow returned no completed translations."
                            )
                        segment["translations"].update(completed)
                        for language, text in segment["translations"].items():
                            await self.send(
                                {
                                    "type": "translation",
                                    "language": language,
                                    "text": text,
                                    "segment_index": index,
                                    "final": True,
                                }
                            )
                        continue
                    await self.send(event)
                language = self.speech_language
                text = segment["translations"].get(language, "")
                if language and text:
                    if self.speech_queue.full():
                        await self.send(
                            {
                                "type": "error",
                                "message": "Speech backlog is full; this phrase will remain text-only.",
                            }
                        )
                    else:
                        await self.speech_queue.put(
                            (
                                index,
                                language,
                                text,
                                self.voice_id,
                                self.selection_version,
                            )
                        )
            except Exception:
                logger.exception("Translation workflow failed")
                segment["translation_error"] = (
                    "Translation workflow failed; inspect worker logs and execution in Studio."
                )
                await self.send(
                    {"type": "error", "message": segment["translation_error"]}
                )
            finally:
                self.save()
                self.translation_queue.task_done()
                await self.send(
                    {"type": "queue", "pending": self.translation_queue.qsize()}
                )

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
            done, _ = await asyncio.wait(
                (receiver, transcript, translations, speech, heartbeat),
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
            await self.translation_queue.join()
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
