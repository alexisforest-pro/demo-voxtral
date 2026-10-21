/* AudioWorklet captures mono samples without deprecated ScriptProcessorNode. */
class MicrophoneProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (channel) this.port.postMessage(channel.slice());
    return true;
  }
}
registerProcessor('microphone-processor', MicrophoneProcessor);
