"""Browser acceptance scenarios with one module-scoped runtime and isolated pages."""

import json
import os
import shutil
import struct
import threading
from email.parser import BytesParser
from email.policy import default
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from backend.app import VOICE_PREVIEW_TEXT
from backend.config import LATENCY_PRESETS, PROJECT_ROOT
from backend.storage import read_wav
from backend.tests.test_app import wav


@pytest.fixture(scope="module")
def browser_runtime():
    chrome = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
    )
    mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome and mac_chrome.is_file():
        chrome = str(mac_chrome)

    class FrontendHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(PROJECT_ROOT / "frontend"), **kwargs)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), FrontendHandler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        with sync_playwright() as playwright:
            if not chrome and not Path(playwright.chromium.executable_path).is_file():
                pytest.skip("Install Chromium with uv run playwright install chromium.")
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                ],
            )
            try:
                yield browser, f"http://127.0.0.1:{server.server_port}"
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def browser_page(browser_runtime):
    browser, origin = browser_runtime
    context = browser.new_context(
        permissions=["microphone"], viewport={"width": 1280, "height": 800}
    )
    try:
        context.set_default_timeout(5000)
        page = context.new_page()
        page.route(
            "**/api/config",
            lambda route: route.fulfill(
                json={
                    "api_key_configured": True,
                    "models": {
                        "transcription": "test",
                        "translation": "test",
                        "speech": "test",
                    },
                    "deployment_name": "offline-browser-test",
                    "latency_presets": LATENCY_PRESETS,
                    "voice_preview_text": VOICE_PREVIEW_TEXT,
                }
            ),
        )
        page.route(
            "**/api/sessions",
            lambda route: route.fulfill(json={"sessions": []}),
        )
        yield page, origin
    finally:
        context.close()


def test_european_theme_and_provider_neutral_copy(browser_page):
    width = 390
    page, origin = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": []}))
    page.route(
        "**/api/config",
        lambda route: route.fulfill(
            json={
                "api_key_configured": False,
                "models": {
                    "transcription": "voxtral-mini-transcribe-realtime-2602",
                    "translation": "mistral-medium-latest",
                    "speech": "voxtral-mini-tts-2603",
                },
                "deployment_name": "mistral-private-deployment",
                "latency_presets": LATENCY_PRESETS,
                "voice_preview_text": VOICE_PREVIEW_TEXT,
            }
        ),
    )
    page.goto(origin)
    page.wait_for_function(
        "document.getElementById('voice-test-text').textContent.length > 0"
    )
    assert page.locator(".technical, .history, .voice-guidance, .steps").count() == 0
    markup_text = page.locator("body").text_content()
    for removed in [
        "I am recording my own voice and consent",
        "Save one voice for all languages, or create separate profiles",
        "Fictional sample text. Not an official Acme Corp statement",
        "Sound like yourself.",
        "Samples and profiles are saved locally.",
    ]:
        assert removed not in markup_text
    emblem = page.locator(".european-emblem")
    assert emblem.is_visible()
    page.wait_for_function(
        "document.querySelector('.european-emblem').naturalWidth === 108"
    )
    svg = page.request.get(origin + "/european-emblem.svg")
    assert svg.status == 200
    assert svg.text().count('<use href="#star"') == 12
    assert page.locator('link[rel="icon"]').get_attribute("href") == (
        "/european-emblem.svg"
    )
    assert (
        page.locator("#start-live").evaluate(
            "element => getComputedStyle(element).backgroundColor"
        )
        == "rgb(0, 51, 153)"
    )
    assert (
        page.locator(".sidebar").evaluate(
            "element => getComputedStyle(element).backgroundColor"
        )
        == "rgb(0, 43, 127)"
    )
    assert (
        page.locator(".topbar").evaluate(
            "element => getComputedStyle(element).borderTopColor"
        )
        == "rgb(255, 204, 0)"
    )
    assert "Configure the API key" in page.locator("#notice").inner_text()
    for screen in ["live", "voices", "intelligence"]:
        page.locator(f"[data-page={screen}]").click()
        content = page.locator("body").inner_text().lower()
        if screen == "voices":
            content = content.replace(VOICE_PREVIEW_TEXT.lower(), "")
        assert "mistral" not in content
        assert "voxtral" not in content
        for removed in ["presales", "prototype", "demo", "not an official eu service"]:
            assert removed not in content
        assert page.locator("footer, .topbar .badge").count() == 0
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("#voice-test-text").inner_text() == VOICE_PREVIEW_TEXT
    for viewport_width, gap in [(1280, 32), (390, 24)]:
        page.set_viewport_size({"width": viewport_width, "height": 800})
        brand = page.locator(".brand").bounding_box()
        navigation = page.locator(".sidebar nav").bounding_box()
        assert navigation["y"] - (brand["y"] + brand["height"]) == pytest.approx(gap)
        assert page.locator(".nav-button").count() == 3
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [1280, 900, 390])
def test_voice_cards_are_separate_and_aligned(browser_page, width):
    page, origin = browser_page
    page.set_viewport_size({"width": width, "height": 900})
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": []}))
    page.goto(origin)
    page.locator("[data-page=voices]").click()
    expect(page.locator("#voice-list")).to_contain_text("Your voice library is empty")
    create = page.locator(".capture-panel")
    preview = page.locator(".voice-test")
    library = page.locator(".voice-library")
    assert create.locator(".voice-test").count() == 0
    assert preview.locator("#test-voice, #voice-test-select").count() == 2
    assert library.locator("#voice-list, #clear-voices").count() == 2
    styles = [
        card.evaluate(
            "element => { const s = getComputedStyle(element); return [s.backgroundColor, s.padding, s.borderRadius, s.borderColor]; }"
        )
        for card in [create, preview, library]
    ]
    assert styles[0] == styles[1] == styles[2]
    assert styles[0][0] == "rgb(255, 255, 255)"
    create_box, preview_box, library_box = [
        card.bounding_box() for card in [create, preview, library]
    ]
    gap = 20 if width <= 760 else 25
    assert preview_box["x"] == pytest.approx(create_box["x"])
    assert preview_box["width"] == pytest.approx(create_box["width"])
    assert preview_box["y"] - create_box["y"] - create_box["height"] == pytest.approx(
        gap
    )
    if width > 1050:
        assert library_box["y"] == pytest.approx(create_box["y"])
        assert library_box["x"] - create_box["x"] - create_box[
            "width"
        ] == pytest.approx(gap)
        assert library.locator("h2").bounding_box()["y"] == pytest.approx(
            create.locator("h2").bounding_box()["y"]
        )
    else:
        assert library_box["x"] == pytest.approx(create_box["x"])
        assert library_box["width"] == pytest.approx(create_box["width"])
        assert library_box["y"] - preview_box["y"] - preview_box[
            "height"
        ] == pytest.approx(gap)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("source", ["microphone", "tab", "loopback"])
