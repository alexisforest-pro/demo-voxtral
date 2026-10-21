# VoxBridge

**Speak once, follow the conversation in four languages.** A local, single-user presales prototype for live transcription, translation, and speech with a cloned or default voice. Not a production meeting platform or a guaranteed real-time interpreter.

## Features

- **Live Transcript:** full original text plus German, French, Spanish, and Italian translations.
- **03 Record Intelligence:** one row per saved session; click for transcript-derived KPIs, announcements, actions, deadlines, and supporting quotes. Missing information displays **N/A**.
- **Optional live speech:** enable **Live Translation** on one language card at a time.
- **Recorded playback:** stop recording, then stream the entire saved translation—not just the first phrase.
- **Voice capture:** record your voice or a speaker who explicitly permits hosted cloning. Permission is handled outside the app; new profiles do not claim a recorded consent timestamp. Choose microphone, Teams/browser-tab audio, or a configured desktop loopback audio input; save it for all languages or a specific language. Input level, missing-audio errors, and silent/clipped sample checks help catch capture problems before saving.
- **English voice preview:** stream the fixed fictional sample: “EU Countries have reached a unique agreement to deploy an exceptional budget to train “Le Gros Chaton” with Mistral AI. The three-year programme will coordinate open datasets, public demonstrations, and multilingual workshops across every member state.” Includes Stop, volume, and cached replay. The sample is not an official announcement.
- **Clear voice library:** confirmed bulk deletion, with progress and failed profiles retained for retry.
- **Clear all transcripts:** reset the workspace. Starting a new recording also clears it automatically.
- **EU-inspired interface:** blue/gold palette and European emblem, provider-neutral UI copy, no technical panel; saved sessions appear in Record Intelligence. Independent demo, not an official EU service or endorsement.

Translation controls are grouped **Default voice / Play / Live Translation**. Play is a compact light-blue button; the recording hint is small and italic. It becomes Stop during playback.

## Run locally

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), a recent AudioWorklet-capable browser, microphone access, and a Mistral API key with access to Workflows, realtime transcription, translation, and TTS/voice cloning.

From the project root (`demo-voxtral`):

```bash
uv sync --locked
```

For a new installation, copy `.env.example` to `.env` and enter the key. **Do not overwrite an existing `.env` or commit credentials.** Backend and worker must use the same `DEPLOYMENT_NAME` and shared data directory.

Run in separate terminals:

```bash
make start-worker
make start-app
```

Open **http://127.0.0.1:8000**; API docs: `/docs`. Both processes must stay running. Microphone access requires localhost or HTTPS. No Node.js build, database, Redis, or local Temporal server is needed; AI processing and workflow orchestration are hosted.

After these updates, **restart both backend and worker, then refresh the browser**. Later frontend-only changes need a refresh; Python changes need the affected process restarted.

## Demo flow

1. Optionally open **Capture a voice**, enter a name/scope, choose an audio source, and obtain the voice owner’s explicit permission outside the app before recording and hosted cloning.
2. Record 5–20 seconds in a quiet room (API limits: 3–30 seconds), preview the sample, and **Save cloned voice**.
3. Click **Read in English** to hear the saved voice immediately as audio arrives. Stop or adjust volume while streaming; use native audio controls for replay/seek afterward.
4. Open **Live Transcript**, choose source language and audio-update preset, then **Start recording**. No clone is needed for text-only capture.
5. For live spoken output, select a voice and enable **Live Translation** on one card. Leave it unchecked for text-only recording.
6. Stop recording. **03 Record Intelligence** analyses the saved source transcript in the background; open a session row for KPIs and details. Wait for translation/speech finishing before **Play** on any language card. Select another card to switch languages; click Stop to cancel. Export the current session JSON before refreshing.

Translated speech has mute, queue clearing, and volume from 0–200%. Gain above 100% can clip. Use headphones to prevent generated speech feeding back into the microphone. These controls do not suppress the original speaker.

### Teams showcase on one laptop

**Recommended: Teams web in a separate tab in desktop Chrome or Edge.** The app is not a Teams bot and cannot access a meeting just because you joined it. It captures only the source you explicitly share. Browser/OS/organisation restrictions may prevent capture; rehearse on the exact demo machine.

