// Bound memory and latency: one 2048-sample packet, no playback on the phone.
class CastPCM extends AudioWorkletProcessor {
  constructor() { super(); this.samples = new Int16Array(2048); this.offset = 0; }
  process(inputs) {
    const channels = inputs[0];
    if (!channels || !channels.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let value = 0;
      for (const channel of channels) value += channel[i] / channels.length;
      this.samples[this.offset++] = Math.round(Math.max(-1, Math.min(1, value)) * 32767);
      if (this.offset === this.samples.length) {
        const packet = new ArrayBuffer(this.samples.length * 2);
        const view = new DataView(packet);
        for (let j = 0; j < this.samples.length; j++) view.setInt16(j * 2, this.samples[j], true);
        this.port.postMessage(packet, [packet]);
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor('cast-pcm', CastPCM);