def test_voice_save_validation_retry_and_success(browser_page, source):
    page, origin = browser_page
    requests = []
    voices = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def voice_api(route):
        if route.request.method == "GET":
            route.fulfill(json={"voices": voices})
            return
        raw = route.request.post_data_buffer
        content_type = route.request.headers["content-type"]
        multipart = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + raw
        )
        fields = {
            part.get_param("name", header="content-disposition"): part.get_payload(
                decode=True
            )
            for part in multipart.iter_parts()
        }
        requests.append(fields)
        if len(requests) == 1:
            route.fulfill(status=502, json={"detail": "Workflow unavailable."})
            return
        voice = {
            "id": "a" * 32,
            "name": fields["name"].decode(),
            "languages": json.loads(fields["languages"]),
            "created_at": "2026-10-15T13:00:00Z",
            "sample_url": "/api/voices/test/sample",
        }
        voices.append(voice)
        route.fulfill(status=201, json=voice)

    page.route("**/api/voices", voice_api)
    page.goto(origin)
    page.locator("[data-page=voices]").click()
    assert page.locator("#test-voice").is_disabled()
    assert page.locator("#voice-test-text").inner_text() == VOICE_PREVIEW_TEXT
    assert "FICTIONAL TEXT" in page.locator(".voice-test").inner_text()
    assert page.locator("#voices-page input[type=checkbox]").count() == 0
    assert page.locator("#voice-source-hint").count() == 0
    assert (
        "For Teams, obtain the speaker’s permission first"
        not in page.locator("#voices-page").inner_text()
    )
    page.select_option("#voice-source", source)
    if source == "tab":
        page.evaluate("""() => {
            navigator.mediaDevices.getDisplayMedia = async options => {
                window.displayOptions = options;
                const context = new AudioContext();
                const destination = context.createMediaStreamDestination();
                const oscillator = context.createOscillator();
                oscillator.connect(destination); oscillator.start();
                const video = document.createElement('canvas').captureStream().getVideoTracks()[0];
                window.sharedStream = new MediaStream([...destination.stream.getTracks(), video]);
                return sharedStream;
            };
            navigator.mediaDevices.getUserMedia = async () => { throw new Error('Unexpected microphone capture'); };
        }""")
    elif source == "loopback":
        page.locator("#refresh-voice-inputs").click()
        page.wait_for_function(
            "!document.getElementById('refresh-voice-inputs').disabled"
        )
        page.locator("#voice-input").select_option(index=1)
        page.evaluate("""() => {
            const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
            navigator.mediaDevices.getUserMedia = async options => {
                window.inputOptions = options;
                return getUserMedia(options);
            };
        }""")
    page.evaluate(
        """async () => {
            const { AudioRecorder } = await import('/audio-recorder.js');
            const start = AudioRecorder.prototype.start;
            AudioRecorder.prototype.start = async function(options) {
                await start.call(this, options);
                window.testRecorder = this;
                this.capture(new Float32Array(this.context.sampleRate * 4).fill(0.25));
            };
        }"""
    )
    page.locator("#record-voice").click()
    page.wait_for_function("window.testRecorder?.accepting")
    assert page.locator("#voice-source").is_disabled()
    expect(page.locator("#voice-capture-status")).to_contain_text("Audio detected")
    if source == "tab":
        assert page.evaluate("displayOptions.audio.echoCancellation") is False
    elif source == "loopback":
        assert page.evaluate("inputOptions.audio.echoCancellation") is False
        assert page.evaluate("inputOptions.audio.noiseSuppression") is False
        assert page.evaluate("inputOptions.audio.autoGainControl") is False
        assert page.evaluate("inputOptions.audio.deviceId.exact")
    else:
        assert page.evaluate("testRecorder.source") == "microphone"
    page.locator("#record-voice").click()
    page.wait_for_function("!document.getElementById('save-voice').disabled")

    page.locator("#save-voice").click()
    assert not requests
    assert page.locator("#voice-name").get_attribute("aria-invalid") == "true"
    assert page.locator("#voice-name").evaluate(
        "element => element === document.activeElement"
    )
    assert "Enter a voice name" in page.locator("#voice-save-status").inner_text()

    page.fill("#voice-name", "French voice")
    page.select_option("#voice-scope", "fr")
    page.locator("#save-voice").click()
    page.wait_for_function(
        "document.getElementById('voice-save-status').dataset.state === 'error'"
    )
    status = page.locator("#voice-save-status")
    assert "Workflow unavailable" in status.inner_text()
    assert "recording is kept" in status.inner_text()
    assert page.locator("#save-voice").is_enabled()
    assert page.locator("#voice-preview").is_visible()
    assert status.evaluate(
        "element => { const rect = element.getBoundingClientRect(); return rect.top >= 0 && rect.bottom <= innerHeight; }"
    )

    page.locator("#save-voice").click()
    page.wait_for_selector(".voice-item")
    assert len(requests) == 2
    assert requests[1]["name"] == b"French voice"
    assert requests[1]["languages"] == b'["fr"]'
    assert "consent" not in requests[1]
    read_wav(requests[1]["sample"], max_seconds=30, min_seconds=3)
    assert requests[0]["sample"] == requests[1]["sample"]
    assert status.get_attribute("data-state") == "success"
    assert "French voice” saved" in status.inner_text()
    assert page.locator(".voice-item strong").inner_text() == "French voice"
    assert page.locator("#save-voice").is_disabled()
    assert page.locator("#record-voice").is_enabled()
    assert page.locator("#voice-test-select").input_value() == voices[0]["id"]
    assert page.locator("#test-voice").is_enabled()

    previews = []

    def preview_api(route):
        previews.append(route.request)
        if len(previews) == 1:
            route.fulfill(status=502, json={"detail": "Speech unavailable."})
        else:
            route.fulfill(
                json={"url": "/api/audio/preview.mp3", "voice_id": voices[0]["id"]}
            )

    page.route("**/api/voices/*/preview", preview_api)
    page.route(
        "**/api/audio/preview.mp3",
        lambda route: route.fulfill(content_type="audio/wav", body=wav()),
    )
    page.locator("#test-voice").click()
    page.wait_for_function(
        "document.getElementById('voice-test-status').dataset.state === 'error'"
    )
    assert "Speech unavailable" in page.locator("#voice-test-status").inner_text()
    assert "saved voice is kept" in page.locator("#voice-test-status").inner_text()
    assert page.locator("#test-voice").is_enabled()
    page.locator("#test-voice").click()
    page.wait_for_function(
        "document.getElementById('voice-test-status').dataset.state === 'success'"
    )
    assert len(previews) == 2
    assert previews[-1].method == "POST"
    assert previews[-1].url.endswith(f"/api/voices/{voices[0]['id']}/preview")
    assert page.locator("#voice-test-audio").is_visible()
    page.wait_for_function(
        "document.getElementById('voice-test-audio').readyState >= 2"
    )
    assert page.locator("#voice-test-audio").get_attribute("src") == (
        "/api/audio/preview.mp3"
    )
    page.wait_for_function("!document.getElementById('test-voice').disabled")
    assert not errors


def test_voice_preview_selection_and_navigation_ignore_stale_audio(browser_page):
    page, origin = browser_page
    voices = [
        {
            "id": letter * 32,
            "name": name,
            "languages": [scope],
            "created_at": "2026-10-15T13:00:00Z",
            "sample_url": "/api/voices/test/sample",
        }
        for letter, name, scope in [
            ("a", "French voice", "fr"),
            ("b", "English voice", "en"),
        ]
    ]
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": voices}))
    pending = []
    page.route("**/api/voices/*/preview", lambda route: pending.append(route))
    page.goto(origin)
    page.locator("[data-page=voices]").click()
    page.select_option("#voice-test-select", voices[1]["id"])
    page.locator("#test-voice").click()
    page.wait_for_function("document.getElementById('test-voice').disabled")
    assert page.locator("#voice-test-select").is_disabled()
    assert "Generating" in page.locator("#voice-test-status").inner_text()
    assert len(pending) == 1
    assert pending[0].request.url.endswith(f"/api/voices/{voices[1]['id']}/preview")

    page.locator("[data-page=live]").click()
    with page.expect_response("**/api/voices/*/preview"):
        pending[0].fulfill(
            json={"url": "/api/audio/stale.mp3", "voice_id": voices[1]["id"]}
        )
    page.locator("[data-page=voices]").click()
    assert page.locator("#voice-test-audio").is_hidden()
    assert page.locator("#voice-test-audio").get_attribute("src") is None
    assert page.locator("#test-voice").is_enabled()
    assert "Ready to test" in page.locator("#voice-test-status").inner_text()


def test_clear_voice_library_confirmation_progress_and_retry(browser_page):
    fail_first = True
    page, origin = browser_page
    voices = [
        {
            "id": letter * 32,
            "name": name,
            "languages": ["all"],
            "created_at": "2026-10-15T13:00:00Z",
            "sample_url": "/api/voices/test/sample",
        }
        for letter, name in [("a", "First voice"), ("b", "Second voice")]
    ]
    identifiers = [voice["id"] for voice in voices]
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": voices}))
    pending = []
    page.route("**/api/voices/*", lambda route: pending.append(route))
    previews = []
    page.route("**/api/voices/*/preview", lambda route: previews.append(route))
    page.goto(origin)
    page.wait_for_selector(".voice-item", state="attached")
    page.select_option("#voice-fr", identifiers[0])
    page.evaluate(
        """() => {
            document.getElementById('source-text').textContent = 'Keep this transcript';
            document.getElementById('text-fr').textContent = 'Keep this translation';
        }"""
    )
    page.locator("[data-page=voices]").click()
    clear = page.locator("#clear-voices")
    assert clear.is_enabled()
    dialogs = []

    def dismiss_clear(dialog):
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.once("dialog", dismiss_clear)
    clear.click()
    assert "all 2 cloned voices" in dialogs[0]
    assert "cannot be undone" in dialogs[0]
    assert not pending
    assert page.locator(".voice-item").count() == 2

    page.locator("#test-voice").click()
    page.wait_for_function("document.getElementById('test-voice').disabled")
    assert len(previews) == 1
    assert clear.is_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    clear.click()
    page.wait_for_function("document.getElementById('clear-voices').disabled")
    assert "Deleting voice 1 of 2" in page.locator("#voice-library-status").inner_text()
    assert page.locator("#record-voice").is_disabled()
    assert page.locator("#save-voice").is_disabled()
    assert page.locator("#test-voice").is_disabled()
    assert page.locator("#start-live").is_disabled()
    assert page.locator(".voice-item button:enabled").count() == 0
    assert len(pending) == 1
    assert pending[0].request.method == "DELETE"
    assert pending[0].request.url.endswith(identifiers[0])

    with page.expect_response("**/api/voices/*/preview"):
        previews[0].fulfill(json={"url": "/api/audio/stale.mp3"})
    assert page.locator("#voice-test-audio").get_attribute("src") is None
    assert page.locator("#voice-test-audio").is_hidden()
    if fail_first:
        pending[0].fulfill(status=502, json={"detail": "Remote deletion failed."})
    else:
        voices.pop(0)
        pending[0].fulfill(status=204)
    page.wait_for_function(
        "document.getElementById('voice-library-status').textContent.includes('voice 2 of 2')"
    )
    assert len(pending) == 2
    assert pending[1].request.url.endswith(identifiers[1])
    voices[:] = [voice for voice in voices if voice["id"] != identifiers[1]]
    pending[1].fulfill(status=204)
    page.wait_for_function(
        "document.getElementById('voice-library-status').dataset.state !== 'info'"
    )
    status = page.locator("#voice-library-status")
    assert page.locator("#record-voice").is_enabled()
    assert page.locator("#start-live").is_enabled()
    if fail_first:
        assert status.get_attribute("data-state") == "error"
        assert "Deleted 1 of 2" in status.inner_text()
        assert "First voice”" in status.inner_text()
        assert "Remote deletion failed" in status.inner_text()
        assert page.locator(".voice-item strong").inner_text() == "First voice"
        assert page.locator("#test-voice").is_enabled()
        assert page.locator("#voice-fr").input_value() == identifiers[0]
        assert clear.is_enabled()
        page.once("dialog", lambda dialog: dialog.accept())
        clear.click()
        page.wait_for_function("document.getElementById('clear-voices').disabled")
        assert len(pending) == 3
        assert pending[2].request.url.endswith(identifiers[0])
        voices.clear()
        pending[2].fulfill(status=204)
    page.wait_for_function(
        "document.getElementById('voice-library-status').dataset.state === 'success'"
    )
    assert "Voice library cleared" in status.inner_text()
    assert clear.is_disabled()
    assert page.locator(".voice-item").count() == 0
    assert "library is empty" in page.locator("#voice-list").inner_text()
    assert page.locator("#test-voice").is_disabled()
    assert page.locator("#voice-test-select").is_disabled()
    assert page.locator("#voice-fr").input_value() == ""
    assert page.locator("#source-text").text_content() == "Keep this transcript"
    assert page.locator("#text-fr").text_content() == "Keep this translation"
    assert page.locator("#session-history").count() == 0
    page.reload()
    page.locator("[data-page=voices]").click()
    assert page.locator("#clear-voices").is_disabled()
    assert page.locator(".voice-item").count() == 0


