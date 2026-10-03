/**
 * AudioWorklet 处理器：把麦克风 Float32 采样按块送回主线程。
 *
 * 为什么不用 ScriptProcessorNode：它已废弃且强制在主线程跑，容易卡顿。
 * AudioWorklet 在音频线程运行，延迟更低。
 *
 * 主线程收到后再转成 PCM16LE 并封装 AUD1 帧头（见 app.js）。
 */
class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // 每块采样数：1024 帧 @48kHz ≈ 21ms，兼顾延迟与包数量
    this.blockSize = 1024;
    this.buffer = new Float32Array(this.blockSize);
    this.filled = 0;
    this.running = true;

    this.port.onmessage = (event) => {
      if (event.data && event.data.type === 'stop') {
        this.running = false;
      }
    };
  }

  process(inputs) {
    if (!this.running) return false;

    const input = inputs[0];
    if (!input || input.length === 0) return true;

    // 只取第一声道（桌面端按 mono 播放）
    const channel = input[0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      this.buffer[this.filled++] = channel[i];
      if (this.filled >= this.blockSize) {
        // 拷贝一份再发送：buffer 会被后续采样覆写
        this.port.postMessage(this.buffer.slice(0));
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor('pcm-capture', PCMCaptureProcessor);
