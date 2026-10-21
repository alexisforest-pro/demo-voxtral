"""Shared configuration for the API and the local workflow worker."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=True)

LANGUAGES = [
    {"code": "de", "name": "German"},
    {"code": "fr", "name": "French"},
    {"code": "es", "name": "Spanish"},
    {"code": "it", "name": "Italian"},
]
LANGUAGE_CODES = {"en", "de", "fr", "es", "it"}
LATENCY_PRESETS = [
    {
        "id": "fast",
        "label": "Responsive",
        "chunk_seconds": 0.2,
        "target_delay_ms": 240,
        "description": "200 ms audio updates with a low-lookahead realtime stream.",
    },
    {
        "id": "balanced",
        "label": "Balanced",
        "chunk_seconds": 1,
        "target_delay_ms": 960,
        "description": "1 second audio updates with more model lookahead.",
    },
    {
        "id": "accurate",
        "label": "More context",
        "chunk_seconds": 2,
        "target_delay_ms": 1920,
        "description": "2 second audio updates; more context, not guaranteed accuracy.",
    },
]


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default_factory=lambda: os.getenv("MISTRAL_API_KEY", ""))
    server_url: str = field(
        default_factory=lambda: os.getenv("SERVER_URL", "https://api.mistral.ai")
    )
    deployment_name: str = field(
        default_factory=lambda: os.getenv("DEPLOYMENT_NAME", "voxbridge-local")
    )
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("DATA_DIR", str(PROJECT_ROOT / "data"))
        ).resolve()
    )
    transcription_model: str = field(
        default_factory=lambda: os.getenv(
            "TRANSCRIPTION_MODEL", "voxtral-mini-transcribe-realtime-2602"
        )
    )
    translation_model: str = field(
        default_factory=lambda: os.getenv("TRANSLATION_MODEL", "mistral-medium-latest")
    )
    intelligence_model: str = field(
        default_factory=lambda: os.getenv(
            "INTELLIGENCE_MODEL",
            os.getenv("TRANSLATION_MODEL", "mistral-medium-latest"),
        )
    )
    speech_model: str = field(
        default_factory=lambda: os.getenv("SPEECH_MODEL", "voxtral-mini-tts-2603")
    )
    default_voice_id: str = field(
        default_factory=lambda: os.getenv("DEFAULT_VOICE_ID", "")
    )
    workflow_timeout: int = 180
    live_timeout: int = 3600