@pytest.mark.parametrize("ending", ["complete", "error"])
def test_preview_stream_starts_audio_before_completion_and_stops_safely(
    browser_page, ending
):
    page, origin = browser_page
    voice = {
        "id": "a" * 32,
        "name": "Preview voice",
        "languages": ["all"],
        "created_at": "2026-10-15T13:00:00Z",
        "sample_url": "/api/voices/test/sample",
    }
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": [voice]}))
    requests = []

    def preview(route):
        requests.append(route.request)
        route.fulfill(json={"stream_url": "/offline-preview-stream", "cached": False})

    page.route("**/api/voices/*/preview", preview)
    page.route(
        "**/api/audio/cached.wav",
        lambda route: route.fulfill(content_type="audio/wav", body=wav()),
    )
    page.goto(origin)
    page.evaluate("""async () => {
        const { VoicePreviewPlayer } = await import('/voice-preview-player.js');
        const clear = VoicePreviewPlayer.prototype.clear;
        VoicePreviewPlayer.prototype.clear = function() {
            window.previewPlayer = this;
            return clear.call(this);
        };
        const fetchOriginal = window.fetch;
        window.fetch = async (url, options) => {
            if (url !== '/offline-preview-stream') return fetchOriginal(url, options);
            window.streamFetches = (window.streamFetches || 0) + 1;
            const stream = new ReadableStream({ start(controller) { window.previewStream = controller; } });
            return new Response(stream);
        };
        window.pushPreview = event => previewStream.enqueue(new TextEncoder().encode(JSON.stringify(event) + String.fromCharCode(10)));
    }""")
    page.locator("[data-page=voices]").click()
    page.locator("#test-voice").click()
    page.wait_for_function("window.previewStream !== undefined")
    page.evaluate("""() => {
        const data = new Float32Array(2400).fill(0.25);
        const bytes = new Uint8Array(data.buffer);
        pushPreview({type:'audio',sample_rate:24000,audio_data:btoa(String.fromCharCode(...bytes))});
    }""")
    page.wait_for_function("window.previewPlayer.sources.size > 0")
    assert "as audio arrives" in page.locator("#voice-test-status").inner_text()
    assert page.locator("#voice-test-audio").is_hidden()
    page.locator("#voice-test-volume").fill("40")
    page.locator("#voice-test-volume").dispatch_event("input")
    assert page.evaluate("previewPlayer.gain.gain.value") == pytest.approx(0.4)
    if ending == "stop":
        page.locator("#stop-voice-test").click()
        assert page.evaluate("previewPlayer.sources.size") == 0
        assert page.locator("#voice-test-audio").get_attribute("src") is None
        assert page.locator("#test-voice").is_enabled()
        return
    if ending == "error":
        page.evaluate(
            "pushPreview({type:'error',message:'Speech failed.'}); previewStream.close()"
        )
        page.wait_for_function(
            "document.getElementById('voice-test-status').dataset.state === 'error'"
        )
        assert page.evaluate("previewPlayer.sources.size") == 0
        assert "saved voice is kept" in page.locator("#voice-test-status").inner_text()
        return
    page.evaluate(
        "pushPreview({type:'done',url:'/api/audio/cached.wav'}); previewStream.close()"
    )
    page.wait_for_function(
        "document.getElementById('voice-test-status').dataset.state === 'success'"
    )
    assert (
        page.locator("#voice-test-audio").get_attribute("src")
        == "/api/audio/cached.wav"
    )
    assert page.locator("#stop-voice-test").is_hidden()
    page.route(
        "**/api/voices/*/preview",
        lambda route: route.fulfill(
            json={"url": "/api/audio/cached.wav", "cached": True}
        ),
    )
    page.locator("#test-voice").click()
    page.wait_for_function(
        "document.getElementById('voice-test-status').textContent.includes('from cache')"
    )
    assert page.evaluate("streamFetches") == 1
    assert page.locator("#voice-test-audio").is_visible()


@pytest.fixture
def offline_page(browser_page):
    page, origin = browser_page
    errors = []
    unexpected = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def local_only(route):
        url = urlsplit(route.request.url)
        if url.netloc != urlsplit(origin).netloc or (
            url.path.startswith("/api/")
            and url.path not in {"/api/config", "/api/voices", "/api/sessions"}
        ):
            unexpected.append(route.request.url)
            route.abort()
        else:
            route.fallback()

    def reject_socket(socket):
        unexpected.append(socket.url)
        socket.close()

    page.route("**/*", local_only)
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": []}))
    page.route_web_socket("**/*", reject_socket)
    yield page, origin
    assert not unexpected, f"Unexpected network requests: {unexpected}"
    assert not errors, f"Browser errors: {errors}"


def sample_pcm(count):
    # Exactly representable floats in JS, with changing samples at every boundary.
    return struct.pack(
        f"<{count}h", *((index * 73) % 60001 - 30000 for index in range(count))
    )


def test_config_populates_latency_options_and_status(offline_page):
    page, origin = offline_page
    labels = [
        "Responsive · 200 ms audio updates",
        "Balanced · 1 s audio updates",
        "More context · 2 s audio updates",
    ]
    markup = (PROJECT_ROOT / "frontend/index.html").read_text()
    for label in labels:
        markup = markup.replace(label, "Stale preset")
    assert markup.count("Stale preset") == 3
    page.route(
        origin + "/",
        lambda route: route.fulfill(content_type="text/html", body=markup),
    )
    page.goto(origin)
    expect(page.locator("#voice-test-text")).not_to_be_empty()
    assert page.locator("#latency option").evaluate_all(
        "options => options.map(option => [option.value, option.textContent])"
    ) == [[preset["id"], label] for preset, label in zip(LATENCY_PRESETS, labels)]
    expect(page.locator("#latency")).to_have_value("balanced")
    expect(page.locator("#queue-status")).to_have_text(
        "1 s audio updates + model processing"
    )
    for preset, duration in zip(LATENCY_PRESETS, ["200 ms", "1 s", "2 s"]):
        page.select_option("#latency", preset["id"])
        expect(page.locator("#queue-status")).to_have_text(
            f"{duration} audio updates + model processing"
        )
    expect(page.locator("#notice")).to_be_hidden()


def test_capture_exact_cadence_and_lossless_tail(offline_page):
    preset, tail_samples = LATENCY_PRESETS[0], 800
    page, origin = offline_page
    page.goto(origin)
    chunk_samples = int(preset["chunk_seconds"] * 16000)
    result = page.evaluate(
        """async ({ seconds, chunkSamples, tailSamples }) => {
            const { AudioRecorder } = await import('/audio-recorder.js');
            const blobs = [], checkpoints = [];
            const recorder = new AudioRecorder({
                seconds, onChunk: blob => blobs.push(blob),
            });
            recorder.context = { sampleRate: 16000, state: 'closed' };
            recorder.capture(new Float32Array(3200).fill(0.25));
            const beforeReady = blobs.length;
            recorder.accepting = true;
            let offset = 0;
            // Land immediately before/on a boundary, then cross two in one call.
            for (const length of [chunkSamples - 1, 1, 37,
                                  2 * chunkSamples - 38, 1, tailSamples]) {
                const samples = Float32Array.from({ length }, (_, i) =>
                    (((offset + i) * 73) % 60001 - 30000) / 32768);
                recorder.capture(samples);
                offset += length;
                checkpoints.push({
                    samples: offset, chunks: blobs.length,
                    buffered: recorder.samples.length,
                });
            }
            const tail = await recorder.stop();
            recorder.capture(new Float32Array(3200).fill(0.5));
            const duplicate = recorder.flush();
            return {
                checkpoints, beforeReady, tailSize: tail?.size,
                accepting: recorder.accepting,
                buffered: recorder.samples.length,
                duplicate: duplicate !== null,
                chunks: await Promise.all(blobs.map(async blob => ({
                    type: blob.type,
                    bytes: Array.from(new Uint8Array(await blob.arrayBuffer())),
                }))),
            };
        }""",
        {
            "seconds": preset["chunk_seconds"],
            "chunkSamples": chunk_samples,
            "tailSamples": tail_samples,
        },
    )
    assert result["beforeReady"] == 0
    for checkpoint in result["checkpoints"]:
        assert checkpoint["chunks"] == checkpoint["samples"] // chunk_samples
        assert checkpoint["buffered"] == checkpoint["samples"] % chunk_samples
    assert [checkpoint["chunks"] for checkpoint in result["checkpoints"]] == [
        0,
        1,
        1,
        2,
        3,
        3,
    ]
    sizes = [44 + 2 * chunk_samples] * 3 + [44 + 2 * tail_samples]
    assert [len(chunk["bytes"]) for chunk in result["chunks"]] == sizes
    assert result["tailSize"] == sizes[-1]
    assert result["accepting"] is False
    assert result["buffered"] == 0
    assert result["duplicate"] is False
    pcm = []
    for chunk in result["chunks"]:
        assert chunk["type"] == "audio/wav"
        samples, rate = read_wav(
            bytes(chunk["bytes"]), max_seconds=3, min_seconds=1 / 16000
        )
        assert rate == 16000
        pcm.append(samples)
    assert [len(samples) / 32000 for samples in pcm[:3]] == [
        preset["chunk_seconds"]
    ] * 3
    assert len(pcm[-1]) / 32000 == tail_samples / 16000
    assert b"".join(pcm) == sample_pcm(3 * chunk_samples + tail_samples)


