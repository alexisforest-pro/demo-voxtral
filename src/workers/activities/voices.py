"""Create and delete remote Mistral voices through workflow activities."""

import base64
from datetime import timedelta

from mistralai import workflows
from mistralai.client import Mistral
from mistralai.client.errors import SDKError

from backend.config import Settings
from backend.storage import FileStore
from workers.models import VoiceInput


@workflows.activity(
    start_to_close_timeout=timedelta(seconds=90), retry_policy_max_attempts=1
)
async def manage_voice(params: VoiceInput) -> dict:
    settings = Settings()
    async with Mistral(
        api_key=settings.api_key, server_url=settings.server_url
    ) as client:
        if params.action == "delete":
            try:
                await client.audio.voices.delete_async(voice_id=params.remote_voice_id)
            except SDKError as exc:
                if exc.status_code != 404:
                    raise
            return {"deleted": True}
        sample = FileStore(settings.data_dir).path(params.sample_path).read_bytes()
        voice = await client.audio.voices.create_async(
            name=params.name,
            sample_audio=base64.b64encode(sample).decode(),
            sample_filename="voice.wav",
            languages=params.languages,
        )
        return {"remote_voice_id": voice.id}
