"""Small workflow payloads carry text or shared local audio references."""

from typing import Literal

from pydantic import BaseModel, Field


class LiveTranscriptionInput(BaseModel):
    input_dir: str
    target_delay_ms: int = 960
    transcription_model: str
    max_seconds: int = Field(default=3600, gt=0, le=3600)


class SegmentInput(BaseModel):
    text: str
    segment_index: int = Field(ge=0)
    source_language: str = "auto"
    previous_text: str = ""
    translation_model: str


class VoiceInput(BaseModel):
    action: Literal["create", "delete"]
    name: str = ""
    languages: list[str] = Field(default_factory=list)
    sample_path: str = ""
    remote_voice_id: str = ""


class SpeechInput(BaseModel):
    text: str
    language: Literal["en", "de", "fr", "es", "it"]
    segment_index: int = Field(ge=0)
    output_path: str
    model: str
    remote_voice_id: str | None = None
    stream_dir: str | None = None
    max_audio_seconds: int = Field(default=120, gt=0, le=1800)


class IntelligenceInput(BaseModel):
    transcript_path: str
    model: str