@pytest.mark.parametrize(
    "failure", ["no-audio", "window", "denied", "unsupported", "setup"]
)
def test_tab_capture_rejects_missing_audio_and_cleans_up(offline_page, failure):
    page, origin = offline_page
    page.goto(origin)
    page.locator("[data-page=voices]").click()
    page.select_option("#voice-source", "tab")
    page.evaluate(
        """failure => {
        window.captureCalls = 0;
        navigator.mediaDevices.getUserMedia = async () => { throw new Error('Unexpected microphone fallback'); };
        navigator.mediaDevices.getDisplayMedia = async () => {
            captureCalls++;
            if (failure === 'denied') throw new DOMException('Denied', 'NotAllowedError');
            const video = document.createElement('canvas').captureStream().getVideoTracks()[0];
            video.getSettings = () => ({ displaySurface: failure === 'window' ? 'window' : 'browser' });
            window.sharedStream = new MediaStream([video]);
            if (failure === 'setup') {
                const context = new AudioContext();
                sharedStream.addTrack(context.createMediaStreamDestination().stream.getAudioTracks()[0]);
                AudioWorklet.prototype.addModule = async () => { throw new Error('Worklet setup failed'); };
            }
            return sharedStream;
        };
        if (failure === 'unsupported') navigator.mediaDevices.getDisplayMedia = undefined;
    }""",
        failure,
    )
    page.locator("#record-voice").click()
    expected = {
        "no-audio": "No shared audio",
        "window": "Select the Teams browser tab",
        "denied": "Sharing was cancelled or denied",
        "unsupported": "Tab audio sharing is unavailable",
        "setup": "Worklet setup failed",
    }[failure]
    expect(page.locator("#voice-capture-status")).to_contain_text(expected)
    expect(page.locator("#save-voice")).to_be_disabled()
    expect(page.locator("#record-voice")).to_be_enabled()
    expect(page.locator("#voice-source")).to_be_enabled()
    assert page.evaluate(
        "!window.sharedStream || sharedStream.getTracks().every(track => track.readyState === 'ended')"
    )
    assert page.evaluate("captureCalls") == (0 if failure == "unsupported" else 1)


@pytest.mark.parametrize(
    "ending", ["manual", "audio-ended", "video-ended", "limit", "silent", "clipped"]
)
def test_tab_sample_shutdown_quality_and_source_reset(offline_page, ending):
    page, origin = offline_page
    page.goto(origin)
    page.locator("[data-page=voices]").click()
    page.select_option("#voice-source", "tab")
    page.evaluate(
        """async ending => {
        navigator.mediaDevices.getDisplayMedia = async () => {
            const context = new AudioContext();
            const destination = context.createMediaStreamDestination();
            const video = document.createElement('canvas').captureStream().getVideoTracks()[0];
            video.getSettings = () => ({ displaySurface: 'browser' });
            window.sharedStream = new MediaStream([...destination.stream.getTracks(), video]);
            return sharedStream;
        };
        const { AudioRecorder } = await import('/audio-recorder.js');
        const start = AudioRecorder.prototype.start;
        AudioRecorder.prototype.start = async function(options) {
            await start.call(this, options);
            window.testRecorder = this;
            // Deterministic signal injection; the browser stream itself stays silent.
            const value = ending === 'silent' ? 0 : ending === 'clipped' ? 1 : 0.25;
            this.capture(new Float32Array(this.context.sampleRate * 4).fill(value));
        };
    }""",
        ending,
    )
    page.locator("#record-voice").click()
    expect(page.locator("#record-voice")).to_have_text("Stop sample")
    assert page.evaluate("testRecorder.input.mediaStream.getVideoTracks().length") == 0
    page.locator("[data-page=intelligence]").click()
    expect(page.locator("#notice")).to_contain_text("Stop the voice sample")
    expect(page.locator("#voices-page")).to_be_visible()
    if ending in {"audio-ended", "video-ended"}:
        page.evaluate(
            """kind => {
            const track = sharedStream.getTracks().find(track => track.kind === kind);
            track.stop(); track.dispatchEvent(new Event('ended'));
        }""",
            "audio" if ending == "audio-ended" else "video",
        )
    elif ending == "limit":
        page.evaluate(
            "testRecorder.capture(new Float32Array(testRecorder.context.sampleRate * 30).fill(0.25))"
        )
    else:
        page.locator("#record-voice").click()
    expect(page.locator("#record-voice")).to_have_text("Record again")
    assert page.evaluate(
        "sharedStream.getTracks().every(track => track.readyState === 'ended')"
    )
    assert page.evaluate("testRecorder.context.state") == "closed"
    assert page.evaluate("testRecorder.accepting") is False
    if ending == "limit":
        assert page.evaluate("testRecorder.capturedSamples") == 29 * 16000
    expect(page.locator("#voice-preview")).to_be_visible()
    if ending in {"silent", "clipped"}:
        expect(page.locator("#save-voice")).to_be_disabled()
        expect(page.locator("#voice-capture-status")).to_contain_text(
            "No usable audio" if ending == "silent" else "heavily clipped"
        )
    else:
        expect(page.locator("#save-voice")).to_be_enabled()
        expect(page.locator("#voice-capture-status")).to_contain_text("Sample captured")
    page.select_option("#voice-source", "microphone")
    expect(page.locator("#save-voice")).to_be_disabled()
    expect(page.locator("#voice-preview")).to_be_hidden()
    assert page.locator("#voice-preview").get_attribute("src") is None


def test_tab_audio_reaches_worklet_from_stereo_right_channel(offline_page):
    page, origin = offline_page
    page.goto(origin)
    result = page.evaluate("""async () => {
        const { AudioRecorder } = await import('/audio-recorder.js');
        const source = new AudioContext();
        await source.resume();
        const oscillator = source.createOscillator();
        const gain = source.createGain(); gain.gain.value = 0.2;
        const merger = source.createChannelMerger(2);
        const destination = source.createMediaStreamDestination();
        oscillator.connect(gain); gain.connect(merger, 0, 1); merger.connect(destination);
        oscillator.start();
        const video = document.createElement('canvas').captureStream().getVideoTracks()[0];
        const stream = new MediaStream([...destination.stream.getTracks(), video]);
        navigator.mediaDevices.getDisplayMedia = async () => stream;
        const recorder = new AudioRecorder({ source: 'tab' });
        try {
            await recorder.start();
            await new Promise((resolve, reject) => {
                const timeout = setTimeout(() => reject(new Error('Right-channel audio did not reach capture')), 3000);
                recorder.onLevel = () => {
                    if (recorder.audibleSamples >= 1600) { clearTimeout(timeout); resolve(); }
                };
            });
            const blob = await recorder.stop();
            return {
                size: blob.size, audible: recorder.audibleSamples,
                rms: Math.sqrt(recorder.signalEnergy / recorder.capturedSamples),
                stopped: stream.getTracks().every(track => track.readyState === 'ended'),
            };
        } finally {
            await recorder.stop(false); oscillator.stop(); await source.close();
        }
    }""")
    assert result["size"] >= 3244
    assert result["audible"] >= 1600
    assert result["rms"] > 0.01
    assert result["stopped"] is True


def test_loopback_device_listing_releases_permission_tracks(offline_page):
    page, origin = offline_page
    page.goto(origin)
    page.locator("[data-page=voices]").click()
    page.select_option("#voice-source", "loopback")
    page.evaluate("""() => {
        const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        navigator.mediaDevices.getUserMedia = async options => {
            window.permissionStream = await getUserMedia(options);
            return permissionStream;
        };
    }""")
    page.locator("#refresh-voice-inputs").click()
    expect(page.locator("#voice-capture-status")).to_contain_text(
        "Choose the loopback device"
    )
    assert page.locator("#voice-input option").count() > 1
    assert page.evaluate(
        "permissionStream.getTracks().every(track => track.readyState === 'ended')"
    )
    page.locator("#record-voice").click()
    expect(page.locator("#voice-capture-status")).to_contain_text(
        "Select your loopback audio input first"
    )
    expect(page.locator("#save-voice")).to_be_disabled()
    expect(page.locator("#record-voice")).to_be_enabled()


def test_voice_sample_quality_limits_are_deterministic(offline_page):
    page, origin = offline_page
    page.goto(origin)
    result = page.evaluate("""async () => {
        const { AudioRecorder } = await import('/audio-recorder.js');
        const problems = [];
        for (const [length, value] of [[47999, 0.25], [48000, 0], [48000, 0.0001], [48000, 1], [48000, 0.25]]) {
            const recorder = new AudioRecorder();
            recorder.context = { sampleRate: 16000, state: 'closed' };
            recorder.accepting = true;
            recorder.capture(new Float32Array(length).fill(value));
            problems.push(recorder.sampleProblem());
        }
        return problems;
    }""")
    assert "at least three seconds" in result[0]
    assert "No usable audio" in result[1]
    assert "No usable audio" in result[2]
    assert "heavily clipped" in result[3]
    assert result[4] is None