1. Start backend and worker, open VoxBridge on localhost, and join Teams **in the same browser** in another tab. Use headphones. Share the VoxBridge window/tab to your meeting audience separately; Teams screen sharing does not supply audio to VoxBridge.
2. Obtain the participant’s explicit permission to record their sample, send it to the hosted AI service, and clone their voice for this showcase. Explain that the sample/profile is stored locally and the cloned voice is hosted remotely. Ask everyone else to stay silent; this app does not isolate speakers from a mixed meeting.
3. Open **Capture a voice**, name the speaker, choose **Teams / browser tab audio**. There is no in-app permission checkbox; obtain permission separately before recording.
4. Click **Record sample**. In the browser capture picker choose the **Teams tab** and enable **Share tab audio**. Do not choose VoxBridge, a window, or an entire screen. The browser requires visual sharing for tab-audio access, but this app processes/saves only audio; video is not uploaded.
5. Ask the participant to read the displayed sentence or speak naturally for 5–20 seconds. Check the input-level meter and **Audio detected** status. Stop, listen to the sample, and only then **Save cloned voice**. Samples below three seconds, silent/very quiet samples, and heavily clipped samples cannot be saved from the UI; these signal checks do not prove speaker identity or voice quality. Capture stops by 29 seconds or when sharing ends.
6. Click **Read in English**. To let the audience hear generated speech, enable Teams **Include sound** / **Share tab audio** on your presentation share where supported. This is separate from VoxBridge’s capture permission. Stop voice capture before playing the preview to avoid feedback. Rehearse sound sharing too.

**Desktop Teams:** a normal microphone does not receive Teams’ digital audio. Select **Audio input · desktop Teams / loopback** only after configuring an OS-level virtual audio device (for example BlackHole/Loopback on macOS or a virtual audio cable on Windows). Route Teams’ **speaker output** into that device, click **Refresh audio inputs**, and select the matching input in VoxBridge. Device refresh temporarily requests microphone access and immediately releases its tracks. Echo cancellation, noise suppression, and automatic gain control are disabled for this source. Monitor through headphones using the loopback tool’s routing; keep your Teams microphone on your real microphone, not the loopback, to avoid broadcasting the meeting back into itself. VoxBridge does not install or configure these devices.

No missing-audio condition silently falls back to the microphone. If your browser cannot provide tab audio, use the configured loopback route instead. Playing Teams through laptop speakers and re-recording it is not a reliable cloning source. **Live Transcript still uses the microphone**; the new source selector applies only to voice samples. Cloning/model access and fidelity still depend on the hosted service and the participant’s Teams audio quality.

### Clearing and deletion

| Action | Effect | Preserved |
|---|---|---|
| **Clear all transcripts** / new recording | Stop playback; reset source/translated text, timers, diagnostics, and export link. Clear also stops microphone capture and disconnects an active session; late updates are ignored. | Voice/language selections, volume/mute, voice library, saved files and audio caches. |
| **Clear voice library** | Confirm, then delete listed remote voices and local profiles/samples sequentially. Failures remain for retry; deleted selections fall back to Default voice. | Transcripts, saved sessions, generated audio. |
| **Clear audio queue** | Stop current/queued browser audio. | Recording and future live speech generation. |

Transcript clear is temporarily disabled during connection setup. Library clear is disabled while recording or saving a voice; deletion blocks conflicting voice creation, preview, and new recording actions. Active-session voices are protected by the API. Individual voice deletion remains available.

## Record Intelligence

Press **Stop recording** in screen 02 to request analysis. After the final original transcript is saved, a fifth workflow extracts KPIs in the background without waiting for translations or speech. Screen **03 Record Intelligence** lists every saved session and its analysis status; click a row for details. Results survive refresh/restart. Every session has an automatic readable title: the existing AI title when available, otherwise a short source-transcript excerpt; empty recordings use their date/time. Older sessions get these titles without extra inference. Older sessions are listed but are only analysed when you click **Analyse**; **Retry** restarts failed/interrupted analysis. **Re-analyse** on completed reports asks for confirmation before new hosted inference; the previous report remains visible until a successful replacement, including when the new attempt fails. Existing reports are not automatically rewritten. Empty transcripts show N/A without inference.

