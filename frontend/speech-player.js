/** Ordered phrase playback, with gain above 100% and cancellation on voice changes. */
export class SpeechPlayer {
  constructor(onError) { this.onError = onError; this.queue = []; this.version = 0; this.volume = 1; this.muted = false; }
  async unlock() {
    if (!this.context) {
      this.context = new AudioContext(); this.gain = this.context.createGain(); this.gain.connect(this.context.destination);
    }
    await this.context.resume(); this.applyVolume();
  }
  applyVolume() { if (this.gain) this.gain.gain.value = this.muted ? 0 : this.volume; }
  setVolume(value) { this.volume = value; this.applyVolume(); }
  setMuted(value) { this.muted = value; if (value) this.clear(); this.applyVolume(); }
  clear() { this.version++; this.cancelPlayback?.(); this.cancelPlayback = null; this.queue = []; if (this.current) { this.current.onended = null; try { this.current.stop(); } catch {} this.current = null; } this.busy = false; }
  async play(url) {
    const version = this.version;
    if (this.muted) return false;
    await this.unlock();
    if (version !== this.version) return false;
    const response = await fetch(url);
    if (!response.ok) throw new Error('Recorded audio could not be loaded.');
    const audio = await this.context.decodeAudioData(await response.arrayBuffer());
    if (version !== this.version) return false;
    const source = this.context.createBufferSource(); this.current = source;
    source.buffer = audio; source.connect(this.gain);
    return new Promise(resolve => {
      this.cancelPlayback = () => resolve(false);
      source.onended = () => {
        if (version === this.version) { this.current = null; this.cancelPlayback = null; resolve(true); }
      };
      source.start();
    });
  }
  enqueue(url) {
    if (this.muted) return;
    if (this.queue.length >= 12) { this.onError('Audio playback is falling behind. Clear the audio queue or mute spoken translation.'); return; }
    this.queue.push(url); this.playNext();
  }
  async playNext() {
    if (this.busy || !this.queue.length || this.muted) return;
    this.busy = true; const version = this.version; const url = this.queue.shift();
    try {
      await this.unlock();
      const response = await fetch(url);
      if (!response.ok) throw new Error('Translated audio could not be loaded.');
      const audio = await this.context.decodeAudioData(await response.arrayBuffer());
      if (version !== this.version) return;
      const source = this.context.createBufferSource(); this.current = source;
      source.buffer = audio; source.connect(this.gain);
      source.onended = () => { if (version === this.version) { this.busy = false; this.current = null; this.playNext(); } };
      source.start();
    } catch (error) {
      if (version === this.version) { this.busy = false; this.onError(error.message); this.playNext(); }
    }
  }
}