def test_non_live_voice_sample_keeps_100ms_minimum(offline_page):
    for samples in [1599, 1600]:
        page, origin = offline_page
        page.goto(origin)
        result = page.evaluate(
            """async length => {
                const { AudioRecorder } = await import('/audio-recorder.js');
                const blobs = [];
                const recorder = new AudioRecorder({ onChunk: blob => blobs.push(blob) });
                recorder.context = { sampleRate: 16000, state: 'closed' };
                recorder.accepting = true;
                recorder.capture(new Float32Array(length).fill(0.25));
                const beforeStop = blobs.length;
                const tail = await recorder.stop();
                return {
                    beforeStop, buffered: recorder.samples.length,
                    tail: tail ? Array.from(new Uint8Array(await tail.arrayBuffer())) : null,
                    chunks: blobs.length,
                };
            }""",
            samples,
        )
        assert result["beforeStop"] == 0
        assert result["buffered"] == 0
        if samples < 1600:
            assert result["tail"] is None
            assert result["chunks"] == 0
        else:
            assert result["chunks"] == 1
            assert len(result["tail"]) == 3244
            pcm, rate = read_wav(bytes(result["tail"]), max_seconds=3)
            assert rate == 16000
            assert pcm == struct.pack("<h", 8192) * samples


@pytest.mark.parametrize("width", [1280, 390])
def test_full_transcript_retains_completed_phrases(browser_page, width):
    page, origin = browser_page
    page.set_viewport_size({"width": width, "height": 900})
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": []}))
    page.route(
        "**/api/sessions",
        lambda route: route.fulfill(
            status=201,
            json={"id": "test-session"}
            if route.request.method == "POST"
            else {"sessions": []},
        ),
    )
    sockets = []
    page.route_web_socket(
        "**/api/sessions/*/live", lambda socket: sockets.append(socket)
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(origin)
    page.evaluate("""async () => {
        const { AudioRecorder } = await import('/audio-recorder.js');
        AudioRecorder.prototype.start = async function() { this.accepting = true; };
        AudioRecorder.prototype.stop = async function() { this.accepting = false; };
    }""")
    for language in ["de", "fr", "es", "it"]:
        card = page.locator(f"#card-{language}")
        assert card.locator(".model-label").count() == 0
        assert card.locator(".language-options").evaluate(
            "row => [...row.children].map(element => element.tagName)"
        ) == ["SELECT", "BUTTON", "LABEL"]
        assert (
            page.locator(f"#play-{language}").evaluate(
                "button => getComputedStyle(button).backgroundColor"
            )
            == "rgb(233, 242, 255)"
        )
        assert page.locator(f"#replay-status-{language}").evaluate(
            "hint => [getComputedStyle(hint).fontSize, getComputedStyle(hint).fontStyle]"
        ) == ["10px", "italic"]
    page.locator("#start-live").click()
    page.wait_for_function(
        "document.getElementById('session-status').textContent === 'Recording live'"
    )
    assert len(sockets) == 1

    def transcript(index, text, final):
        sockets[0].send(
            json.dumps(
                {
                    "type": "transcript",
                    "segment_index": index,
                    "text": text,
                    "final": final,
                }
            )
        )

    transcript(0, "First phrase", False)
    transcript(0, "First phrase completed.", True)
    transcript(1, "Second phrase", False)
    page.wait_for_function(
        "document.querySelectorAll('#source-text p[data-segment]').length === 2"
    )
    source = page.locator("#source-text")
    assert source.locator("p").all_text_contents() == [
        "First phrase completed.",
        "Second phrase",
    ]
    transcript(1, "Second phrase completed.", True)
    transcript(0, "", False)
    transcript(1, "Late partial must not replace completed text", False)
    transcript(1, "", True)
    transcript(3, "Fourth phrase completed.", True)
    transcript(2, "Third phrase completed.", True)
    page.wait_for_function(
        "document.querySelectorAll('#source-text p[data-segment]').length === 4"
    )
    assert source.locator("p").all_text_contents() == [
        "First phrase completed.",
        "Second phrase completed.",
        "Third phrase completed.",
        "Fourth phrase completed.",
    ]
    assert source.evaluate("element => element.clientHeight") >= (
        300 if width == 1280 else 240
    )

    for index in range(4, 24):
        transcript(index, f"Phrase {index}: " + "More of the conversation. " * 6, True)
    page.wait_for_function(
        "document.querySelectorAll('#source-text p[data-segment]').length === 24"
    )
    assert source.evaluate(
        "element => element.scrollHeight - element.scrollTop - element.clientHeight < 2"
    )
    source.evaluate("element => element.scrollTop = 0")
    transcript(24, "Newest phrase while reviewing the beginning.", False)
    page.wait_for_function(
        "document.querySelectorAll('#source-text p[data-segment]').length === 25"
    )
    assert source.evaluate("element => element.scrollTop") == 0
    assert "First phrase completed." in source.inner_text()
    assert "Newest phrase while reviewing the beginning." in source.inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    page.locator("#start-live").click()
    page.wait_for_function(
        "document.getElementById('session-status').textContent === 'Processing final phrases'"
    )
    sockets[0].send(json.dumps({"type": "stopped"}))
    page.wait_for_function(
        "document.getElementById('session-status').textContent === 'Ready to listen'"
    )
    assert source.locator("p[data-segment]").count() == 25
    page.locator("#start-live").click()
    page.wait_for_function(
        "document.getElementById('session-status').textContent === 'Recording live'"
    )
    assert source.locator("p[data-segment]").count() == 0
    assert page.locator("#elapsed").inner_text() == "00:00"
    assert page.locator("#execution-id").count() == 0
    sockets[1].send(
        json.dumps(
            {
                "type": "transcript",
                "segment_index": 0,
                "text": "New recording.",
                "final": True,
            }
        )
    )
    page.wait_for_function("document.querySelector('#source-text [data-segment]')")
    page.locator("#clear-transcripts").click()
    page.wait_for_function(
        "document.getElementById('session-status').textContent === 'Ready to listen'"
    )
    assert source.locator("p[data-segment]").count() == 0
    assert page.locator("#export-current").is_hidden()
    assert page.locator("#export-current").get_attribute("href") is None
    assert page.locator("#elapsed").inner_text() == "00:00"
    assert all(
        page.locator(f"#play-{language}").is_disabled()
        for language in ["de", "fr", "es", "it"]
    )
    assert not errors


@pytest.fixture
def recorded_page(browser_page):
    page, origin = browser_page
    voice = {
        "id": "a" * 32,
        "name": "European voice",
        "languages": ["all"],
        "created_at": "2026-10-15T13:00:00Z",
        "sample_url": "/api/voices/test/sample",
    }
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": [voice]}))
    page.route(
        "**/api/sessions",
        lambda route: route.fulfill(
            json={
                "id": "recorded-session",
            }
            if route.request.method == "POST"
            else {
                "sessions": [
                    {
                        "id": "saved-session",
                        "created_at": voice["created_at"],
                        "status": "completed",
                    }
                ]
            }
        ),
    )
    segments = [
        {
            "segment_index": index,
            "text": f"Source phrase {index}.",
            "final": True,
            "translations": {
                language: f"{language} phrase {index}."
                for language in ["de", "fr", "es", "it"]
            },
        }
        for index in range(14)
    ]
    page.route(
        "**/api/sessions/*/export",
        lambda route: route.fulfill(
            json={
                "id": "recorded-session",
                "status": "completed",
                "segments": list(reversed(segments)),
            }
        ),
    )
    sockets, errors = [], []
    page.route_web_socket(
        "**/api/sessions/*/live", lambda socket: sockets.append(socket)
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(origin)
    page.evaluate("""async () => {
        const { AudioRecorder } = await import('/audio-recorder.js');
        AudioRecorder.prototype.start = async function() { this.accepting = true; };
        AudioRecorder.prototype.stop = async function() { this.accepting = false; };
        const { SpeechPlayer } = await import('/speech-player.js');
        window.played = [];
        SpeechPlayer.prototype.play = async function(url) { window.played.push(url); return true; };
    }""")
    page.locator("#start-live").click()
    page.wait_for_function(
        "document.getElementById('session-status').textContent === 'Recording live'"
    )
    for language in ["de", "fr", "es", "it"]:
        page.select_option(f"#voice-{language}", voice["id"])
        sockets[0].send(
            json.dumps(
                {
                    "type": "translation",
                    "language": language,
                    "segment_index": 0,
                    "text": segments[0]["translations"][language],
                    "final": True,
                }
            )
        )
    page.wait_for_function("document.querySelector('#text-it [data-final=true]')")
    yield page, sockets, voice
    assert not errors


def finish_recording(page, sockets):
    page.locator("#start-live").click()
    page.wait_for_function(
        "document.getElementById('session-status').textContent === 'Processing final phrases'"
    )
    sockets[0].send(json.dumps({"type": "stopped"}))
    page.wait_for_function(
        "document.getElementById('session-status').textContent === 'Ready to listen'"
    )


def test_recorded_translation_plays_all_phrases_in_order_and_reuses_audio(
    recorded_page,
):
    page, sockets, voice = recorded_page
    language = "fr"
    requests = []

    def speech(route):
        payload = json.loads(route.request.post_data)
        requests.append(payload)
        route.fulfill(json={"url": f"/api/audio/{language}-full.wav", "cached": True})

    page.route("**/api/sessions/*/playback", speech)
    assert page.locator(f"#play-{language}").is_disabled()
    assert not requests
    finish_recording(page, sockets)
    page.locator(f"#play-{language}").click()
    page.wait_for_function(
        f"document.getElementById('replay-status-{language}').textContent.startsWith('Playback finished')"
    )
    assert requests == [{"language": language, "voice_id": voice["id"]}]
    assert page.evaluate("window.played") == [f"/api/audio/{language}-full.wav"]
    page.locator(f"#play-{language}").click()
    page.wait_for_function("window.played.length === 2")
    assert len(requests) == 2
    assert page.locator(f"#voice-{language}").input_value() == voice["id"]
    assert page.locator("#session-history").count() == 0


def test_replay_voice_change_and_new_recording_cancel_stale_requests(recorded_page):
    page, sockets, _ = recorded_page
    pending = []
    page.route("**/api/sessions/*/playback", lambda route: pending.append(route))
    finish_recording(page, sockets)
    page.locator("#play-fr").click()
    page.wait_for_function(
        "document.getElementById('replay-status-fr').textContent.includes('Loading the saved')"
    )
    page.select_option("#voice-fr", "")
    with page.expect_response("**/api/sessions/*/playback"):
        pending[0].fulfill(json={"url": "/api/audio/old-voice.mp3"})
    assert page.evaluate("window.played") == []
    page.locator("#play-de").click()
    page.wait_for_function(
        "document.getElementById('replay-status-de').textContent.includes('Loading the saved')"
    )
    page.locator("#start-live").click()
    page.wait_for_function(
        "document.getElementById('session-status').textContent === 'Recording live'"
    )
    with page.expect_response("**/api/sessions/*/playback"):
        pending[1].fulfill(json={"url": "/api/audio/old-session.mp3"})
    assert page.evaluate("window.played") == []
    assert page.locator("#text-de [data-segment]").count() == 0
    assert page.locator("#play-de").is_disabled()
    sockets[1].send(
        json.dumps(
            {
                "type": "translation",
                "language": "fr",
                "segment_index": 0,
                "text": "Nouvelle phrase",
                "final": True,
            }
        )
    )
    finish_recording(page, sockets[1:])
    page.locator("#play-fr").click()
    page.wait_for_function(
        "document.getElementById('replay-status-fr').textContent.includes('Loading the saved')"
    )
    assert len(pending) == 3
    page.locator("#clear-transcripts").click()
    with page.expect_response("**/api/sessions/*/playback"):
        pending[2].fulfill(json={"url": "/api/audio/stale.mp3"})
    assert page.evaluate("window.played") == []
    assert page.locator("#export-current").is_hidden()
    assert page.locator("#play-fr").is_disabled()
    assert page.locator("#text-fr [data-segment]").count() == 0


@pytest.mark.parametrize("ending", ["stop", "complete"])
def test_full_translation_stream_keeps_playing_across_chunks_and_stops(
    recorded_page, ending
):
    page, sockets, voice = recorded_page
    page.route(
        "**/api/sessions/*/playback",
        lambda route: route.fulfill(
            json={"stream_url": "/full-stream", "cached": False}
        ),
    )
    page.evaluate("""async () => {
        const { VoicePreviewPlayer } = await import('/voice-preview-player.js');
        const originalClear = VoicePreviewPlayer.prototype.clear;
        VoicePreviewPlayer.prototype.clear = function() { window.fullPlayer = this; return originalClear.call(this); };
        const originalFetch = window.fetch;
        window.fetch = async (url, options) => {
            if (url !== '/full-stream') return originalFetch(url, options);
            return new Response(new ReadableStream({ start(controller) { window.fullStream = controller; } }));
        };
        window.pushAudio = () => {
            const data = new Float32Array(2400).fill(0.25);
            const bytes = new Uint8Array(data.buffer);
            const event = {type:'audio',sample_rate:24000,audio_data:btoa(String.fromCharCode(...bytes))};
            fullStream.enqueue(new TextEncoder().encode(JSON.stringify(event) + String.fromCharCode(10)));
        };
    }""")
    finish_recording(page, sockets)
    page.locator("#play-fr").click()
    page.wait_for_function("window.fullStream !== undefined")
    page.evaluate("pushAudio(); pushAudio(); pushAudio()")
    page.wait_for_function("window.fullPlayer.sources.size > 0")
    assert "full recorded translation" in page.locator("#replay-status-fr").inner_text()
    assert page.locator("#play-fr").inner_text() == "Stop"
    page.locator("#volume").fill("150")
    page.locator("#volume").dispatch_event("input")
    assert page.evaluate("fullPlayer.gain.gain.value") == 1.5
    if ending == "complete":
        page.evaluate("""() => {
            fullStream.enqueue(new TextEncoder().encode(JSON.stringify({type:'done',url:'/api/audio/full.wav'}) + String.fromCharCode(10)));
            fullStream.close();
            // Deterministically finish scheduled buffers; headless audio clocks can stall.
            for (const source of [...fullPlayer.sources]) source.onended();
        }""")
        page.wait_for_function(
            "document.getElementById('replay-status-fr').textContent.startsWith('Playback finished')"
        )
        assert page.locator("#play-fr").inner_text() == "Play"
        return
    page.locator("#play-fr").click()
    assert page.evaluate("fullPlayer.sources.size") == 0
    assert page.locator("#play-fr").inner_text() == "Play"
    assert page.locator("#voice-fr").input_value() == voice["id"]


def test_recorded_playback_web_audio_stops_and_settles_on_clear(browser_page):
    from backend.tests.test_app import wav

    page, origin = browser_page
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": []}))
    page.route(
        "**/api/audio/long.mp3",
        lambda route: route.fulfill(content_type="audio/wav", body=wav()),
    )
    page.goto(origin)
    page.evaluate("""async () => {
        const { SpeechPlayer } = await import('/speech-player.js');
        window.replayPlayer = new SpeechPlayer(() => {});
        window.replayPromise = replayPlayer.play('/api/audio/long.mp3');
    }""")
    page.wait_for_function("window.replayPlayer.current !== undefined")
    result = page.evaluate("""async () => {
        const current = replayPlayer.current;
        replayPlayer.setVolume(1.5);
        const gain = replayPlayer.gain.gain.value;
        replayPlayer.clear();
        return { finished: await replayPromise, current: replayPlayer.current, gain, stopped: current.onended === null };
    }""")
    assert result == {"finished": False, "current": None, "gain": 1.5, "stopped": True}


@pytest.mark.parametrize("fatal", [True, False])
def test_disconnect_preserves_fatal_reason_and_stops_microphone(browser_page, fatal):
    page, origin = browser_page
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": []}))
    page.route(
        "**/api/sessions",
        lambda route: route.fulfill(
            json={"id": "test-session"}
            if route.request.method == "POST"
            else {"sessions": []},
        ),
    )
    sockets, errors = [], []
    page.route_web_socket(
        "**/api/sessions/*/live", lambda socket: sockets.append(socket)
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(origin)
    page.evaluate(
        """async () => {
            const { AudioRecorder } = await import('/audio-recorder.js');
            const start = AudioRecorder.prototype.start;
            AudioRecorder.prototype.start = async function(options) {
                await start.call(this, options);
                window.testRecorder = this;
            };
        }"""
    )
    page.locator("#start-live").click()
    expect(page.locator("#session-status")).to_have_text("Recording live")
    sockets[0].send(
        json.dumps(
            {
                "type": "transcript",
                "text": "Earlier speech stays visible.",
                "segment_index": 0,
                "final": True,
            }
        )
    )
    expect(page.locator("#source-text")).to_contain_text(
        "Earlier speech stays visible."
    )
    message = "Voxtral rejected the live audio stream (code 3804): Response timeout. Workflow execution: failed-execution."
    displayed = message.replace("Voxtral", "speech recognition")
    if fatal:
        sockets[0].send(
            json.dumps({"type": "error", "message": message, "fatal": True})
        )
        expect(page.locator("#session-status")).to_have_text(
            "Recording stopped due to an error"
        )
        expect(page.locator("#notice")).to_have_text(displayed)
        page.wait_for_function(
            "window.testRecorder.stream.getTracks().every(track => track.readyState === 'ended')"
        )
        expect(page.locator("#start-live")).to_be_disabled()
    sockets[0].close()
    expect(page.locator("#session-status")).to_have_text("Ready to listen")
    expect(page.locator("#notice")).to_have_text(
        displayed
        if fatal
        else "The live session disconnected. Recording has stopped; check worker and backend logs."
    )
    page.wait_for_function(
        "window.testRecorder.stream.getTracks().every(track => track.readyState === 'ended')"
    )
    expect(page.locator("#source-text")).to_contain_text(
        "Earlier speech stays visible."
    )
    expect(page.locator("#start-live")).to_be_enabled()
    expect(page.locator("#latency")).to_be_enabled()

    page.locator("#start-live").click()
    expect(page.locator("#session-status")).to_have_text("Recording live")
    expect(page.locator("#notice")).to_be_hidden()
    assert len(sockets) == 2
    sockets[1].close()
    expect(page.locator("#session-status")).to_have_text("Ready to listen")
    expect(page.locator("#notice")).to_contain_text("The live session disconnected.")
    assert not errors


def test_intelligence_saved_list_details_retry_and_visible_polling(browser_page):
    page, origin = browser_page
    page.clock.install(time="2026-10-16T10:00:00Z")
    unsafe = '<img src=x onerror="window.injected=true">'
    report = {
        "title": unsafe,
        "summary": None,
        "topics": ["Budget", unsafe],
        "speakers": [],
        "organisations": ["Acme Corp"],
        "policy_references": [],
        "kpis": [
            {
                "name": "Investment",
                "value": 0,
                "unit": "EUR",
                "timeframe": None,
                "context": None,
                "evidence": unsafe,
            }
        ],
        "actions": [
            {
                "action": "Review budget",
                "owner": None,
                "deadline": None,
                "evidence": "Please review the budget.",
            }
        ],
        "announcements": [
            {"description": "New programme", "evidence": "We launch today."}
        ],
        "dates": [
            {"date": "2026-11-01", "context": None, "evidence": "On November 1."}
        ],
    }
    states = {
        "a" * 32: {
            "status": "completed",
            "report": report,
            "transcript_complete": False,
        },
        "b" * 32: {
            "status": "failed",
            "report": None,
            "transcript_complete": True,
            "message": "Extraction unavailable.",
        },
        "c" * 32: {"status": "not_started", "report": None},
    }
    sessions = [
        {
            "id": identifier,
            "created_at": "2026-10-16T10:00:00Z",
            "source_language": "en",
            "status": "completed",
        }
        for identifier in states
    ]
    sessions[0]["title"] = unsafe
    report["title"] = "AI-generated alternative title"
    sessions[2]["title"] = "Budget discussion · 16 October"
    sessions[2]["source_language"] = "auto"
    sessions[0]["stop_timings_ms"] = {
        "transcription": 1200,
        "translations": 3000,
        "speech": 0,
        "total": 4200,
    }
    calls, posts, errors, pending, reanalyses = [], [], [], [], []
    fail_list = False
    hold_details = False
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": []}))

    def list_api(route):
        calls.append(route.request.url)
        if fail_list:
            route.fulfill(status=503, json={"detail": "List temporarily unavailable."})
        else:
            route.fulfill(
                json={
                    "sessions": [
                        {**session, "intelligence": states[session["id"]]}
                        for session in sessions
                    ]
                }
            )

    def detail_api(route):
        identifier = route.request.url.split("/")[-2]
        if route.request.method == "POST":
            if urlsplit(route.request.url).query == "reanalyse=true":
                reanalyses.append(identifier)
            else:
                posts.append(identifier)
            states[identifier] = {**states[identifier], "status": "queued"}
            route.fulfill(status=202, json=states[identifier])
        elif hold_details:
            pending.append(route)
        else:
            session = next(item for item in sessions if item["id"] == identifier)
            route.fulfill(json={"session": session, "intelligence": states[identifier]})

    page.route("**/api/sessions", list_api)
    page.route("**/api/sessions/*/intelligence*", detail_api)
    page.goto(origin)
    expect(page.locator("#voice-test-text")).not_to_be_empty()
    page.clock.pause_at("2026-10-16T11:00:00Z")
    assert not calls
    page.locator("[data-page=intelligence]").click()
    expect(page.locator("#intelligence-list tr")).to_have_count(3)
    expect(page.locator("#breadcrumb")).to_have_text("Record Intelligence")
    assert page.locator("#intelligence-list tr").first.locator(
        "td"
    ).all_text_contents()[2:] == ["en", "1", "Completed"]
    page.locator(".intelligence-open").first.click()
    dialog = page.locator("#intelligence-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#intelligence-title")).to_have_text(unsafe)
    expect(page.locator("#intelligence-report")).to_contain_text(
        "Incomplete transcript"
    )
    assert (
        page.locator("#intelligence-report dt").all_text_contents().count("Context")
        == 2
    )
    assert "N/A" in page.locator("#intelligence-report dd").all_text_contents()
    assert "0" in page.locator("#intelligence-report dd").all_text_contents()
    assert page.locator("#intelligence-report blockquote").all_text_contents() == [
        f"“{unsafe}”",
        "“Please review the budget.”",
        "“We launch today.”",
        "“On November 1.”",
    ]
    assert dialog.locator("img, script").count() == 0
    assert page.evaluate("window.injected === undefined")
    assert page.locator("#intelligence-export").get_attribute("href") == (
        f"/api/sessions/{sessions[0]['id']}/export"
    )
    expect(page.locator("#intelligence-analyse")).to_have_text("Re-analyse")
    expect(page.locator("#intelligence-report")).to_contain_text("4200 ms")
    page.evaluate(
        "window.reportNode = document.querySelector('#intelligence-report dl')"
    )
    with page.expect_response("**/api/sessions/*/intelligence"):
        page.clock.fast_forward(2200)
    assert page.evaluate(
        "reportNode === document.querySelector('#intelligence-report dl')"
    )
    confirmations = []

    def cancel_reanalysis(dialog):
        confirmations.append(dialog.message)
        dialog.dismiss()

    page.once("dialog", cancel_reanalysis)
    page.locator("#intelligence-analyse").click()
    assert not reanalyses and not posts
    assert "new hosted inference" in confirmations[0]
    assert "previous report will remain visible" in confirmations[0]
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_request("**/api/sessions/*/intelligence?reanalyse=true"):
        page.locator("#intelligence-analyse").click()
    expect(page.locator("#intelligence-detail-status")).to_have_text("Queued")
    expect(page.locator("#intelligence-analyse")).to_be_hidden()
    expect(page.locator("#intelligence-report")).to_contain_text("Review budget")
    states[sessions[0]["id"]] = {
        **states[sessions[0]["id"]],
        "status": "completed",
        "report": {"title": "New analysis", "summary": "New validated summary"},
    }
    sessions[0]["title"] = "New analysis"
    page.clock.fast_forward(2200)
    expect(page.locator("#intelligence-title")).to_have_text("New analysis")
    expect(page.locator("#intelligence-report")).to_contain_text(
        "New validated summary"
    )
    expect(page.locator("#intelligence-analyse")).to_have_text("Re-analyse")
    assert reanalyses == [sessions[0]["id"]]
    page.locator("#intelligence-close").click()
    page.locator(".intelligence-open").nth(1).click()
    expect(page.locator("#intelligence-detail-status")).to_contain_text(
        "Extraction unavailable."
    )
    expect(page.locator("#intelligence-analyse")).to_have_text("Retry")
    expect(page.locator("#intelligence-report")).not_to_contain_text(
        "Incomplete transcript"
    )
    expect(page.locator("#intelligence-report")).to_contain_text("N/A")
    page.locator("#intelligence-analyse").click()
    expect(page.locator("#intelligence-detail-status")).to_have_text("Queued")
    expect(page.locator("#intelligence-analyse")).to_be_hidden()
    states[sessions[1]["id"]] = {
        "status": "completed",
        "report": {"title": "Recovered"},
        "transcript_complete": True,
    }
    page.clock.fast_forward(2200)
    expect(page.locator("#intelligence-title")).to_have_text("Recovered")
    page.locator("#intelligence-close").click()
    expect(page.locator(".intelligence-open").nth(2)).to_have_text(sessions[2]["title"])
    assert page.locator("#intelligence-list small").count() == 0
    assert page.locator("#intelligence-list tr").nth(2).locator(
        "td"
    ).all_text_contents()[2:4] == ["N/A", "N/A"]
    page.locator(".intelligence-open").nth(2).click()
    expect(page.locator("#intelligence-report .intelligence-caveat")).to_have_count(0)
    expect(page.locator("#intelligence-analyse")).to_have_text("Analyse")
    page.locator("#intelligence-analyse").click()
    expect(page.locator("#intelligence-detail-status")).to_have_text("Queued")
    assert posts == [sessions[1]["id"], sessions[2]["id"]]
    page.locator("#intelligence-close").click()
    fail_list = True
    page.clock.fast_forward(2200)
    expect(page.locator("#intelligence-list-status")).to_contain_text(
        "Previous results are kept"
    )
    expect(page.locator("#intelligence-list tr")).to_have_count(3)
    fail_list = False
    page.reload()
    page.locator("[data-page=intelligence]").click()
    expect(page.locator("#intelligence-list tr")).to_have_count(3)
    hold_details = True
    with page.expect_request("**/api/sessions/*/intelligence"):
        page.locator(".intelligence-open").first.click()
    assert pending
    page.locator("#intelligence-close").click()
    page.locator("[data-page=live]").click()
    count = len(calls)
    with page.expect_response("**/api/sessions/*/intelligence"):
        pending[0].fulfill(status=503, json={"detail": "Stale detail failure"})
    page.clock.fast_forward(2200)
    assert len(calls) == count
    expect(page.locator("#notice")).to_be_hidden()
    expect(dialog).not_to_be_visible()
    assert not errors


def test_intelligence_navigation_keeps_microphone_and_finishing_stages(browser_page):
    page, origin = browser_page
    sockets, messages, errors = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": []}))
    page.route(
        "**/api/sessions",
        lambda route: route.fulfill(
            json={"id": "live-session"}
            if route.request.method == "POST"
            else {"sessions": []}
        ),
    )

    def live_socket(socket):
        sockets.append(socket)
        socket.on_message(lambda message: messages.append(message))

    page.route_web_socket("**/api/sessions/*/live", live_socket)
    page.goto(origin)
    page.evaluate("""async () => {
        const { AudioRecorder } = await import('/audio-recorder.js');
        const start = AudioRecorder.prototype.start;
        AudioRecorder.prototype.start = async function(options) {
            await start.call(this, options); window.testRecorder = this;
        };
        const { SpeechPlayer } = await import('/speech-player.js');
        const clear = SpeechPlayer.prototype.clear;
        SpeechPlayer.prototype.clear = function() { window.audioClears = (window.audioClears || 0) + 1; clear.call(this); };
        const { VoicePreviewPlayer } = await import('/voice-preview-player.js');
        const previewClear = VoicePreviewPlayer.prototype.clear;
        VoicePreviewPlayer.prototype.clear = function() { window.previewClears = (window.previewClears || 0) + 1; previewClear.call(this); };
    }""")
    page.locator("#start-live").click()
    expect(page.locator("#session-status")).to_have_text("Recording live")
    before = page.evaluate("[audioClears, previewClears]")
    page.locator("[data-page=intelligence]").click()
    expect(page.locator("#intelligence-page")).to_be_visible()
    assert page.evaluate(
        "testRecorder.accepting && testRecorder.stream.getTracks().every(track => track.readyState === 'live')"
    )
    assert page.evaluate("audioClears > 0 && previewClears > 0")
    after = page.evaluate("[audioClears, previewClears]")
    assert all(new > old for new, old in zip(after, before))
    page.locator("[data-page=live]").click()
    expect(page.locator("#session-status")).to_have_text("Recording live")
    page.locator("#start-live").click()
    expect(page.locator("#start-live")).to_be_disabled()
    page.wait_for_function(
        "testRecorder.stream.getTracks().every(track => track.readyState === 'ended')"
    )
    page.wait_for_timeout(100)
    assert any(
        isinstance(message, str) and json.loads(message).get("type") == "stop"
        for message in messages
    )
    for event, text in [
        (
            {"type": "finishing", "stage": "transcription"},
            "Recording stopped · waiting for final text",
        ),
        ({"type": "transcript_saved"}, "Transcript saved · finishing translations"),
        (
            {"type": "finishing", "stage": "translations"},
            "Transcript saved · finishing translations",
        ),
        (
            {"type": "finishing", "stage": "speech"},
            "Transcript saved · finishing speech",
        ),
    ]:
        sockets[0].send(json.dumps(event))
        expect(page.locator("#session-status")).to_have_text(text)
        expect(page.locator("#start-live")).to_be_disabled()
    page.locator("[data-page=intelligence]").click()
    page.locator("[data-page=live]").click()
    expect(page.locator("#session-status")).to_have_text(
        "Transcript saved · finishing speech"
    )
    expect(page.locator("#start-live")).to_be_disabled()
    sockets[0].send(json.dumps({"type": "stopped"}))
    expect(page.locator("#session-status")).to_have_text("Ready to listen")
    expect(page.locator("#start-live")).to_be_enabled()
    assert len(sockets) == 1
    assert not errors


def test_intelligence_delete_records_confirmation_retention_and_races(browser_page):
    page, origin = browser_page
    page.clock.install(time="2026-10-16T10:00:00Z")
    voice = {
        "id": "v" * 32,
        "name": "Preserved voice",
        "languages": ["all"],
        "created_at": "2026-10-16T10:00:00Z",
        "sample_url": "/api/voices/sample",
    }
    sessions = [
        {
            "id": letter * 32,
            "title": title,
            "created_at": voice["created_at"],
            "source_language": "en",
            "status": "completed",
            "intelligence": {
                "status": status,
                "report": report,
                "transcript_complete": True,
            },
        }
        for letter, title, status, report in [
            ("a", "Current conversation", "completed", {"title": "AI alternative"}),
            (
                "b",
                "Retained conversation",
                "completed",
                {"summary": "Keep this report"},
            ),
            ("c", None, "not_started", None),
        ]
    ]
    current_id = sessions[0]["id"]
    stale_sessions = json.loads(json.dumps(sessions))
    deletes, stale_lists, stale_details, analyses, sockets, errors, methods = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )
    hold_list = False
    hold_detail = False
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/voices", lambda route: route.fulfill(json={"voices": [voice]}))

    def sessions_api(route):
        methods.append(route.request.method)
        if route.request.method == "DELETE":
            deletes.append(route)
        elif route.request.method == "POST":
            route.fulfill(status=201, json={"id": current_id})
        elif hold_list:
            stale_lists.append(route)
        else:
            route.fulfill(json={"sessions": sessions})

    def details_api(route):
        identifier = route.request.url.split("/")[-2]
        if route.request.method == "POST":
            analyses.append(route)
        elif hold_detail:
            stale_details.append(route)
            page.evaluate("window.heldDetail = true")
        else:
            session = next(item for item in sessions if item["id"] == identifier)
            route.fulfill(
                json={"session": session, "intelligence": session["intelligence"]}
            )

    page.route("**/api/sessions", sessions_api)
    page.route("**/api/sessions/*/intelligence", details_api)
    page.route_web_socket(
        "**/api/sessions/*/live", lambda socket: sockets.append(socket)
    )
    page.goto(origin)
    expect(page.locator("#voice-test-select")).to_have_value(voice["id"])
    page.clock.pause_at("2026-10-16T11:00:00Z")
    page.evaluate("""async () => {
        const { AudioRecorder } = await import('/audio-recorder.js');
        AudioRecorder.prototype.start = async function() { this.accepting = true; };
        AudioRecorder.prototype.stop = async function() { this.accepting = false; };
    }""")
    page.select_option("#voice-fr", voice["id"])
    page.locator("#start-live").click()
    expect(page.locator("#session-status")).to_have_text("Recording live")
    page.locator("[data-page=intelligence]").click()
    expect(page.locator("#intelligence-list tr")).to_have_count(3)
    button = page.locator("#intelligence-delete")
    expect(button).to_be_disabled()
    expect(page.locator(".intelligence-open").nth(2)).to_have_text("Untitled recording")
    page.locator("[data-page=live]").click()
    page.locator("#start-live").click()
    expect(page.locator("#session-status")).to_have_text("Processing final phrases")
    page.locator("[data-page=intelligence]").click()
    expect(button).to_be_disabled()
    sockets[0].send(json.dumps({"type": "stopped"}))
    expect(button).to_be_enabled()
    dialogs = []

    def cancel(dialog):
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.once("dialog", cancel)
    button.click()
    assert not deletes
    assert (
        "local transcript/translation session records and intelligence reports/snapshots"
        in dialogs[0]
    )
    assert (
        "voice library, microphone recordings, generated audio, and hosted histories will be preserved"
        in dialogs[0]
    )
    assert "cannot be undone" in dialogs[0]
    page.locator(".intelligence-open").nth(2).click()
    expect(page.locator("#intelligence-analyse")).to_have_text("Analyse")
    with page.expect_request("**/api/sessions/*/intelligence"):
        page.locator("#intelligence-analyse").click()
    page.locator("#intelligence-close").click()
    expect(button).to_be_disabled()
    analyses[0].fulfill(status=202, json={"status": "queued", "report": None})
    expect(button).to_be_enabled()
    page.locator(".intelligence-open").first.click()
    expect(page.locator("#intelligence-title")).to_have_text("Current conversation")
    assert (
        "Current conversation"
        in page.locator("#intelligence-report dd").all_text_contents()
    )
    hold_list = True
    hold_detail = True
    with page.expect_request("**/api/sessions/*/intelligence"):
        page.clock.fast_forward(2200)
    page.wait_for_function("window.heldDetail === true")
    assert stale_lists and stale_details
    page.locator("#intelligence-close").click()
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_request("**/api/sessions"):
        button.click()
    expect(button).to_be_disabled()
    expect(page.locator("#intelligence-dialog")).not_to_be_visible()
    count = len(methods)
    page.clock.fast_forward(2200)
    assert len(methods) == count
    page.locator("[data-page=live]").click()
    expect(page.locator("#start-live")).to_be_disabled()
    page.locator("#start-live").dispatch_event("click")
    assert methods.count("POST") == 1
    expect(page.locator("#export-current")).to_be_visible()
    hold_list = False
    hold_detail = False
    sessions[:] = [sessions[1]]
    deletes[0].fulfill(
        json={
            "deleted": [current_id, "c" * 32],
            "retained": [{"id": "b" * 32, "message": "Analysis is running."}],
        }
    )
    expect(page.locator("#export-current")).to_be_hidden()
    assert page.locator("#export-current").get_attribute("href") is None
    expect(page.locator("#start-live")).to_be_enabled()
    expect(page.locator("#voice-fr")).to_have_value(voice["id"])
    assert page.locator(".voice-item strong").text_content() == voice["name"]
    page.locator("[data-page=intelligence]").click()
    expect(page.locator("#intelligence-list tr")).to_have_count(1)
    with page.expect_response("**/api/sessions"):
        stale_lists[0].fulfill(json={"sessions": stale_sessions})
    with page.expect_response("**/api/sessions/*/intelligence"):
        stale_details[0].fulfill(status=503, json={"detail": "Stale detail failure"})
    expect(page.locator("#intelligence-list tr")).to_have_count(1)
    status = page.locator("#intelligence-delete-status")
    expect(status).to_contain_text("Retained conversation: Analysis is running.")
    page.clock.fast_forward(2200)
    expect(status).to_contain_text("retry")
    page.locator(".intelligence-open").click()
    expect(page.locator("#intelligence-report")).to_contain_text("Keep this report")
    page.locator("#intelligence-close").click()
    page.once("dialog", lambda dialog: dialog.accept())
    button.click()
    expect(button).to_be_disabled()
    deletes[1].fulfill(status=503, json={"detail": "Local deletion unavailable."})
    expect(status).to_contain_text("Local deletion unavailable.")
    expect(page.locator("#intelligence-list tr")).to_have_count(1)
    page.clock.fast_forward(2200)
    expect(status).to_contain_text("Previous results are kept")
    expect(button).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    button.click()
    sessions.clear()
    deletes[2].fulfill(json={"deleted": ["b" * 32], "retained": []})
    expect(page.locator("#intelligence-list tr")).to_have_count(0)
    expect(button).to_be_disabled()
    expect(status).to_contain_text(
        "Voice library, audio, and hosted histories are preserved."
    )
    assert len(deletes) == 3
    assert not errors
