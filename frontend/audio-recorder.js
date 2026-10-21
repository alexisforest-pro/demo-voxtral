/** Resample captured audio to 16 kHz and emit independent PCM WAV buffers. */
export function encodeWav(samples) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const write = (offset, text) => [...text].forEach((char, i) => view.setUint8(offset + i, char.charCodeAt(0)));
  write(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); write(8, 'WAVE');
  write(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, 16000, true); view.setUint32(28, 32000, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  samples.forEach((sample, i) => view.setInt16(44 + i * 2, Math.max(-32768, Math.min(32767, sample * 32768)), true));
  return new Blob([buffer], { type: 'audio/wav' });
}

export class AudioRecorder {
  constructor({ seconds = 0, source = 'microphone', deviceId = '', maxSeconds = 0,
    onChunk = () => {}, onLevel = () => {}, onEnded = () => {} } = {}) {
    this.seconds = seconds; this.source = source; this.deviceId = deviceId; this.maxSeconds = maxSeconds;
    this.onChunk = onChunk; this.onLevel = onLevel; this.onEnded = onEnded;
    this.samples = []; this.sum = 0; this.count = 0; this.phase = 0; this.accepting = false;
    this.capturedSamples = 0; this.signalEnergy = 0; this.audibleSamples = 0; this.clippedSamples = 0;
  }
  async start({ paused = false } = {}) {
    if (!window.isSecureContext || !navigator.mediaDevices) {
      throw new Error('Audio capture requires localhost or HTTPS and a supported browser.');
    }
    try {
      if (this.source === 'tab') {
        if (!navigator.mediaDevices.getDisplayMedia) throw new Error('Tab audio sharing is unavailable. Use a recent desktop Chrome or Edge, or select a loopback audio input.');
        // Browsers require a visual surface for tab audio; only audio is processed or saved.
        this.stream = await navigator.mediaDevices.getDisplayMedia({
          video: { displaySurface: 'browser' },
          audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
          selfBrowserSurface: 'exclude', systemAudio: 'exclude', preferCurrentTab: false,
        });
        const surface = this.stream.getVideoTracks()[0]?.getSettings().displaySurface;
        if (surface && surface !== 'browser') throw new Error('Select the Teams browser tab, not a window or entire screen, and enable Share tab audio. For desktop Teams, use a loopback audio input.');
        if (!this.stream.getAudioTracks().length) throw new Error('No shared audio. Select the Teams browser tab and enable Share tab audio. For desktop Teams, use a loopback audio input.');
      } else {
        if (this.source === 'loopback' && !this.deviceId) throw new Error('Select your loopback audio input first. Use Refresh audio inputs to list devices.');
        const processed = this.source !== 'loopback';
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: {
          channelCount: 1, echoCancellation: processed, noiseSuppression: processed, autoGainControl: processed,
          ...(this.deviceId ? { deviceId: { exact: this.deviceId } } : {}),
        } });
      }
      this.context = new AudioContext();
      if (!this.context.audioWorklet) throw new Error('This browser does not support AudioWorklet. Use a recent Chrome, Edge, Firefox, or Safari.');
      await this.context.audioWorklet.addModule('/audio-worklet.js');
      this.input = this.context.createMediaStreamSource(new MediaStream(this.stream.getAudioTracks()));
      this.worklet = new AudioWorkletNode(this.context, 'microphone-processor', { channelCount: 1, channelCountMode: 'explicit' });
      this.silence = this.context.createGain(); this.silence.gain.value = 0;
      this.input.connect(this.worklet); this.worklet.connect(this.silence); this.silence.connect(this.context.destination);
      this.endCapture = () => { if (this.accepting) { this.accepting = false; this.onEnded(); } };
      this.stream.getTracks().forEach(track => track.addEventListener('ended', this.endCapture));
      this.accepting = !paused;
      this.worklet.port.onmessage = ({ data }) => this.capture(data);
      await this.context.resume();
      if (this.stream.getTracks().some(track => track.readyState === 'ended')) throw new Error('Audio sharing ended before recording started. Record again and keep sharing active.');
    } catch (error) {
      await this.stop(false);
      if (this.source === 'tab' && error.name === 'NotAllowedError') throw new Error('Sharing was cancelled or denied. Record again, select the Teams browser tab, and enable Share tab audio.');
      throw error;
    }
  }
  sampleProblem() {
    if (this.capturedSamples < 48000) return 'Record at least three seconds for a voice sample.';
    if (Math.sqrt(this.signalEnergy / this.capturedSamples) < 0.001 || this.audibleSamples < 1600) return 'No usable audio detected. Check Share tab audio or loopback routing and ask the speaker to speak again.';
    if (this.clippedSamples / this.capturedSamples > 0.01) return 'The sample is heavily clipped. Lower the input volume and record again.';
    return null;
  }
  capture(data) {
    if (!this.accepting) return;
    let energy = 0;
    for (const sample of data) {
      energy += sample * sample;
      this.sum += sample; this.count++; this.phase += 16000;
      if (this.phase >= this.context.sampleRate) {
        this.phase -= this.context.sampleRate;
        const value = this.sum / this.count;
        this.samples.push(value); this.sum = 0; this.count = 0;
        this.capturedSamples++; this.signalEnergy += value * value;
        if (Math.abs(value) >= 0.003) this.audibleSamples++;
        if (Math.abs(value) >= 0.99) this.clippedSamples++;
        if (this.seconds && this.samples.length >= this.seconds * 16000) this.flush();
        if (this.maxSeconds && this.capturedSamples >= this.maxSeconds * 16000) {
          this.accepting = false; this.onEnded(); return;
        }
      }
    }
    this.onLevel(Math.sqrt(energy / data.length));
  }
  flush() {
    if (!this.samples.length || (!this.seconds && this.samples.length < 1600)) { this.samples = []; return null; }
    const blob = encodeWav(this.samples); this.samples = [];
    this.onChunk(blob); return blob;
  }
  async stop(flush = true) {
    this.accepting = false;
    const blob = flush ? this.flush() : null;
    this.stream?.getTracks().forEach(track => {
      if (this.endCapture) track.removeEventListener('ended', this.endCapture);
      track.stop();
    });
    this.input?.disconnect(); this.worklet?.disconnect(); this.silence?.disconnect();
    if (this.context && this.context.state !== 'closed') await this.context.close();
    this.onLevel(0); return blob;
  }
}
