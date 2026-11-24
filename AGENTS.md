# AGENTS.md — VoxBridge (demo-voxtral)

## What this project is

VoxBridge: a local, single-user presales prototype for **live transcription, translation (DE/FR/ES/NL), and spoken playback with a cloned or default voice**, plus "Record Intelligence" (KPI extraction from saved session transcripts). It is a demo, not a production meeting platform.

All AI inference (STT, translation, TTS, voice cloning, intelligence) runs **hosted on Mistral Workflows** (Temporal-based orchestration). The backend never calls model endpoints directly — everything goes through workflow activities.

## Architecture and layout

```text
frontend/                  Plain HTML/CSS/ES modules, no build step. Served as static files.
  app.js                   Main UI logic (785 lines)
  audio-recorder.js        Microphone capture, AudioWorklet resampling, WAV chunks
  speech-player.js         Web Audio playback of streamed PCM/MP3
  voice-preview-player.js  English preview playback
  api.js                   Backend API client
src/backend/               FastAPI app (single 127.0.0.1 origin), storage, queues, gateway
  app.py                   HTTP/WebSocket entrypoint, routes
  config.py                Settings dataclass + LANGUAGES + LATENCY_PRESETS (env-driven)
  live_session.py          Per-session live transcription/translation/speech orchestration
  workflow_gateway.py      Client for hosted Workflows (start, events, task snapshots)
  storage.py               Atomic JSON/WAV file store under DATA_DIR
  record_intelligence.py   Background KPI analysis wiring
  voice_preview.py         Fixed-text English preview + WAV cache
  tests/                   pytest suite (core + Playwright browser tests)
src/workers/
  worker.py                Registers the 5 workflows and polls (python -m workers.worker)
  workflows/               5 workflow definitions (live_transcription, live_segment,
                           spoken_translation, voice_profile, record_intelligence)
  activities/              All remote I/O: transcription, translation, speech, voices, intelligence
  intelligence_schema.py   Pydantic schema for KPI reports
  models.py                Workflow input models
data/                      Runtime state (gitignored): sessions/, voices/, intelligence/,
                           audio/input|output. Backend and worker MUST share this directory.
```

Data flow: `Microphone → WAV chunks → live-transcription workflow → source text → 4 parallel translation activities → optional speech workflow → MP3/PCM → browser`. Stop triggers the record-intelligence workflow on the saved source transcript.

## Commands (from project root)

```bash
uv sync --locked           # install locked deps (Python 3.12+, uv required)
make start-worker          # terminal 1: workflow worker
make start-app             # terminal 2: uvicorn on http://127.0.0.1:8000 (API docs at /docs)
make test                  # fast offline core tests (85)
make test-browser          # Playwright browser tests (37 scenarios; needs Chromium)
make test-all              # all offline tests (122)
make check                 # lint + format-check + mypy(workflows) + semgrep + core tests
make check-all             # everything incl. browser tests
make format                # ruff format + autofix
make docker-build / docker-run   # worker-only container sharing ./data
```

Config: copy `.env.example` to `.env` once; never overwrite or commit `.env`. Key vars: `MISTRAL_API_KEY` (required), `DEPLOYMENT_NAME` (must match between backend and worker), `DATA_DIR`, and model overrides (`TRANSCRIPTION_MODEL`, `TRANSLATION_MODEL`, `SPEECH_MODEL`, `INTELLIGENCE_MODEL`, `DEFAULT_VOICE_ID`). Defaults live in `src/backend/config.py`.

## Rules and conventions for agents

1. **All model/remote-voice calls belong in workflow activities** under `src/workers/activities/`. Never add direct Mistral model calls to the backend.
2. **Workflow definitions must stay deterministic.** Import activities inside `workflows.workflow.unsafe.imports_passed_through()` blocks; no I/O in workflow code. Semgrep rules in `.agents/skills/workflows/scripts/linting/rules/` enforce this — run `make lint-workflows`.
3. **Workflow payloads carry file paths, never audio/text blobs** — backend and worker share the filesystem via `DATA_DIR`.
4. **Activities use `retry_policy_max_attempts=1`** — do not add retries to paid/non-resumable inference.
5. **Storage is atomic JSON/WAV** via `src/backend/storage.py`; keep it single-user — no multi-user isolation, no encryption at rest.
6. Python style: ruff format + ruff check on `src/backend/` and `src/workers/`; mypy only on `src/workers/workflows/`. Run `make check` before finishing — a `.vibe` hook also runs it automatically after each agent turn and blocks on failure.
7. Tests live in `src/backend/tests/`; mock all hosted inference. Browser tests need `uv run playwright install chromium` and are skipped without a browser.
8. Frontend is vanilla ES modules — no Node build, no new frontend dependencies without discussion.
9. **Bind to 127.0.0.1 only.** This app has no auth; never expose it publicly.
10. **Never commit credentials or runtime data**: `.env*` (except `.env.example`) and `data/**` are gitignored — keep them that way.
11. After changing Python, the affected process (backend or worker) must be restarted; frontend-only changes just need a browser refresh. Backend and worker must always run with the same `DEPLOYMENT_NAME` and `DATA_DIR`.
12. README.md is the product spec — keep it in sync when behavior, endpoints, models, or test counts change.

## Useful references

- Mistral docs: realtime transcription, TTS/voices, Workflows streaming/consuming events (links in README.md).
- `make check` is the gate; `make check-all` before handing off larger changes.
