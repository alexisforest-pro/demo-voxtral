import { request, postJson } from './api.js';
import { AudioRecorder } from './audio-recorder.js';
import { SpeechPlayer } from './speech-player.js';
import { VoicePreviewPlayer } from './voice-preview-player.js';

const $ = id => document.getElementById(id);
const languages = [
  { code: 'de', name: 'German', native: 'Deutsch' },
  { code: 'fr', name: 'French', native: 'Français' },
  { code: 'es', name: 'Spanish', native: 'Español' },
  { code: 'it', name: 'Italian', native: 'Italiano' },
];
const scripts = {
  all: 'Hello, and welcome. Today we are bringing our teams together across Europe. Every voice deserves to be heard, in every language. Together, we will build new bridges between our countries, our communities, and our ideas.',
  en: 'Hello, and welcome. Today we are bringing our teams together across Europe. Every voice deserves to be heard, in every language. Together, we will build new bridges between our countries, our communities, and our ideas.',
  fr: 'Bonjour et bienvenue. Aujourd’hui, nous réunissons nos équipes à travers l’Europe. Chaque voix mérite d’être entendue, dans toutes les langues. Ensemble, nous construirons de nouveaux ponts entre nos pays, nos communautés et nos idées.',
  de: 'Hallo und herzlich willkommen. Heute bringen wir unsere Teams in ganz Europa zusammen. Jede Stimme verdient es, in jeder Sprache gehört zu werden. Gemeinsam bauen wir neue Brücken zwischen unseren Ländern, unseren Gemeinschaften und unseren Ideen.',
  es: 'Hola y bienvenidos. Hoy reunimos a nuestros equipos de toda Europa. Cada voz merece ser escuchada, en todos los idiomas. Juntos construiremos nuevos puentes entre nuestros países, nuestras comunidades y nuestras ideas.',
  it: 'Ciao e benvenuti. Oggi riuniamo i nostri team in tutta Europa. Ogni voce merita di essere ascoltata, in ogni lingua. Insieme costruiremo nuovi ponti tra i nostri paesi, le nostre comunità e le nostre idee.',
};
let config, voices = [], socket, liveRecorder, voiceRecorder, voiceBlob, previewUrl;
let liveState = 'idle', speechLanguage = null, muted = false, liveFatalError = null;
let liveTimer, voiceTimer, liveStarted, voiceStarted;
let testVoice = null, voiceTestVersion = 0, voiceTestBusy = false;
let voiceLibraryBusy = false, voiceSaveBusy = false, voiceInputsBusy = false;
let currentSession = null, sessionFinished = false, replayLanguage = null, replayVersion = 0;
let activePage = 'live', intelligenceTimer, intelligenceVersion = 0, intelligenceListRequest = 0;
let intelligenceDetailVersion = 0, intelligenceDetailRequest = 0, intelligenceSession = null, intelligenceBusy = false;
let intelligenceDeleteBusy = false, intelligenceRequests = 0, intelligenceRecords = [];
const selectedVoices = Object.fromEntries(languages.map(language => [language.code, null]));
const player = new SpeechPlayer(showError);
const voicePreviewPlayer = new VoicePreviewPlayer();
const recordedPlayer = new VoicePreviewPlayer();

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function displayMessage(message) {
  return String(message).replace(/MISTRAL_API_KEY/g, 'the API key').replace(/\bMistral(?:\s+(?:AI|Studio))?\b/gi, 'the AI service').replace(/\bVoxtral[\w.-]*\b/gi, 'speech recognition');
}
function showError(message) { $('notice').textContent = displayMessage(message); $('notice').hidden = false; }
function clearError() { $('notice').hidden = true; }
function showVoiceStatus(message, state = 'info') {
  const status = $('voice-save-status');
  status.textContent = displayMessage(message); status.dataset.state = state; status.hidden = false;
}
function showVoiceTestStatus(message, state = 'info') {
  const status = $('voice-test-status');
  status.textContent = displayMessage(message); status.dataset.state = state;
}
function updateVoiceTestControls() {
  $('test-voice').disabled = !testVoice || voiceTestBusy || Boolean(voiceRecorder) || voiceLibraryBusy;
  $('voice-test-select').disabled = !voices.length || voiceTestBusy || voiceLibraryBusy;
  $('stop-voice-test').hidden = !voiceTestBusy;
  $('voice-test-volume').disabled = !testVoice;
  $('test-voice').textContent = voiceTestBusy ? 'Generating through Workflows…' : 'Read in English';
}
function resetVoiceTest() {
  voiceTestVersion++; voiceTestBusy = false; voicePreviewPlayer.clear();
  const audio = $('voice-test-audio');
  audio.pause(); audio.removeAttribute('src'); audio.load(); audio.hidden = true;
  updateVoiceTestControls();
  showVoiceTestStatus(testVoice ? `Ready to test “${testVoice.name}” in English.` : 'Save a cloned voice to enable the English preview.');
}
function updateVoiceTestSelection() {
  const select = $('voice-test-select'); select.replaceChildren();
  if (!voices.length) {
    const empty = node('option', '', 'Save a voice first'); empty.value = ''; select.append(empty);
  }
  for (const voice of voices) {
    const option = node('option', '', voice.name); option.value = voice.id; select.append(option);
  }
  const current = voices.find(voice => voice.id === testVoice?.id) || voices[0] || null;
  if (current?.id !== testVoice?.id) { testVoice = current; resetVoiceTest(); }
  select.value = testVoice?.id || ''; updateVoiceTestControls();
}
function timeLabel(seconds) { return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`; }
function delayLabel(ms) { return ms < 1000 ? `${ms} ms` : `${ms / 1000} s`; }
function updateLatencyStatus() {
  const preset = config?.latency_presets.find(preset => preset.id === $('latency').value);
  if (preset) $('queue-status').textContent = `${delayLabel(preset.target_delay_ms)} lookahead + model processing`;
}
function safely(action) { return (...args) => Promise.resolve(action(...args)).catch(error => showError(error.message)); }

for (const button of document.querySelectorAll('[data-page]')) {
  button.addEventListener('click', () => {
    const page = button.dataset.page;
    if (page === 'voices' && liveState !== 'idle') { showError('Finish the live recording before capturing a voice.'); return; }
    if (page !== 'voices' && voiceRecorder) { showError('Stop the voice sample recording first.'); return; }
    resetVoiceTest(); stopRecordedPlayback();
    closeIntelligence(); activePage = page;
    for (const name of ['voices', 'live', 'intelligence']) $(`${name}-page`).hidden = page !== name;
    $('breadcrumb').textContent = { voices: 'Capture a voice', live: 'Live Transcript', intelligence: 'Record Intelligence' }[page];
    updateIntelligencePolling();
    document.querySelectorAll('[data-page]').forEach(item => item.classList.toggle('active', item === button));
    clearError();
  });
}

const intelligenceLabels = {
  not_started: 'Not started', waiting: 'Waiting for final transcript', queued: 'Queued', running: 'Analysing',
  completed: 'Completed', failed: 'Failed', unavailable: 'Unavailable',
};
const availableText = value => ((typeof value === 'string' && value.trim()) || typeof value === 'number') ? String(value) : 'N/A';
const reportArray = value => Array.isArray(value) ? value : [];
const sourceLanguageText = value => availableText(value === 'auto' ? null : value);
const recordingTitle = (session, state) => [session?.title, state?.report?.title].find(value => typeof value === 'string' && value.trim()) || 'Untitled recording';
function updateIntelligenceControls() {
  $('intelligence-delete').disabled = !intelligenceRecords.length || intelligenceDeleteBusy || intelligenceRequests > 0 || liveState !== 'idle';
  $('intelligence-delete').textContent = intelligenceDeleteBusy ? 'Deleting local records…' : 'Delete all records';
  document.querySelectorAll('.intelligence-open').forEach(button => { button.disabled = intelligenceDeleteBusy; });
}
let intelligenceDetailPayload = null;
function intelligenceVisible() { return !document.hidden && (activePage === 'intelligence' || $('intelligence-dialog').open); }
function intelligenceStatus(state) { return intelligenceLabels[state?.status] || 'N/A'; }
function sessionDate(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : 'N/A';
}
function renderIntelligenceList(sessions) {
  intelligenceRecords = sessions;
  const rows = sessions.map(session => {
    const row = node('tr'), state = session.intelligence;
    const title = node('button', 'intelligence-open', recordingTitle(session, state));
    title.type = 'button'; title.addEventListener('click', () => openIntelligence(session.id));
    const titleCell = node('td'); titleCell.append(title);
    row.append(node('td', '', sessionDate(session.created_at)), titleCell,
      node('td', '', sourceLanguageText(session.source_language)),
      node('td', '', reportArray(state?.report?.kpis).length || 'N/A'),
      node('td', '', intelligenceStatus(state)));
    row.addEventListener('click', event => { if (!event.target.closest('button')) openIntelligence(session.id); });
    return row;
  });
  $('intelligence-list').replaceChildren(...rows);
  $('intelligence-list-status').textContent = sessions.length ? `${sessions.length} saved sessions` : 'No saved sessions yet.';
  updateIntelligenceControls();
}
async function refreshIntelligenceList() {
  if (intelligenceDeleteBusy || !intelligenceVisible() || activePage !== 'intelligence') return;
  const version = intelligenceVersion, sequence = ++intelligenceListRequest;
  try {
    const result = await request('/api/sessions');
    if (version !== intelligenceVersion || sequence !== intelligenceListRequest || !intelligenceVisible()) return;
    renderIntelligenceList(reportArray(result.sessions));
  } catch (error) {
    if (version === intelligenceVersion && sequence === intelligenceListRequest && intelligenceVisible()) {
      $('intelligence-list-status').textContent = `Could not refresh saved sessions: ${displayMessage(error.message)} Previous results are kept.`;
    }
  }
}
function reportField(container, label, value) {
  const field = node('div', 'intelligence-field');
  field.append(node('dt', '', label), node('dd', '', availableText(value))); container.append(field);
}
function reportSection(title, entries, fields) {
  const section = node('section', 'intelligence-section'); section.append(node('h3', '', title));
  if (!entries.length) section.append(node('p', '', 'N/A'));
  for (const entry of entries) {
    const item = node('div', 'intelligence-item');
    if (!fields) item.append(node('p', '', availableText(entry)));
    else {
      const details = node('dl');
      for (const [key, label] of fields) reportField(details, label, entry?.[key]);
      item.append(details, node('small', '', 'Source evidence'), node('blockquote', '', entry?.evidence ? `“${availableText(entry.evidence)}”` : 'N/A'));
    }
    section.append(item);
  }
  return section;
}
function renderIntelligenceDetail(session, state) {
  intelligenceSession = { session, state };
  $('intelligence-title').textContent = recordingTitle(session, state);
  $('intelligence-detail-status').textContent = intelligenceStatus(state) + (['failed', 'unavailable'].includes(state?.status) && state.message ? `: ${displayMessage(state.message)}` : '');
  const button = $('intelligence-analyse'), active = ['ready', 'recording', 'finishing'].includes(session.status);
  button.hidden = !['not_started', 'failed', 'completed'].includes(state?.status);
  button.disabled = active || intelligenceBusy || intelligenceDeleteBusy;
  button.textContent = intelligenceBusy ? 'Requesting…' : { completed: 'Re-analyse', failed: 'Retry' }[state?.status] || 'Analyse';
  const payload = JSON.stringify({ session, state });
  if (payload === intelligenceDetailPayload) return;
  intelligenceDetailPayload = payload;
  const content = node('dl', 'intelligence-metadata');
  reportField(content, 'Date', sessionDate(session.created_at)); reportField(content, 'Session', session.id);
  reportField(content, 'Source language', sourceLanguageText(session.source_language)); reportField(content, 'Recording status', session.status);
  if (session.stop_timings_ms) {
    for (const [key, label] of [['transcription', 'Final text wait'], ['translations', 'Translation drain'], ['speech', 'Speech drain'], ['total', 'Total stop wait']]) {
      const value = session.stop_timings_ms[key];
      reportField(content, label, typeof value === 'number' ? `${value} ms` : null);
    }
  }
  const report = state?.report || {}, summary = node('dl');
  reportField(summary, 'Title', recordingTitle(session, state)); reportField(summary, 'Summary', report.summary);
  const sections = [content];
  if (state?.transcript_complete === false) sections.push(node('p', 'intelligence-caveat', 'Incomplete transcript: this analysis may omit speech that was not finalized.'));
  sections.push(summary);
  for (const [key, title] of [['topics', 'Topics'], ['speakers', 'Speakers'], ['organisations', 'Organisations'], ['policy_references', 'Policy references']]) {
    sections.push(reportSection(title, reportArray(report[key])));
  }
  sections.push(reportSection('KPIs', reportArray(report.kpis), [['name', 'Name'], ['value', 'Value'], ['unit', 'Unit'], ['timeframe', 'Timeframe'], ['context', 'Context']]),
    reportSection('Actions', reportArray(report.actions), [['action', 'Action'], ['owner', 'Owner'], ['deadline', 'Deadline']]),
    reportSection('Announcements', reportArray(report.announcements), [['description', 'Description']]),
    reportSection('Dates', reportArray(report.dates), [['date', 'Date'], ['context', 'Context']]));
  $('intelligence-report').replaceChildren(...sections);
}
async function refreshIntelligenceDetail() {
  if (intelligenceDeleteBusy || !intelligenceVisible() || !$('intelligence-dialog').open || !intelligenceSession || intelligenceBusy) return;
  const id = intelligenceSession.session.id, version = intelligenceDetailVersion, sequence = ++intelligenceDetailRequest;
  try {
    const result = await request(`/api/sessions/${encodeURIComponent(id)}/intelligence`);
    if (version !== intelligenceDetailVersion || sequence !== intelligenceDetailRequest || !intelligenceVisible() || !$('intelligence-dialog').open) return;
    renderIntelligenceDetail(result.session, result.intelligence);
  } catch (error) {
    if (version === intelligenceDetailVersion && sequence === intelligenceDetailRequest && intelligenceVisible() && $('intelligence-dialog').open) {
      $('intelligence-detail-status').textContent = `Could not refresh analysis: ${displayMessage(error.message)} Previous results are kept.`;
    }
  }
}
function closeIntelligence() {
  intelligenceDetailVersion++; intelligenceSession = null; intelligenceBusy = false; intelligenceDetailPayload = null;
  if ($('intelligence-dialog').open) $('intelligence-dialog').close();
}
function openIntelligence(id) {
  if (intelligenceDeleteBusy || typeof id !== 'string' || !id) return;
  closeIntelligence();
  intelligenceSession = { session: { id }, state: null };
  $('intelligence-title').textContent = 'Record Intelligence'; $('intelligence-detail-status').textContent = 'Loading analysis…';
  $('intelligence-report').replaceChildren(); $('intelligence-analyse').hidden = true;
  $('intelligence-export').href = `/api/sessions/${encodeURIComponent(id)}/export`;
  $('intelligence-dialog').showModal(); updateIntelligencePolling();
}
function updateIntelligencePolling() {
  clearInterval(intelligenceTimer); intelligenceVersion++; intelligenceDetailRequest++;
  if (intelligenceDeleteBusy || !intelligenceVisible()) return;
  const refresh = () => { refreshIntelligenceList(); refreshIntelligenceDetail(); };
  refresh(); intelligenceTimer = setInterval(refresh, 2000);
}
$('intelligence-close').addEventListener('click', closeIntelligence);
$('intelligence-dialog').addEventListener('close', () => {
  if (!$('intelligence-dialog').open) {
    intelligenceDetailVersion++; intelligenceSession = null; intelligenceBusy = false;
    if (activePage !== 'intelligence') updateIntelligencePolling();
  }
});
$('intelligence-analyse').addEventListener('click', async () => {
  if (intelligenceDeleteBusy || !intelligenceSession || intelligenceBusy) return;
  const { session, state } = intelligenceSession, version = intelligenceDetailVersion;
  const reanalyse = state?.status === 'completed';
  if (reanalyse && !window.confirm('Re-analyse this recording with new hosted inference? The previous report will remain visible until a new analysis succeeds.')) return;
  intelligenceRequests++; updateIntelligenceControls();
  intelligenceBusy = true; intelligenceDetailRequest++; renderIntelligenceDetail(session, state);
  try {
    const result = await request(`/api/sessions/${encodeURIComponent(session.id)}/intelligence${reanalyse ? '?reanalyse=true' : ''}`, { method: 'POST' });
    if (version !== intelligenceDetailVersion || !intelligenceVisible() || !$('intelligence-dialog').open) return;
    intelligenceBusy = false; renderIntelligenceDetail(session, result.intelligence || result);
    refreshIntelligenceList();
  } catch (error) {
    if (version === intelligenceDetailVersion && intelligenceVisible() && $('intelligence-dialog').open) {
      intelligenceBusy = false; renderIntelligenceDetail(session, state);
      $('intelligence-detail-status').textContent = displayMessage(error.message);
    }
  } finally {
    if (version === intelligenceDetailVersion) intelligenceBusy = false;
    intelligenceRequests--; updateIntelligenceControls();
  }
});
function showIntelligenceDeleteStatus(message, state = 'info') {
  const status = $('intelligence-delete-status');
  status.textContent = displayMessage(message); status.dataset.state = state; status.hidden = false;
}
$('intelligence-delete').addEventListener('click', async () => {
  if (intelligenceDeleteBusy || intelligenceRequests || liveState !== 'idle' || !intelligenceRecords.length) return;
  if (!window.confirm('Permanently delete all local transcript/translation session records and intelligence reports/snapshots? The voice library, microphone recordings, generated audio, and hosted histories will be preserved. This cannot be undone.')) return;
  intelligenceDeleteBusy = true; closeIntelligence(); updateIntelligencePolling();
  stopRecordedPlayback(); updateReplayControls(); updateVoiceLibraryControls(); updateIntelligenceControls();
  showIntelligenceDeleteStatus('Deleting local records…');
  try {
    const result = await request('/api/sessions', { method: 'DELETE' });
    if (!Array.isArray(result.deleted) || !Array.isArray(result.retained)) throw new Error('Unexpected deletion response. Refresh to check which records remain.');
    const deleted = new Set(result.deleted);
    const retained = result.retained;
    const remaining = intelligenceRecords.filter(session => !deleted.has(session.id));
    if (deleted.has(currentSession)) resetTranscripts();
    renderIntelligenceList(remaining);
    const reasons = retained.map(item => {
      const session = remaining.find(record => record.id === item.id);
      return `${recordingTitle(session, session?.intelligence)}: ${availableText(item.message)}`;
    });
    showIntelligenceDeleteStatus(retained.length
      ? `Deleted ${deleted.size} records. ${retained.length} retained; retry after resolving these issues. ${reasons.join(' ')}`
      : `Deleted ${deleted.size} local records. Voice library, audio, and hosted histories are preserved.`, retained.length ? 'error' : 'success');
  } catch (error) {
    showIntelligenceDeleteStatus(`Records could not be deleted: ${error.message} Previous results are kept; retry when available.`, 'error');
  } finally {
    intelligenceDeleteBusy = false; updateIntelligenceControls(); updateReplayControls(); updateVoiceLibraryControls();
    updateIntelligencePolling();
  }
});
document.addEventListener('visibilitychange', updateIntelligencePolling);

function buildCards() {
  for (const language of languages) {
    const card = node('article', 'panel language-card'); card.id = `card-${language.code}`;
    const heading = node('div', 'card-heading'); const title = node('div', 'language-title');
    title.append(node('span', 'language-code', language.code.toUpperCase()));
    const name = node('div'); name.append(node('h2', '', language.name), node('small', '', language.native));
    title.append(name); heading.append(title);
    const transcript = node('div', 'transcript'); transcript.id = `text-${language.code}`;
    transcript.append(node('p', 'placeholder', 'Translation will appear here.'));
    const options = node('div', 'language-options'); const label = node('label', 'speak-label');
    const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.id = `speak-${language.code}`;
    checkbox.addEventListener('change', safely(async () => {
      speechLanguage = checkbox.checked ? language.code : null;
      stopRecordedPlayback(); updateSpeechSelection(); sendConfiguration();
      if (speechLanguage) await player.unlock();
    }));
    label.append(checkbox, document.createTextNode('Live Translation'));
    const select = node('select'); select.id = `voice-${language.code}`;
    select.setAttribute('aria-label', `${language.name} playback voice`);
    select.addEventListener('change', () => {
      if (replayLanguage === language.code) stopRecordedPlayback();
      selectedVoices[language.code] = select.value || null;
      if (speechLanguage === language.code) { player.clear(); sendConfiguration(); }
    });
    const play = node('button', 'secondary recorded-playback', 'Play'); play.id = `play-${language.code}`; play.disabled = true;
    play.setAttribute('aria-label', `Play recorded ${language.name} translation`); play.title = 'Play recorded translation';
    play.addEventListener('click', () => playRecordedTranslation(language.code));
    const status = node('p', 'replay-status', 'Stop recording to play the full translation.'); status.id = `replay-status-${language.code}`; status.setAttribute('role', 'status');
    options.append(select, play, label); card.append(heading, transcript, options, status); $('translations').append(card);
  }
}
function updateSpeechSelection() {
  for (const language of languages) {
    $(`speak-${language.code}`).checked = speechLanguage === language.code;
    $(`card-${language.code}`).classList.toggle('speaking', speechLanguage === language.code);
  }
}
function sendConfiguration() {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({
    type: 'configure', speech_language: muted ? null : speechLanguage,
    voice_id: !muted && speechLanguage ? selectedVoices[speechLanguage] : null,
  }));
}
function updateVoiceDropdowns() {
  for (const language of languages) {
    const select = $(`voice-${language.code}`); select.replaceChildren();
    const preset = node('option', '', 'Default voice'); preset.value = ''; select.append(preset);
    for (const voice of voices.filter(voice => voice.languages.includes('all') || voice.languages.includes(language.code))) {
      const option = node('option', '', voice.name); option.value = voice.id; select.append(option);
    }
    select.value = selectedVoices[language.code] || '';
    if (!select.value) selectedVoices[language.code] = null;
  }
}
function updateVoiceLibraryControls() {
  lockVoiceSource(voiceInputsBusy || voiceLibraryBusy || voiceSaveBusy || Boolean(voiceRecorder));
  $('clear-voices').disabled = !voices.length || voiceLibraryBusy || voiceSaveBusy || Boolean(voiceRecorder) || liveState !== 'idle';
  $('clear-voices').textContent = voiceLibraryBusy ? 'Deleting through Workflows…' : 'Clear voice library';
  document.querySelectorAll('.voice-item button').forEach(button => { button.disabled = voiceLibraryBusy; });
  $('start-live').disabled = intelligenceDeleteBusy || voiceLibraryBusy || liveState === 'starting' || liveState === 'finishing';
}
function showVoiceLibraryStatus(message, state = 'info') {
  const status = $('voice-library-status');
  status.textContent = displayMessage(message); status.dataset.state = state; status.hidden = false;
}
async function deleteVoices(profiles, all = false) {
  if (voiceLibraryBusy || voiceSaveBusy || voiceRecorder || liveState !== 'idle' || !profiles.length) return;
  const prompt = all
    ? `Permanently delete all ${profiles.length} cloned voices from the hosted AI service and their local profiles and samples? Transcripts, saved sessions, and generated audio will be kept. This cannot be undone.`
    : `Delete “${profiles[0].name}” from the hosted AI service and your local voice library?`;
  if (!window.confirm(prompt)) return;
  clearError(); voiceLibraryBusy = true;
  const recordDisabled = $('record-voice').disabled, saveDisabled = $('save-voice').disabled;
  $('record-voice').disabled = true; $('save-voice').disabled = true;
  resetVoiceTest(); stopRecordedPlayback(); updateVoiceLibraryControls();
  document.querySelectorAll('#voice-list audio').forEach(audio => audio.pause());
  const failures = []; let deleted = 0;
  try {
    for (const [index, voice] of profiles.entries()) {
      showVoiceLibraryStatus(`Deleting voice ${index + 1} of ${profiles.length}: “${voice.name}”…`);
      try {
        await request(`/api/voices/${voice.id}`, { method: 'DELETE' });
        voices = voices.filter(profile => profile.id !== voice.id); deleted++;
        renderVoices(); sendConfiguration();
      } catch (error) { failures.push(`“${voice.name}”: ${error.message}`); }
    }
    if (failures.length) showVoiceLibraryStatus(`Deleted ${deleted} of ${profiles.length} voices. ${failures.length} could not be deleted and remain in your library. Retry after resolving these errors. ${failures.join(' ')}`, 'error');
    else showVoiceLibraryStatus(all ? 'Voice library cleared. Transcripts and saved sessions are unchanged.' : 'Voice deleted.', 'success');
  } finally {
    voiceLibraryBusy = false; $('record-voice').disabled = recordDisabled; $('save-voice').disabled = saveDisabled;
    updateVoiceLibraryControls(); updateVoiceTestControls(); updateReplayControls();
  }
}
$('clear-voices').addEventListener('click', safely(() => deleteVoices([...voices], true)));
async function refreshVoices() {
  voices = (await request('/api/voices')).voices; renderVoices();
}
function renderVoices() {
  const list = $('voice-list'); list.replaceChildren();
  if (!voices.length) list.append(node('p', 'muted', 'Your voice library is empty. Create your first profile to get started.'));
  for (const voice of voices) {
    const item = node('article', 'panel voice-item');
    item.append(node('strong', '', voice.name), node('small', '', `${voice.languages.join(', ').toUpperCase()} · ${new Date(voice.created_at).toLocaleDateString()}`));
    const audio = node('audio'); audio.controls = true; audio.src = voice.sample_url; audio.preload = 'none';
    const remove = node('button', 'secondary', 'Delete voice');
    remove.addEventListener('click', safely(() => deleteVoices([voice])));
    item.append(remove, audio); list.append(item);
  }
  updateVoiceDropdowns(); updateVoiceTestSelection(); updateReplayControls(); updateVoiceLibraryControls();
}
function renderText(event) {
  const container = event.type === 'transcript' ? $('source-text') : $(`text-${event.language}`);
  if (!container) return;
  const segmentIndex = Number(event.segment_index);
  if (!Number.isInteger(segmentIndex) || segmentIndex < 0 || typeof event.text !== 'string' || !event.text.trim()) return;
  const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 45;
  container.querySelector('.placeholder')?.remove();
  let paragraph = container.querySelector(`[data-segment="${segmentIndex}"]`);
  if (paragraph?.dataset.final === 'true' && !event.final) return;
  if (!paragraph) {
    paragraph = node('p'); paragraph.dataset.segment = segmentIndex;
    const next = [...container.children].find(item => Number(item.dataset.segment) > segmentIndex);
    container.insertBefore(paragraph, next || null);
  }
  paragraph.textContent = event.text; paragraph.dataset.final = String(Boolean(event.final));
  paragraph.classList.toggle('partial', !event.final);
  if (nearBottom) container.scrollTop = container.scrollHeight;
  updateReplayControls();
}
let waveformLevels = new Array(100).fill(0);
function drawLevel(level) {
  waveformLevels.push(Math.min(1, level * 5)); waveformLevels.shift();
  const canvas = $('waveform'), context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  waveformLevels.forEach((value, index) => {
    const height = Math.max(2, value * 50); context.fillStyle = value > 0.02 ? '#3366bb' : '#dfe5e9';
    context.fillRect(index * 10 + 2, (60 - height) / 2, 3, height);
  });
}
function updateReplayControls() {
  for (const language of languages) {
    const play = $(`play-${language.code}`); if (!play) continue;
    const available = $(`text-${language.code}`).querySelector('[data-final=true]');
    play.disabled = intelligenceDeleteBusy || voiceLibraryBusy || liveState !== 'idle' || !sessionFinished || !currentSession || !available;
    const playing = replayLanguage === language.code;
    play.textContent = playing ? 'Stop' : 'Play';
    play.title = playing ? 'Stop recorded playback' : 'Play recorded translation';
    play.setAttribute('aria-label', `${playing ? 'Stop' : 'Play'} recorded ${language.name} translation`);
  }
}
function stopRecordedPlayback() {
  replayVersion++; player.clear(); recordedPlayer.clear();
  if (replayLanguage) $(`replay-status-${replayLanguage}`).textContent = 'Playback stopped. Click Play to start again.';
  replayLanguage = null; updateReplayControls();
}
async function playRecordedTranslation(language) {
  if (replayLanguage === language) { stopRecordedPlayback(); return; }
  if (intelligenceDeleteBusy || liveState !== 'idle' || !sessionFinished || !currentSession) return;
  stopRecordedPlayback(); clearError();
  if (muted) { showError('Unmute translated speech before playing a recorded translation.'); return; }
  const version = replayVersion, session = currentSession, voice = selectedVoices[language];
  replayLanguage = language; updateReplayControls();
  const status = $(`replay-status-${language}`);
  status.textContent = 'Loading the saved translation…';
  try {
    await recordedPlayer.unlock();
    const result = await postJson(`/api/sessions/${session}/playback`, { language, voice_id: voice });
    if (version !== replayVersion) return;
    if (result.stream_url) {
      const url = await recordedPlayer.playStream(result.stream_url, () => {
        if (version === replayVersion) status.textContent = 'Streaming the full recorded translation…';
      }, 1800);
      if (version !== replayVersion || !url) return;
    } else {
      status.textContent = 'Playing the full recorded translation from cache…';
      if (!await recordedPlayer.play(result.url)) return;
    }
    if (version === replayVersion) status.textContent = 'Playback finished. Click Play to listen again.';
  } catch (error) {
    if (version === replayVersion) { status.textContent = 'Playback failed. Click Play to retry.'; showError(error.message); }
  } finally {
    if (version === replayVersion) { replayLanguage = null; updateReplayControls(); }
  }
}
function resetTranscripts() {
  stopRecordedPlayback(); currentSession = null; sessionFinished = false; liveFatalError = null;
  clearInterval(liveTimer);
  if (socket) { socket.onmessage = null; socket.onclose = null; socket.onerror = null; socket.close(); socket = null; }
  $('source-text').replaceChildren(node('p', 'placeholder', 'Your full transcript will appear here as you speak.')); $('source-text').scrollTop = 0;
  for (const language of languages) {
    $(`text-${language.code}`).replaceChildren(node('p', 'placeholder', 'Translation will appear here.')); $(`text-${language.code}`).scrollTop = 0;
    $(`replay-status-${language.code}`).textContent = 'Stop recording to play the full translation.';
  }
  $('elapsed').textContent = '00:00';
  $('export-current').hidden = true; $('export-current').removeAttribute('href');
  waveformLevels.fill(0); drawLevel(0); updateLatencyStatus(); clearError(); updateReplayControls();
}
async function clearTranscripts() {
  if (liveState === 'starting') return;
  const recorder = liveRecorder; liveRecorder = null;
  setLiveState('finishing'); resetTranscripts();
  try { await recorder?.stop(false); }
  finally { setLiveState('idle'); }
}
function setLiveState(state) {
  liveState = state;
  $('start-live').textContent = { idle: 'Start recording', starting: 'Connecting…', recording: 'Stop recording', finishing: 'Finishing…' }[state];
  $('start-live').disabled = state === 'starting' || state === 'finishing';
  $('latency').disabled = state !== 'idle'; $('source-language').disabled = state !== 'idle';
  $('clear-transcripts').disabled = state === 'starting'; updateReplayControls(); updateVoiceLibraryControls(); updateIntelligenceControls();
  $('session-status').textContent = { idle: 'Ready to listen', starting: 'Connecting to your microphone', recording: 'Recording live', finishing: 'Processing final phrases' }[state];
  $('recording-dot').className = `status-dot ${state === 'recording' ? 'recording' : 'neutral'}`;
}
async function stopLive() {
  if (liveState !== 'recording') return;
  setLiveState('finishing'); clearInterval(liveTimer);
  const recorder = liveRecorder, stoppedSocket = socket;
  await recorder?.stop();
  if (socket !== stoppedSocket) return;
  liveRecorder = null;
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'stop' }));
  else setLiveState('idle');
}
async function startLive() {
  if (intelligenceDeleteBusy) throw new Error('Wait for local record deletion to finish before starting a session.');
  clearError();
  if (voiceLibraryBusy) throw new Error('Wait for voice deletion to finish before starting a session.');
  if (voiceRecorder) throw new Error('Stop the voice sample before starting a session.');
  if (!config?.api_key_configured) throw new Error('Configure the API key in the server settings and start the worker before recording.');
  resetTranscripts(); resetVoiceTest();
  setLiveState('starting');
  try {
    await player.unlock();
    const preset = config.latency_presets.find(preset => preset.id === $('latency').value);
    liveRecorder = new AudioRecorder({ seconds: preset.chunk_seconds, onLevel: drawLevel,
      onChunk: blob => { if (socket?.readyState === WebSocket.OPEN) socket.send(blob); },
    });
    // Permission is obtained before creating a remote-workflow session.
    await liveRecorder.start({ paused: true });
    const session = await postJson('/api/sessions', {
      latency: preset.id, source_language: $('source-language').value,
      speech_language: muted ? null : speechLanguage,
      voice_id: !muted && speechLanguage ? selectedVoices[speechLanguage] : null,
    });
    currentSession = session.id;
    socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/sessions/${session.id}/live`);
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { socket.close(); reject(new Error('Connection timed out.')); }, 10000);
      socket.onopen = () => { clearTimeout(timeout); resolve(); };
      socket.onerror = () => { clearTimeout(timeout); reject(new Error('Could not connect to the live session.')); };
      socket.onclose = () => { clearTimeout(timeout); reject(new Error('Live connection was rejected.')); };
    });
    $('source-text').replaceChildren(node('p', 'placeholder', 'Listening for your first phrase…'));
    for (const language of languages) $(`text-${language.code}`).replaceChildren(node('p', 'placeholder', 'Waiting for the first phrase…'));
    $('export-current').href = `/api/sessions/${session.id}/export`; $('export-current').hidden = false;
    socket.onmessage = ({ data }) => {
      const event = JSON.parse(data);
      if (event.type === 'transcript' || event.type === 'translation') renderText(event);
      else if (event.type === 'audio' && activePage === 'live' && !sessionFinished && !muted && event.language === speechLanguage && event.voice_id === selectedVoices[speechLanguage]) player.enqueue(event.url);
      else if (event.type === 'error') {
        if (event.fatal) {
          liveFatalError = event.message;
          clearInterval(liveTimer); player.clear();
          liveRecorder?.stop(false).catch(error => showError(error.message)); liveRecorder = null;
          setLiveState('finishing'); $('session-status').textContent = 'Recording stopped due to an error';
        }
        showError(event.message);
      }
      else if (event.type === 'queue') $('queue-status').textContent = `${event.pending} translations queued${event.pending > 3 ? ' · Translation is falling behind; original transcript stays live' : ''}`;
      else if (event.type === 'finishing' || event.type === 'transcript_saved') {
        if (!liveFatalError) {
          setLiveState('finishing');
          $('session-status').textContent = event.type === 'transcript_saved' ? 'Transcript saved · finishing translations' : {
            transcription: 'Recording stopped · waiting for final text',
            translations: 'Transcript saved · finishing translations',
            speech: 'Transcript saved · finishing speech',
          }[event.stage] || 'Processing final phrases';
        }
      }
      else if (event.type === 'stopped') { sessionFinished = true; setLiveState('idle'); }
    };
    socket.onclose = () => {
      const unexpected = liveState === 'recording' || liveState === 'starting';
      clearInterval(liveTimer); liveRecorder?.stop(false); liveRecorder = null; sessionFinished = true; setLiveState('idle');
      if (liveFatalError) showError(liveFatalError);
      else if (unexpected) { player.clear(); showError('The live session disconnected. Recording has stopped; check worker and backend logs.'); }
    };
    socket.onerror = () => showError('The live connection encountered an error.');
    liveRecorder.accepting = true;
    setLiveState('recording'); liveStarted = Date.now();
    liveTimer = setInterval(() => { $('elapsed').textContent = timeLabel(Math.floor((Date.now() - liveStarted) / 1000)); }, 1000);
  } catch (error) {
    socket?.close(); await liveRecorder?.stop(false); liveRecorder = null; setLiveState('idle'); throw error;
  }
}
$('start-live').addEventListener('click', safely(() => liveState === 'recording' ? stopLive() : startLive()));
$('clear-transcripts').addEventListener('click', safely(clearTranscripts));
$('latency').addEventListener('change', updateLatencyStatus);
$('volume').addEventListener('input', () => { player.setVolume(Number($('volume').value) / 100); recordedPlayer.setVolume(Number($('volume').value) / 100); $('volume-label').textContent = `${$('volume').value}%`; });
$('mute').addEventListener('click', () => { stopRecordedPlayback(); muted = !muted; player.setMuted(muted); $('mute').textContent = muted ? 'Unmute' : 'Mute'; $('mute').setAttribute('aria-pressed', String(muted)); sendConfiguration(); });
$('stop-playback').addEventListener('click', stopRecordedPlayback);
$('voice-scope').addEventListener('change', () => { $('voice-script').textContent = scripts[$('voice-scope').value]; });

