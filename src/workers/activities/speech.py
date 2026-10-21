"""Voxtral TTS; return an audio file reference, not a large encoded payload."""

import base64
import io
import math
import struct
import wave
from datetime import timedelta

from mistralai import workflows
from mistralai.client import Mistral

from backend.config import Settings
from backend.storage import FileStore
from workers.models import SpeechInput


@workflows.activity(
    start_to_close_timeout=timedelta(seconds=180), retry_policy_max_attempts=1
)
async def generate_speech(params: SpeechInput) -> dict:
    settings = Settings()
    async with Mistral(
        api_key=settings.api_key, server_url=settings.server_url
    ) as client:
        voice_id = params.remote_voice_id or settings.default_voice_id
        if not voice_id:
            presets = await client.audio.voices.list_async(type_="preset", limit=100)
            voice_id = select_default_voice(presets.items, params.language)
        if params.stream_dir:
            return await stream_speech(client, params, voice_id, settings)
        response = await client.audio.speech.complete_async(
            model=params.model,
            input=params.text,
            voice_id=voice_id,
            response_format="mp3",
        )
    FileStore(settings.data_dir).write_bytes(
        params.output_path, base64.b64decode(response.audio_data, validate=True)
    )
    return {"audio_path": params.output_path}


def select_default_voice(presets, language: str) -> str:
    # The catalogue mixes named v1 aliases with voice_<number> presets
    # that are not accepted by the speech v1 endpoint.
    # Use the named v1 alias families, rather than accepting a language match alone.
    compatible = [
        voice
        for voice in presets
        if isinstance(voice.slug, str)
        and voice.slug.startswith(("gb_jane_", "gb_oliver_", "en_paul_", "fr_marie_"))
    ]
    if not compatible:
        raise RuntimeError(
            "No compatible preset voice available. Set DEFAULT_VOICE_ID to a speech v1 preset alias or select a cloned voice."
        )

    def rank(voice):
        codes = {
            code.lower().replace("-", "_").split("_")[0]
            for code in (voice.languages or [])
        }
        return (
            language not in codes,
            not voice.slug.endswith("_neutral"),
            voice.slug != "gb_jane_neutral",
            voice.slug,
        )

    return min(compatible, key=rank).slug


def speech_blocks(text: str) -> list[str]:
    """Keep every word in order while staying under the recommended 300-word prompt size."""
    words = text.split()
    blocks = []
    while words:
        end = min(250, len(words))
        if end < len(words):
            boundary = next(
                (
                    index
                    for index in range(end, end // 2, -1)
                    if words[index - 1].endswith((".", "!", "?"))
                ),
                end,
            )
            end = boundary
        blocks.append(" ".join(words[:end]))
        words = words[end:]
    return blocks


async def stream_speech(
    client, params: SpeechInput, voice_id: str, settings: Settings
) -> dict:
    store = FileStore(settings.data_dir)
    pcm = bytearray()
    tail = b""
    chunks = 0
    done = False
    for text in speech_blocks(params.text):
        done = False
        stream = await client.audio.speech.complete_async(
            model=params.model,
            input=text,
            voice_id=voice_id,
            response_format="pcm",
            stream=True,
        )
        block_bytes = 0
        async with stream:
            async for event in stream:
                if event.data.type == "speech.audio.delta":
                    raw = tail + base64.b64decode(event.data.audio_data, validate=True)
                    aligned = len(raw) - len(raw) % 4
                    tail = raw[aligned:]
                    raw = raw[:aligned]
                    if not raw:
                        continue
                    if len(pcm) + len(raw) > 24000 * 4 * params.max_audio_seconds:
                        raise ValueError(
                            "Generated speech exceeds the audio duration limit."
                        )
                    if any(
                        not math.isfinite(value[0])
                        for value in struct.iter_unpack("<f", raw)
                    ):
                        raise ValueError(
                            "Generated speech contains invalid PCM samples."
                        )
                    pcm.extend(raw)
                    block_bytes += len(raw)
                    for offset in range(0, len(raw), 24000):
                        store.write_bytes(
                            f"{params.stream_dir}/{chunks:06d}.pcm",
                            raw[offset : offset + 24000],
                        )
                        chunks += 1
                    store.write_json(
                        f"{params.stream_dir}/control.json", {"chunks": chunks}
                    )
                elif event.data.type == "speech.audio.done":
                    done = True
                    break
        if not done or not block_bytes or tail:
            raise ValueError("Speech stream ended without complete audio.")
    if not pcm:
        raise ValueError("No speech text was supplied.")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(
            b"".join(
                struct.pack("<h", round(max(-1, min(1, value[0])) * 32767))
                for value in struct.iter_unpack("<f", pcm)
            )
        )
    store.write_bytes(params.output_path, output.getvalue())
    return {"audio_path": params.output_path}
