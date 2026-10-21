/** Incremental 24 kHz mono float32 preview playback, independent of live speech. */
import { SpeechPlayer } from './speech-player.js';

export class VoicePreviewPlayer extends SpeechPlayer {
  constructor() { super(() => {}); this.sources = new Set(); }
  clear() {
    super.clear(); this.controller?.abort(); this.controller = null;
    this.reader?.cancel().catch(() => {}); this.reader = null;
    this.finish?.(false); this.finish = null;
    for (const source of this.sources) { source.onended = null; try { source.stop(); } catch {} }
    this.sources.clear(); this.nextTime = 0;
  }
  async playStream(url, onAudio, maxSeconds = 120) {
    this.clear(); const version = this.version;
    const controller = new AbortController(); this.controller = controller;
    await this.unlock();
    if (version !== this.version) return null;
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok || !response.body) throw new Error('Voice preview stream could not be loaded.');
    const reader = response.body.getReader(), decoder = new TextDecoder(); this.reader = reader;
    let pending = '', result = null, totalSamples = 0;
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (version !== this.version) return null;
        pending += decoder.decode(value, { stream: !done });
        let newline;
        while ((newline = pending.indexOf('\n')) !== -1) {
          const line = pending.slice(0, newline); pending = pending.slice(newline + 1);
          if (!line.trim()) continue;
          if (line.length > 65536) throw new Error('Voice preview frame is too large.');
          const event = JSON.parse(line);
          if (event.type === 'error') throw new Error(event.message || 'Speech preview failed.');
          if (event.type === 'done') { result = event.url; continue; }
          if (event.type !== 'audio' || result || event.sample_rate !== 24000) throw new Error('Invalid voice preview frame.');
          const raw = atob(event.audio_data);
          if (!raw.length || raw.length % 4 || raw.length > 24000) throw new Error('Invalid voice preview PCM.');
          totalSamples += raw.length / 4;
          if (totalSamples > 24000 * maxSeconds) throw new Error('Generated speech exceeds the audio duration limit.');
          const bytes = Uint8Array.from(raw, character => character.charCodeAt(0));
          const view = new DataView(bytes.buffer);
          const audio = this.context.createBuffer(1, raw.length / 4, 24000), samples = audio.getChannelData(0);
          for (let index = 0; index < samples.length; index++) {
            const sample = view.getFloat32(index * 4, true);
            if (!Number.isFinite(sample)) throw new Error('Invalid voice preview sample.');
            samples[index] = Math.max(-1, Math.min(1, sample));
          }
          const source = this.context.createBufferSource(); source.buffer = audio; source.connect(this.gain);
          this.sources.add(source);
          source.onended = () => { this.sources.delete(source); if (!this.sources.size) this.finish?.(true); };
          const start = Math.max(this.context.currentTime + 0.05, this.nextTime || 0);
          this.nextTime = start + audio.duration; source.start(start); onAudio();
        }
        if (pending.length > 65536) throw new Error('Voice preview frame is too large.');
        if (done) break;
      }
      if (!result || !totalSamples || pending.trim()) throw new Error('Voice preview stream ended before completion.');
      if (this.sources.size && !await new Promise(resolve => { this.finish = resolve; })) return null;
      if (version !== this.version) return null;
      return result;
    } catch (error) {
      if (version !== this.version) return null;
      this.clear(); throw error;
    } finally {
      await reader.cancel().catch(() => {});
      if (version === this.version) { this.controller = null; this.reader = null; this.finish = null; }
    }
  }
}