Details include summary, topics, speakers and institutions explicitly mentioned, policy references, budgets/targets/percentages, announcements, actions/owners/deadlines, and dates. Missing values display **N/A**, never zero. KPI/action/announcement/date entries include a source-language quote; unsupported quotes are discarded. Extraction is not factual or legal verification: proposals must not be treated as adopted measures, and speaker identity cannot be inferred from voice. Partial transcripts are marked incomplete.

New extraction explicitly evaluates every section and requires all response fields, including nullable values. Nonempty speech requires title, summary, and topics; basic self-introduction and monetary-figure checks catch obvious omissions. Currency amounts/percentages and standalone counts cannot be Dates. Failed field/type/category checks allow one correction request within the same 150-second deadline (at most two inference calls); continued failure is reported, not saved as a successful empty report. Transport errors, malformed JSON, and truncated output are not automatically retried. Worker logs contain safe validation reasons and collection counts, not raw transcript/model text. These checks improve completeness but do not guarantee every relevant entity or fact is found.

Analysis uses the entire source transcript, not its translations, and is saved separately under `data/intelligence/`. Clearing the workspace, deleting voices, and starting a new recording preserve this history. Hosted analysis may fail for oversized inputs; it does not silently shorten the transcript. Backend interruption marks unfinished analysis for explicit retry instead of automatically replaying paid inference.

**Delete all records** in screen 03 asks for confirmation before permanently removing local saved-session JSON (source and translations), intelligence reports, and analysis snapshots. Voices, microphone recordings, generated audio/caches, and hosted workflow history are preserved; this is not a complete personal-data erasure. Active recordings, analysis jobs, playback generation, or translation recovery are retained and reported for retry. Deletion failures also keep a retryable row. New recordings and analysis requests are blocked in the browser while deletion runs. If the current session is deleted, its workspace/export link is reset. Existing files are not deleted until you confirm this button.

## Streaming playback and caching

- **Full translation:** the backend orders every completed source phrase, recovers missing translations from existing completed workflow results, and refuses playback if any phrase is still untranslated. No silent extract and no new translation inference during recovery. New recordings use completion-event results directly to avoid losing translations to a stale result read.
- **Streaming:** the speech activity produces mono 24 kHz float32 PCM, writes chunks of at most 250 ms, and updates an atomic manifest. The backend checks every 50 ms and relays NDJSON frames to Web Audio. Playback starts before generation finishes.
- **Long text:** up to 250 words per TTS request, with sentence-aware blocks appended automatically in order. Recorded playback limits: 5,000 words, 30 minutes of generated audio, and a 180-second generation timeout. Exceeding limits reports an error, not truncation.
- **Cache:** completed audio is saved atomically as a 16-bit WAV, keyed by text, language, voice/model, and format version. Repeat requests reuse it across refresh/backend restart. Concurrent identical requests share generation in this single-process backend. Failed/incomplete audio is not reused.
- **Cancellation:** Stop, mute, clear, voice/language changes, or navigation stop browser playback and ignore late results. Already-started hosted generation continues to finish the cache. Network/model stalls can still cause gaps.

Preview text is fixed by the backend; it is not a free-text TTS endpoint. Testing in English does not change the profile’s language scope. Voice fidelity and cross-language accent quality depend on the model and recording.

## Language coverage

The app currently enables **four translation/spoken-output languages: German, French, Spanish, Italian**. English is also enabled for the cloned-voice preview. Model coverage is broader than the app:

| Capability | EU coverage | Languages |
|---|---|---|
| Voxtral TTS, including cloning | **7 of 24** documented | English, French, German, Italian, Spanish, Portuguese, Dutch. |
| Language-model translation | **15 of 24** with documented strong expected language performance, including English | The seven above, plus Croatian, Czech, Danish, Finnish, Greek, Polish, Romanian, Swedish. |

The language-model list is not a translation-quality guarantee or a model-specific translation certification. Bulgarian, Estonian, Hungarian, Irish, Latvian, Lithuanian, Maltese, Slovak, and Slovenian can be evaluated, but are not on the published strong-performance list. Portuguese/Dutch TTS and the additional translation languages are **not wired into this prototype**. Speech-to-text coverage is separate.

