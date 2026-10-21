"""Small atomic file store. Audio stays out of workflow payloads."""

import io
import json
import re
import wave
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class FileStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        for directory in ("voices", "sessions", "audio/input", "audio/output"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Path must stay inside the data directory.")
        return path

    @staticmethod
    def validate_id(identifier: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Invalid record identifier.")
        return identifier

    def write_json(self, relative: str, value: dict) -> None:
        self.write_bytes(
            relative, json.dumps(value, ensure_ascii=False, indent=2).encode()
        )

    def write_bytes(self, relative: str, content: bytes) -> None:
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def read_json(self, relative: str) -> dict:
        return json.loads(self.path(relative).read_text())

    def list_records(self, directory: str) -> list[dict]:
        records = [
            json.loads(path.read_text()) for path in self.path(directory).glob("*.json")
        ]
        return sorted(records, key=lambda record: record["created_at"], reverse=True)

    def voice(self, identifier: str) -> dict:
        return self.read_json(f"voices/{self.validate_id(identifier)}.json")

    def session(self, identifier: str) -> dict:
        return self.read_json(f"sessions/{self.validate_id(identifier)}.json")

    def delete_session(self, identifier: str) -> None:
        """Remove local records/snapshots only; voices and audio remain untouched."""
        self.validate_id(identifier)
        directory = self.path(f"intelligence/{identifier}")
        if directory.is_dir():
            for snapshot in directory.glob("*.json"):
                self.path(str(snapshot.relative_to(self.root))).unlink(missing_ok=True)
            directory.rmdir()
        self.path(f"intelligence/{identifier}.json").unlink(missing_ok=True)
        # Remove the session last so a filesystem failure leaves a retryable row.
        self.path(f"sessions/{identifier}.json").unlink(missing_ok=True)

    def delete_voice(self, identifier: str) -> None:
        voice = self.voice(identifier)
        self.path(voice["sample_path"]).unlink(missing_ok=True)
        self.path(f"voices/{identifier}.json").unlink()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def read_wav(
    content: bytes, *, max_seconds: float, min_seconds: float = 0.1
) -> tuple[bytes, int]:
    """Validate standalone PCM WAVs emitted by the browser, then extract samples."""
    try:
        with wave.open(io.BytesIO(content), "rb") as recording:
            if recording.getnchannels() != 1 or recording.getsampwidth() != 2:
                raise ValueError("Record mono, 16-bit PCM WAV audio.")
            if recording.getframerate() != 16000 or recording.getcomptype() != "NONE":
                raise ValueError("Record uncompressed WAV at 16 kHz.")
            duration = recording.getnframes() / recording.getframerate()
            if not min_seconds <= duration <= max_seconds:
                raise ValueError(
                    f"Recording must be between {min_seconds} and {max_seconds} seconds."
                )
            pcm = recording.readframes(recording.getnframes())
            if len(pcm) != recording.getnframes() * 2:
                raise ValueError("Truncated WAV recording.")
            return pcm, recording.getframerate()
    except (wave.Error, EOFError) as exc:
        raise ValueError("Invalid WAV recording.") from exc