function showVoiceCaptureStatus(message, state = 'info') {
  $('voice-capture-status').textContent = message; $('voice-capture-status').dataset.state = state;
}
function clearVoiceSample() {
  voiceBlob = null; $('save-voice').disabled = true;
  $('voice-preview').pause(); $('voice-preview').removeAttribute('src'); $('voice-preview').load(); $('voice-preview').hidden = true;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
}
function lockVoiceSource(locked) {
  for (const id of ['voice-source', 'voice-input', 'refresh-voice-inputs']) $(id).disabled = locked;
}
function updateVoiceSource() {
  const source = $('voice-source').value;
  $('voice-input-controls').hidden = source !== 'loopback';
}
$('voice-source').addEventListener('change', () => {
  clearVoiceSample(); updateVoiceSource();
  showVoiceCaptureStatus('Audio source changed. Record a new sample.');
});
$('voice-input').addEventListener('change', () => { clearVoiceSample(); showVoiceCaptureStatus('Audio input changed. Record a new sample.'); });
$('refresh-voice-inputs').addEventListener('click', safely(async () => {
  if (voiceRecorder || voiceSaveBusy || voiceLibraryBusy || voiceInputsBusy) return;
  voiceInputsBusy = true; lockVoiceSource(true); $('record-voice').disabled = true;
  let permissionStream;
  try {
    permissionStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter(device => device.kind === 'audioinput');
    const selected = $('voice-input').value;
    $('voice-input').replaceChildren(node('option', '', 'Select an audio input'));
    $('voice-input').firstChild.value = '';
    for (const [index, device] of devices.entries()) {
      const option = node('option', '', device.label || `Audio input ${index + 1}`); option.value = device.deviceId; $('voice-input').append(option);
    }
    $('voice-input').value = selected;
    if ($('voice-input').value !== selected) clearVoiceSample();
    showVoiceCaptureStatus('Choose the loopback device receiving Teams speaker output. Refresh does not configure audio routing.');
  } finally {
    permissionStream?.getTracks().forEach(track => track.stop()); voiceInputsBusy = false;
    lockVoiceSource(voiceLibraryBusy || voiceSaveBusy || Boolean(voiceRecorder)); $('record-voice').disabled = voiceLibraryBusy || voiceSaveBusy;
  }
}));
async function stopVoice() {
  if (!voiceRecorder) return;
  const recorder = voiceRecorder; clearInterval(voiceTimer); $('record-voice').disabled = true;
  try {
    const blob = await recorder.stop();
    const problem = recorder.sampleProblem();
    voiceBlob = problem ? null : blob;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    if (blob) { previewUrl = URL.createObjectURL(blob); $('voice-preview').src = previewUrl; $('voice-preview').hidden = false; }
    $('save-voice').disabled = !voiceBlob;
    showVoiceCaptureStatus(problem || 'Sample captured. Listen to confirm one clear, consenting speaker before saving the clone.', problem ? 'error' : 'success');
    if (problem) showError(problem);
  } finally {
    voiceRecorder = null; $('record-voice').textContent = 'Record again'; $('record-voice').disabled = false;
    $('voice-scope').disabled = false; lockVoiceSource(false); $('voice-level').value = 0;
    updateVoiceTestControls(); updateVoiceLibraryControls();
  }
}
$('record-voice').addEventListener('click', safely(async () => {
  if (voiceLibraryBusy || voiceSaveBusy || voiceInputsBusy) return;
  if (voiceRecorder) { await stopVoice(); return; }
  if (liveState !== 'idle') throw new Error('Finish the live session before recording a voice sample.');
  clearError(); $('record-voice').disabled = true; lockVoiceSource(true); $('voice-scope').disabled = true;
  try {
    clearVoiceSample(); $('voice-timer').textContent = '00:00'; $('voice-level').value = 0;
    resetVoiceTest(); stopRecordedPlayback();
    document.querySelectorAll('#voice-list audio').forEach(audio => audio.pause());
    let detected = false;
    voiceRecorder = new AudioRecorder({ source: $('voice-source').value, deviceId: $('voice-source').value === 'loopback' ? $('voice-input').value : '', maxSeconds: 29,
      onLevel: level => {
        $('voice-level').value = Math.min(1, level * 5);
        if (level > 0.003 && !detected) { detected = true; showVoiceCaptureStatus('Audio detected. Record 5–20 seconds of this speaker alone, then stop and listen to the sample.'); }
      },
      onEnded: () => stopVoice().catch(error => showError(error.message)),
    });
    updateVoiceTestControls(); updateVoiceLibraryControls();
    showVoiceCaptureStatus($('voice-source').value === 'tab' ? 'Choose the Teams tab and enable Share tab audio in the browser picker.' : 'Opening the selected audio input…');
    await voiceRecorder.start();
    voiceStarted = Date.now(); $('record-voice').textContent = 'Stop sample';
    if (!detected) showVoiceCaptureStatus('Listening. Ask the consenting speaker to speak and check that the input level moves.');
    voiceTimer = setInterval(() => {
      const seconds = Math.floor((Date.now() - voiceStarted) / 1000); $('voice-timer').textContent = timeLabel(seconds);
      if (seconds >= 3 && !detected) showVoiceCaptureStatus('No audio detected yet. Check Share tab audio or loopback routing while the speaker talks.', 'error');
      if (seconds >= 29) stopVoice().catch(error => showError(error.message));
    }, 250);
  } catch (error) {
    await voiceRecorder?.stop(false); voiceRecorder = null; lockVoiceSource(false); $('voice-scope').disabled = false;
    showVoiceCaptureStatus(error.message, 'error'); throw error;
  } finally { $('record-voice').disabled = false; updateVoiceTestControls(); updateVoiceLibraryControls(); }
}));
$('voice-name').addEventListener('input', () => $('voice-name').removeAttribute('aria-invalid'));
$('save-voice').addEventListener('click', async () => {
  if (voiceLibraryBusy || voiceSaveBusy) return;
  if (!voiceBlob) {
    showVoiceStatus('Record a sample first.', 'error');
    return;
  }
  const name = $('voice-name').value.trim();
  if (!name) {
    showVoiceStatus('Enter a voice name in the required field above, then save again.', 'error');
    $('voice-name').setAttribute('aria-invalid', 'true'); $('voice-name').focus();
    return;
  }
  $('voice-name').removeAttribute('aria-invalid');
  voiceSaveBusy = true; updateVoiceLibraryControls();
  $('save-voice').disabled = true; $('record-voice').disabled = true; $('save-voice').textContent = 'Cloning through Workflows…'; clearError();
  showVoiceStatus('Saving your voice through workflows. Please wait; no need to click again.');
  try {
    const form = new FormData(); form.set('name', name); form.set('languages', JSON.stringify([$('voice-scope').value]));
    form.set('sample', voiceBlob, 'voice.wav');
    const savedVoice = await request('/api/voices', { method: 'POST', body: form });
    testVoice = savedVoice; voices.unshift(savedVoice); resetVoiceTest(); updateVoiceTestSelection();
    clearVoiceSample(); $('voice-name').value = '';
    showVoiceStatus(`Voice “${name}” saved. Try it in English below or select it in a Live Translation card.`, 'success');
    try { await refreshVoices(); }
    catch { showVoiceStatus(`Voice “${name}” was saved, but the library could not refresh. Reload the page to see it.`, 'success'); }
  } catch (error) {
    showVoiceStatus(`${error.message} Your recording is kept so you can retry.`, 'error');
  } finally {
    voiceSaveBusy = false; updateVoiceLibraryControls();
    $('save-voice').disabled = !voiceBlob; $('record-voice').disabled = false; $('save-voice').textContent = 'Save cloned voice';
  }
});
$('voice-test-select').addEventListener('change', () => {
  testVoice = voices.find(voice => voice.id === $('voice-test-select').value) || null; resetVoiceTest();
});
$('stop-voice-test').addEventListener('click', resetVoiceTest);
$('voice-test-volume').addEventListener('input', () => {
  const volume = Number($('voice-test-volume').value) / 100;
  voicePreviewPlayer.setVolume(volume); $('voice-test-audio').volume = volume;
});
$('test-voice').addEventListener('click', async () => {
  if (!testVoice || voiceTestBusy || voiceRecorder || voiceLibraryBusy) return;
  resetVoiceTest();
  const voice = testVoice, version = voiceTestVersion;
  voiceTestBusy = true; updateVoiceTestControls();
  showVoiceTestStatus(`Generating an English preview for “${voice.name}” through workflows…`);
  try {
    await voicePreviewPlayer.unlock();
    const result = await request(`/api/voices/${voice.id}/preview`, { method: 'POST' });
    if (version !== voiceTestVersion) return;
    let url = result.url;
    if (result.stream_url) {
      url = await voicePreviewPlayer.playStream(result.stream_url, () => {
        if (version === voiceTestVersion) showVoiceTestStatus(`Playing “${voice.name}” as audio arrives. The complete preview will be cached for replay.`);
      });
    }
    if (version !== voiceTestVersion || !url) return;
    const audio = $('voice-test-audio'); audio.src = url; audio.hidden = false;
    showVoiceTestStatus(`English preview for “${voice.name}” is ready${result.cached ? ' from cache' : ''}. Use the controls to replay it.`, 'success');
    if (!result.stream_url) {
      try { await audio.play(); }
      catch { if (version === voiceTestVersion) showVoiceTestStatus('Preview ready. Press Play in the audio controls to listen.', 'success'); }
    }
  } catch (error) {
    if (version === voiceTestVersion) showVoiceTestStatus(`${error.message} Your saved voice is kept; click Read in English to retry.`, 'error');
  } finally {
    if (version === voiceTestVersion) { voiceTestBusy = false; updateVoiceTestControls(); }
  }
});
$('voice-test-audio').addEventListener('error', () => {
  if ($('voice-test-audio').getAttribute('src')) showVoiceTestStatus('The preview audio could not be loaded. Click Read in English to generate it again.', 'error');
});
window.addEventListener('beforeunload', () => { liveRecorder?.stream?.getTracks().forEach(track => track.stop()); voiceRecorder?.stream?.getTracks().forEach(track => track.stop()); socket?.close(); });

buildCards(); drawLevel(0);
try {
  config = await request('/api/config');
  $('voice-test-text').textContent = config.voice_preview_text;
  const currentLatency = $('latency').value;
  $('latency').replaceChildren(...config.latency_presets.map(preset => {
    const option = node('option', '', `${preset.label || preset.id} · ${delayLabel(preset.target_delay_ms)} lookahead`);
    option.value = preset.id; return option;
  }));
  $('latency').value = currentLatency;
  updateLatencyStatus();
  if (!config.api_key_configured) showError('Configure the API key in the server settings and restart the backend and worker.');
  await refreshVoices();
} catch (error) { showError(error.message); }