Sources: [TTS languages](https://docs.mistral.ai/studio/audio/text_to_speech), [language-model coverage](https://docs.mistral.ai/resources/languages). Validate each language before customer-facing support claims.

## Architecture

| Component | Responsibility | Why |
|---|---|---|
| Browser (`frontend/`) | Microphone capture/resampling, text rendering, WebSocket upload, Web Audio playback. | Plain HTML/CSS/ES modules; no frontend build. |
| FastAPI (`src/backend/`) | Serve UI/API, validate input, store files, manage independent queues, call Workflows, relay events/chunks. | One localhost origin; credentials stay server-side. |
| Worker (`src/workers/`) | Register five workflows and execute all AI/remote voice activities. | Hosted orchestration; I/O stays outside deterministic workflow definitions. |

**All model inference and remote voice operations run in workflow activities.** The backend never calls transcription/chat/TTS/voice endpoints directly.

```text
Microphone → local WAV chunks → continuous transcription workflow → source text
Committed phrases → four parallel translation activities → DE / FR / ES / IT
Optional live speech → speech workflow → MP3 queue
Full recorded translation / English preview → speech workflow → PCM stream + WAV cache
Voice sample / deletion → voice workflow → remote voice + local profile
Stop → final saved source transcript → intelligence workflow → persisted KPI report
```

| Workflow | Job |
|---|---|
| `voxbridge-live-transcription` | One persistent realtime transcription connection per session. |
| `voxbridge-live-segment` | Translate supplied text into four languages; no audio transcription. |
| `voxbridge-spoken-translation` | Live MP3 speech or streamed PCM for full playback/previews. |
| `voxbridge-voice-profile` | Create/delete remote voices. |
| `voxbridge-record-intelligence` | Extract a structured report from the full saved source transcript. |

Key choices:

- **Persistent STT:** chunks do not reset model context; sending waits for session setup acknowledgment.
- **Independent bounded queues:** translations and speech cannot block source text. Translation backlog preserves source phrases and reports missing translations.
- **Shared files:** workflow payloads contain paths, not audio blobs. Backend and worker must share a writable filesystem; an arbitrary remote worker cannot read laptop paths.
- **Atomic JSON storage:** simple to inspect/export, but intended for one local user, not concurrent multi-user recovery or retention management.
- **Event relay:** SDK Task snapshots/patches stream incremental text; event reconnection resumes the same execution, not microphone sessions after a restart.

## Latency

| Preset | Upload interval | Requested transcription lookahead |
|---|---:|---:|
| Responsive | 200 ms | 240 ms |
| Balanced (default) | 1 s | 960 ms |
| More context | 2 s | 1,920 ms |

A single microphone chunk may not exceed **3 seconds**. These values are not end-to-end guarantees: workflow dispatch, model processing, and network transport add latency. Source deltas render immediately; a three-second whitespace-aware batching timer commits phrases for translation. Silence is retained for continuous context.

Normal stop ends microphone capture immediately. **Finishing** waits for final transcription, then the translation queue, then the speech queue—not just a file save. Source phrases are saved incrementally. The interface now identifies each stage and announces **Transcript saved** before translation/speech drain finishes; intelligence runs independently. Session exports and backend logs contain `stop_timings_ms` (remaining transcription, translation, speech, and total wait) to diagnose future 5–10 second waits. These timings start when the backend receives Stop; they do not measure the browser microphone flush. The waits remain to preserve the final text/audio rather than cutting them off. Fatal transcription errors abort final processing. A completed session may still contain translation/TTS failures. Live transcription has a one-hour timeout, heartbeat, and backend lease; disconnected sessions cannot resume. Activities use one attempt to avoid replaying paid/non-resumable inference.

## Configuration

Project-root `.env` overrides matching process environment values. Without a mounted `.env`, container `-e` overrides `--env-file`.

| Variable | Default / purpose |
|---|---|
| `MISTRAL_API_KEY` | Required for hosted operations. |
| `SERVER_URL` | `https://api.mistral.ai` |
| `DEPLOYMENT_NAME` | `voxbridge-local`; identical for backend/worker. |
| `DATA_DIR` | Project-root `data/`; shared by backend/worker. |
| `TRANSCRIPTION_MODEL` | `voxtral-mini-transcribe-realtime-2602` |
| `TRANSLATION_MODEL` | `mistral-medium-latest` |
| `INTELLIGENCE_MODEL` | Defaults to `TRANSLATION_MODEL`; used for transcript KPI extraction. |
| `SPEECH_MODEL` | `voxtral-mini-tts-2603` |
| `DEFAULT_VOICE_ID` | Optional explicit voice ID/alias; otherwise select a compatible neutral speech-v1 preset. |

Automatic default selection uses named `gb_jane_*`, `gb_oliver_*`, `en_paul_*`, and `fr_marie_*` aliases, excludes unsupported `voice_<number>` presets, and normalizes regional language codes. If no language match exists, it uses a compatible cross-language fallback. Explicit cloned/configured voices are unchanged.

Use models available to your account. `mistral-medium-latest` can change; pin a supported model for reproducibility. `uv.lock` pins dependencies, not hosted model revisions.

## Files and API

```text
frontend/                       UI, microphone and playback modules
src/backend/                    FastAPI, storage, queues, gateway, streaming cache
src/backend/tests/              Offline API, worker and browser regressions
src/workers/workflows/           Five workflow definitions
src/workers/activities/          Transcription, translation, speech and voice I/O
data/voices/                    Profile JSON and recorded samples
data/sessions/                  Session JSON
data/intelligence/              Persisted reports, status and full transcript snapshots
data/audio/input/<session>/     Microphone WAVs, control.json, error diagnostics
data/audio/output/              Live MP3s, PCM chunks and cached WAVs
```

Cache locations: `voice-previews/<voice-id>/<key>.wav` and `recorded-playback/<session-id>/<key>.wav` under `data/audio/output/`. Clearing the workspace or deleting a profile does not remove generated audio. Retention is manual.

Main endpoints:

- `POST /api/voices/{id}/preview` → cached URL or preview stream URL.
- `POST /api/sessions/{id}/playback` → full-translation cached URL or stream URL.
- `GET .../{key}/stream` → NDJSON audio frames and completion/error.
- `GET /api/sessions` → automatically titled saved-session rows with analysis status/report.
- `DELETE /api/sessions` → delete local records/snapshots; return deleted IDs and retained failures/busy records.
- `GET /api/sessions/{id}/intelligence` → session metadata and persisted intelligence.
- `POST /api/sessions/{id}/intelligence` → background analysis/retry for a stopped session; `?reanalyse=true` explicitly regenerates a completed report.
- `GET /api/sessions/{id}/export` → session JSON (the intelligence report is retrieved separately).
- `POST /api/sessions/{id}/speech` → existing phrase-level API, retained for clients; the UI uses full playback.

Exports include source/translated text, selections, transcription/translation execution IDs, and persisted errors. They exclude model/deployment settings, TTS execution IDs, and TTS warnings. Refresh clears the browser workspace; stop/export first.

`uv sync` installs backend/workers in editable mode; root frontend assets require the source checkout. A wheel alone is not a self-contained web deployment. `.agents/` references and the `.vibe/` check hook are development tooling, not runtime code.

### Worker container

```bash
make docker-build
make docker-run
```

The image runs only `python -m workers.worker`. `docker-run` mounts project-root `data/` at `/app/data`, loads `.env`, and sets `DATA_DIR=/app/data`. The host backend must use the same files; use a matching custom bind mount for another data directory. Secrets/data are not baked into the image. No cloud deployment automation is provided.

## Tests

```bash
make test          # Fast offline core tests
make check         # Core tests + Ruff, workflow mypy and Semgrep
make test-browser  # Browser tests
make test-all      # All offline tests
make check-all     # All tests + code checks
make format        # Python formatting/autofixes
```

**85 core tests; 122 total (37 browser scenarios).** The earlier 168-test suite was reduced by removing overlap; focused intelligence/Stop/title/deletion and Teams voice-capture regressions were added. The latest core run took **2.39 seconds**; the latest full suite took **18.74 seconds** on the development machine. Timings vary. The `.vibe/` hook runs the fast `make check`. All browser scenarios share one module-scoped runtime, with a fresh isolated context per case.

Coverage includes stream setup/errors, full text history, voice capture/deletion, compatible presets, full-translation recovery, multi-block PCM continuation, caching, cancellation, volume, five-workflow sandbox validation, full-source intelligence routing, evidence/schema validation, persisted history/retry, N/A rendering, and extraction independence from Stop draining. Tests mock hosted inference and do not prove translation quality, model access, voice fidelity, or cloud latency. Slow live-TTS independence has no dedicated regression.

Install Chromium if needed:

```bash
uv run playwright install chromium
```

`PLAYWRIGHT_CHROMIUM_EXECUTABLE` can select a local browser. Tests skip without one. Current checkbox-removal validation: **85 core tests and 15 focused capture/save browser tests passed**, plus Ruff, workflow mypy, and Semgrep. The full browser suite did not pass: runs encountered headless audio-clock stalls and asynchronous assertion failures in playback/deletion scenarios; these unrelated scenarios were not changed. Teams capture coverage includes microphone/tab/loopback sample upload, no microphone fallback, cancelled/unsupported/missing-audio sharing, setup cleanup, stereo right-channel audio through the real AudioWorklet, sharing-end/29-second shutdown, silent/clipped samples, checkbox-free capture/save and source reset, and input-device permission cleanup. Tests substitute local streams for Teams and mock hosted cloning; an actual Teams call and hosted voice fidelity still require a rehearsal. The flaky wall-clock fake-microphone test was removed; deterministic sample/cadence/tail checks and microphone shutdown scenarios remain. Repetitive language/preset matrices were reduced to representative paths; full-translation recovery still covers all four languages. The suite no longer measures real-device timing. Some audio-completion cases manually finish scheduled buffers to avoid headless audio-clock stalls.

## Troubleshooting

- **No microphone:** use localhost/HTTPS and grant permission.
- **Teams voice sample is silent:** use Teams web + browser-tab capture with **Share tab audio**, or a configured desktop loopback input. Granting microphone permission alone does not capture Teams output. Check the input meter and listen to the sample before saving; do not just increase speaker volume.
- **Finishing for several seconds:** check whether the status is waiting for final text, translations, or speech. The session details/export include stage timings for new recordings; intelligence does not block completion.
- **Intelligence failed:** restart both processes so the fifth workflow is registered, check analysis model access, then click **Retry**. No report is fabricated on failure.
- **No text / workflow failure:** keep backend and worker running with matching deployment/data paths. Check worker logs, exports, and `data/audio/input/<session>/workflow_error.json`. Parsed service diagnostics include available code/request/execution IDs. An earlier live transcription rejection remains unexplained.
- **Playback is incomplete:** missing translations are recovered from completed workflows when possible; unresolved phrases block full playback. Export/check their execution IDs rather than accepting an extract.
- **TTS code 3051:** an incompatible preset was selected. Automatic selection excludes these presets; restart the worker after the fix. An explicit `DEFAULT_VOICE_ID` must be compatible, e.g. `gb_jane_neutral`. Moderation can also reject speech text.
- **Backlog/gaps:** shorten the session, add pauses, or disable live speech. Streaming reduces wait-for-file latency, not model/network delays.
- **Disconnect/restart:** start a new microphone session. Existing local records remain; some hosted work may finish after disconnect.

## Privacy and limits

**Local files do not mean local-only processing.** Audio/text/voice samples are sent to Mistral; workflows and observability are hosted, and cloned voices remain remote until deleted. Confirm processing location, contracts, and retention before using sensitive material. This prototype does not establish GDPR compliance or EU-only processing.

No authentication, encryption at rest, automatic retention, multi-user isolation, or cross-process cache deduplication. Bind to `127.0.0.1`, not a public interface. Use app deletion for remote voices; deleting local files alone is insufficient.

## References

- [Realtime transcription](https://docs.mistral.ai/studio/audio/speech_to_text/realtime_transcription)
- [Reusable voices](https://docs.mistral.ai/studio/audio/text_to_speech/voices)
- [Speech generation](https://docs.mistral.ai/studio/audio/text_to_speech/speech)
- [Workflow Task publishing](https://docs.mistral.ai/studio/workflows/building-workflows/streaming)
- [Workflow event consumption](https://docs.mistral.ai/studio-api/workflows/building-workflows/consuming_events)
