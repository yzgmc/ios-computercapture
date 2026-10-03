/**
 * PhoneCam 网页端客户端。
 *
 * 协议与 iOS 端完全一致，桌面端因此可以零改动复用既有解码链路：
 * - 视频 WebSocket：每条二进制消息 = 28B RAW1 头 + payload
 *     magic(4)="RAW1" | frame_id(4) | width(4) | height(4)
 *     | format(4) | bytes_per_row(4) | payload_length(4)   （全大端）
 *     format: 0=BGRA / 10=JPEG / 20=H.264 Annex-B
 * - 音频 WebSocket：每条二进制消息 = 16B AUD1 头 + PCM16LE payload
 *     magic(4)="AUD1" | seq(4) | sample_rate(4) | channels(1)
 *     | format(1) | payload_length(2)                      （全大端）
 *
 * 视频编码按能力三级降级：
 *   H.264（WebCodecs 硬编，最优）→ JPEG 抓帧 → BGRA 原始帧
 */
(function () {
  'use strict';

  var RAW1_HEADER_SIZE = 28;
  var AUD1_HEADER_SIZE = 16;
  var AUD_MAX_PAYLOAD = 1456;      // 与 iOS 端一致：单包控制在 MTU 友好范围

  var FORMAT_BGRA = 0;
  var FORMAT_JPEG = 10;
  var FORMAT_H264 = 20;
  var AUDIO_PCM16LE = 0;

  var START_CODE = new Uint8Array([0, 0, 0, 1]);

  function el(id) { return document.getElementById(id); }

  var state = {
    running: false,
    wsVideo: null,
    wsAudio: null,
    stream: null,
    videoEl: null,
    canvas: null,
    ctx2d: null,
    encoder: null,
    audioCtx: null,
    workletNode: null,
    audioSource: null,
    frameId: 0,
    audioSeq: 0,
    lastDescription: null,
    framesSinceKey: 0,
    encodingBusy: false,
    rafId: 0,
    timerId: 0,
    sentFrames: 0,
    dropped: 0,
    bytesWindow: 0,
    statStart: 0,
    statFrames: 0,
    mode: 'h264',
    width: 1280,
    height: 720,
    fps: 30,
    facing: 'user',
    sampleRate: 48000,
    channels: 1,
    audioEnabled: true
  };

  // ------------------------------------------------------------------ //
  // 二进制封装
  // ------------------------------------------------------------------ //

  function packVideo(payload, width, height, format, bytesPerRow) {
    var buf = new ArrayBuffer(RAW1_HEADER_SIZE + payload.byteLength);
    var view = new DataView(buf);
    var out = new Uint8Array(buf);

    out[0] = 0x52; out[1] = 0x41; out[2] = 0x57; out[3] = 0x31; // "RAW1"
    view.setUint32(4, state.frameId >>> 0, false);
    view.setUint32(8, width, false);
    view.setUint32(12, height, false);
    view.setUint32(16, format, false);
    view.setUint32(20, bytesPerRow, false);
    view.setUint32(24, payload.byteLength, false);
    out.set(payload, RAW1_HEADER_SIZE);

    state.frameId = (state.frameId + 1) >>> 0;
    return buf;
  }

  function packAudio(payload, sampleRate, channels) {
    var buf = new ArrayBuffer(AUD1_HEADER_SIZE + payload.byteLength);
    var view = new DataView(buf);
    var out = new Uint8Array(buf);

    out[0] = 0x41; out[1] = 0x55; out[2] = 0x44; out[3] = 0x31; // "AUD1"
    view.setUint32(4, state.audioSeq >>> 0, false);
    view.setUint32(8, sampleRate, false);
    out[12] = channels & 0xFF;
    out[13] = AUDIO_PCM16LE;
    view.setUint16(14, payload.byteLength, false);
    out.set(payload, AUD1_HEADER_SIZE);

    state.audioSeq = (state.audioSeq + 1) >>> 0;
    return buf;
  }

  function sendVideo(payload, width, height, format, bytesPerRow) {
    var ws = state.wsVideo;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    // 背压：发送缓冲积压过多时丢帧，宁可掉帧也不要堆积延迟
    if (ws.bufferedAmount > 2 * 1024 * 1024) {
      state.dropped++;
      return false;
    }
    var packet = packVideo(payload, width, height, format, bytesPerRow);
    state.bytesWindow += packet.byteLength;
    state.sentFrames++;
    state.statFrames++;
    ws.send(packet);
    return true;
  }

  function sendAudioChunk(bytes) {
    var ws = state.wsAudio;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (ws.bufferedAmount > 512 * 1024) return;  // UDP 式处理：丢掉即可
    ws.send(packAudio(bytes, state.sampleRate, state.channels));
  }

  // ------------------------------------------------------------------ //
  // H.264：AVCC → Annex-B
  // ------------------------------------------------------------------ //

  function isAnnexB(u8) {
    return u8.length >= 4 && u8[0] === 0 && u8[1] === 0 && u8[2] === 0 && u8[3] === 1;
  }

  /**
   * 解析 avcC（decoderConfig.description），取出 SPS / PPS 与 NAL 长度字段字节数。
   * 浏览器即使配置了 annexb，某些版本仍会吐 AVCC，这里统一兜底转换。
   */
  function parseAvcC(desc) {
    var result = { nalLengthSize: 4, sps: [], pps: [] };
    if (!desc || desc.byteLength < 6) return result;

    var d = desc instanceof Uint8Array ? desc : new Uint8Array(desc);
    result.nalLengthSize = (d[4] & 0x03) + 1;

    var offset = 5;
    var numSps = d[offset] & 0x1F;
    offset++;
    for (var i = 0; i < numSps && offset + 2 <= d.length; i++) {
      var len = (d[offset] << 8) | d[offset + 1];
      offset += 2;
      if (offset + len > d.length) break;
      result.sps.push(d.subarray(offset, offset + len));
      offset += len;
    }
    if (offset >= d.length) return result;
    var numPps = d[offset] & 0x1F;
    offset++;
    for (var j = 0; j < numPps && offset + 2 <= d.length; j++) {
      var plen = (d[offset] << 8) | d[offset + 1];
      offset += 2;
      if (offset + plen > d.length) break;
      result.pps.push(d.subarray(offset, offset + plen));
      offset += plen;
    }
    return result;
  }

  function toAnnexB(chunk, isKey) {
    var data = new Uint8Array(chunk.byteLength);
    chunk.copyTo(data);
    if (isAnnexB(data)) return data;   // 已是 Annex-B，直接透传

    var parsed = parseAvcC(state.lastDescription);
    var parts = [];
    var i;

    if (isKey) {
      for (i = 0; i < parsed.sps.length; i++) parts.push(START_CODE, parsed.sps[i]);
      for (i = 0; i < parsed.pps.length; i++) parts.push(START_CODE, parsed.pps[i]);
    }

    var offset = 0;
    var nls = parsed.nalLengthSize;
    while (offset + nls <= data.length) {
      var len = 0;
      for (var k = 0; k < nls; k++) len = (len << 8) | data[offset + k];
      offset += nls;
      if (len <= 0 || offset + len > data.length) break;
      parts.push(START_CODE, data.subarray(offset, offset + len));
      offset += len;
    }
    if (!parts.length) return data;

    var total = 0;
    for (i = 0; i < parts.length; i++) total += parts[i].length;
    var out = new Uint8Array(total);
    var pos = 0;
    for (i = 0; i < parts.length; i++) { out.set(parts[i], pos); pos += parts[i].length; }
    return out;
  }

  // ------------------------------------------------------------------ //
  // 视频编码路径
  // ------------------------------------------------------------------ //

  function pickMode() {
    var sel = el('codec').value;
    if (sel !== 'auto') return sel;
    if (typeof VideoEncoder !== 'undefined') return 'h264';
    return 'jpeg';
  }

  async function setupEncoder(width, height, fps) {
    if (typeof VideoEncoder === 'undefined') return null;

    var codecs = ['avc1.42001f', 'avc1.4d0028', 'avc1.640028'];
    var chosen = null;
    for (var i = 0; i < codecs.length; i++) {
      try {
        var support = await VideoEncoder.isConfigSupported({
          codec: codecs[i], width: width, height: height,
          bitrate: 4e6, framerate: fps
        });
        if (support && support.supported) { chosen = codecs[i]; break; }
      } catch (e) { /* 该 codec 不支持，试下一个 */ }
    }
    if (!chosen) return null;

    var encoder = new VideoEncoder({
      output: function (chunk, metadata) {
        if (metadata && metadata.decoderConfig && metadata.decoderConfig.description) {
          state.lastDescription = new Uint8Array(metadata.decoderConfig.description);
        }
        try {
          var annexb = toAnnexB(chunk, chunk.type === 'key');
          sendVideo(annexb, width, height, FORMAT_H264, 0);
        } catch (e) {
          console.warn('encode output error', e);
        } finally {
          try { chunk.close(); } catch (e) { /* 已关闭 */ }
        }
      },
      error: function (e) {
        console.error('VideoEncoder error', e);
        setStatus('编码器错误，已停止', 'err');
        stop();
      }
    });

    var config = {
      codec: chosen,
      width: width,
      height: height,
      bitrate: Math.min(16e6, Math.max(1.5e6, width * height * fps * 0.10)),
      framerate: fps,
      latencyMode: 'realtime'
    };
    // 优先让编码器直接吐 Annex-B（桌面端 PyAV 按 Annex-B 解码）
    try {
      config.avc = { format: 'annexb' };
      encoder.configure(config);
    } catch (e) {
      delete config.avc;
      encoder.configure(config);   // 老实现不支持该选项，回退后由 toAnnexB 转换
    }
    return encoder;
  }

  function ensureCanvas(width, height) {
    if (!state.canvas) {
      state.canvas = document.createElement('canvas');
      state.ctx2d = state.canvas.getContext('2d', { alpha: false });
    }
    if (state.canvas.width !== width || state.canvas.height !== height) {
      state.canvas.width = width;
      state.canvas.height = height;
    }
    return state.ctx2d;
  }

  function frameFromVideo() {
    var v = state.videoEl;
    if (!v || v.readyState < 2 || !v.videoWidth) return null;
    var w = v.videoWidth, h = v.videoHeight;
    state.width = w; state.height = h;
    return { w: w, h: h };
  }

  function encodeH264Tick() {
    var dim = frameFromVideo();
    if (!dim || !state.encoder) return;
    var frame;
    try {
      frame = new VideoFrame(state.videoEl, { timestamp: performance.now() * 1000 });
    } catch (e) {
      return;
    }
    // 每 60 帧强制一个关键帧：便于桌面端断线重连后快速恢复
    var forceKey = state.framesSinceKey >= 60;
    state.framesSinceKey = forceKey ? 0 : state.framesSinceKey + 1;
    try {
      state.encoder.encode(frame, { keyFrame: forceKey });
    } catch (e) {
      console.warn('encode failed', e);
    } finally {
      frame.close();
    }
  }

  function encodeJPEGTick() {
    if (state.encodingBusy) { state.dropped++; return; }
    var dim = frameFromVideo();
    if (!dim) return;
    var ctx = ensureCanvas(dim.w, dim.h);
    ctx.drawImage(state.videoEl, 0, 0, dim.w, dim.h);

    state.encodingBusy = true;
    state.canvas.toBlob(function (blob) {
      state.encodingBusy = false;
      if (!blob || !state.running) return;
      var reader = new FileReader();
      reader.onloadend = function () {
        sendVideo(new Uint8Array(reader.result), dim.w, dim.h, FORMAT_JPEG, 0);
      };
      reader.readAsArrayBuffer(blob);
    }, 'image/jpeg', 0.75);
  }

  function encodeBGRATick() {
    var dim = frameFromVideo();
    if (!dim) return;
    var ctx = ensureCanvas(dim.w, dim.h);
    ctx.drawImage(state.videoEl, 0, 0, dim.w, dim.h);
    var img = ctx.getImageData(0, 0, dim.w, dim.h);
    var src = img.data;                       // RGBA
    var out = new Uint8Array(src.length);     // 转 BGRA：桌面端 format=0 期望 BGRA
    for (var i = 0; i < src.length; i += 4) {
      out[i] = src[i + 2];
      out[i + 1] = src[i + 1];
      out[i + 2] = src[i];
      out[i + 3] = 255;
    }
    sendVideo(out, dim.w, dim.h, FORMAT_BGRA, dim.w * 4);
  }

  function tick() {
    if (!state.running) return;
    try {
      if (state.mode === 'h264') encodeH264Tick();
      else if (state.mode === 'jpeg') encodeJPEGTick();
      else encodeBGRATick();
    } catch (e) {
      console.warn('tick error', e);
    }
    scheduleTick();
  }

  function scheduleTick() {
    var v = state.videoEl;
    // requestVideoFrameCallback 能对齐真实采集节奏，比定时器更稳
    if (v && typeof v.requestVideoFrameCallback === 'function') {
      state.rafId = v.requestVideoFrameCallback(tick);
      return;
    }
    state.timerId = setTimeout(tick, Math.max(16, 1000 / state.fps));
  }

  function cancelTick() {
    var v = state.videoEl;
    if (state.rafId && v && typeof v.cancelVideoFrameCallback === 'function') {
      try { v.cancelVideoFrameCallback(state.rafId); } catch (e) { /* 已触发 */ }
    }
    state.rafId = 0;
    if (state.timerId) { clearTimeout(state.timerId); state.timerId = 0; }
  }

  // ------------------------------------------------------------------ //
  // 音频采集
  // ------------------------------------------------------------------ //

  async function startAudio() {
    if (!state.audioEnabled) return;
    if (typeof AudioContext === 'undefined') {
      setStatus('浏览器不支持 WebAudio，音频已跳过', 'err');
      return;
    }
    state.audioCtx = new AudioContext({ latencyHint: 'interactive' });
    state.sampleRate = state.audioCtx.sampleRate || 48000;
    state.channels = 1;

    try {
      await state.audioCtx.resume();
      await state.audioCtx.audioWorklet.addModule('audio-worklet.js');
    } catch (e) {
      console.warn('AudioWorklet 加载失败，音频跳过', e);
      setStatus('音频不可用（AudioWorklet 加载失败）', 'err');
      return;
    }

    var node = new AudioWorkletNode(state.audioCtx, 'pcm-capture', {
      numberOfInputs: 1, numberOfOutputs: 1,
      outputChannelCount: [1], channelCount: 1, channelCountMode: 'explicit'
    });
    state.workletNode = node;

    node.port.onmessage = function (event) {
      var f32 = event.data;
      if (!f32 || !f32.length) return;
      var i16 = new Int16Array(f32.length);
      for (var i = 0; i < f32.length; i++) {
        var s = Math.max(-1, Math.min(1, f32[i]));
        i16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }
      var bytes = new Uint8Array(i16.buffer);
      var offset = 0;
      while (offset < bytes.length) {
        var end = Math.min(offset + AUD_MAX_PAYLOAD, bytes.length);
        sendAudioChunk(bytes.subarray(offset, end));
        offset = end;
      }
    };

    state.audioSource = state.audioCtx.createMediaStreamSource(state.stream);
    state.audioSource.connect(node);
    // 部分浏览器只有连到 destination 才会驱动 worklet，用零增益避免回声
    var mute = state.audioCtx.createGain();
    mute.gain.value = 0;
    node.connect(mute);
    mute.connect(state.audioCtx.destination);
  }

  // ------------------------------------------------------------------ //
  // 连接与生命周期
  // ------------------------------------------------------------------ //

  function wsUrl(kind) {
    var host = (el('host').value || '').trim();
    var port = (el('port').value || '8443').trim();
    if (!host) host = location.hostname;
    return 'wss://' + host + ':' + port + '/ws/' + kind;
  }

  function connectWS(kind) {
    return new Promise(function (resolve, reject) {
      var ws;
      try {
        ws = new WebSocket(wsUrl(kind));
      } catch (e) {
        reject(e);
        return;
      }
      ws.binaryType = 'arraybuffer';
      ws.onopen = function () { resolve(ws); };
      ws.onerror = function () {
        reject(new Error('无法连接 ' + kind + ' 通道（证书未信任或地址错误）'));
      };
      ws.onclose = function () {
        if (state.running) {
          setStatus('连接断开', 'err');
          stop();
        }
      };
      setTimeout(function () {
        if (ws.readyState !== WebSocket.OPEN) {
          try { ws.close(); } catch (e) { /* 忽略 */ }
          reject(new Error('连接超时'));
        }
      }, 8000);
    });
  }

  function setStatus(text, kind) {
    el('status').textContent = text;
    var dot = el('dot');
    dot.className = 'dot' + (kind === 'on' ? ' on' : kind === 'err' ? ' err' : '');
  }

  async function start() {
    if (state.running) return;

    state.mode = pickMode();
    state.fps = parseInt(el('fps').value, 10) || 30;
    state.facing = el('facing').value;
    state.audioEnabled = el('audio').value === '1';
    var targetH = parseInt(el('resolution').value, 10) || 720;

    setStatus('正在连接…');
    try {
      state.wsVideo = await connectWS('video');
      if (state.audioEnabled) {
        try {
          state.wsAudio = await connectWS('audio');
        } catch (e) {
          console.warn('音频通道连接失败，继续仅推视频', e);
          state.wsAudio = null;
        }
      }
    } catch (e) {
      setStatus(e.message || '连接失败', 'err');
      return;
    }

    setStatus('正在获取摄像头权限…');
    try {
      state.stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: state.facing,
          width: { ideal: Math.round(targetH * 16 / 9) },
          height: { ideal: targetH },
          frameRate: { ideal: state.fps }
        },
        audio: state.audioEnabled
          ? { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
          : false
      });
    } catch (e) {
      setStatus('摄像头/麦克风权限被拒绝', 'err');
      try { state.wsVideo.close(); } catch (err) { /* 忽略 */ }
      return;
    }

    state.running = true;
    state.videoEl = el('preview');
    state.videoEl.srcObject = state.stream;
    try { await state.videoEl.play(); } catch (e) { /* 自动播放策略兜底 */ }

    if (state.mode === 'h264') {
      var dim = frameFromVideo();
      state.encoder = await setupEncoder(
        (dim && dim.w) || Math.round(targetH * 16 / 9),
        (dim && dim.h) || targetH,
        state.fps);
      if (!state.encoder) {
        state.mode = 'jpeg';   // 无 WebCodecs 或 H.264 不可用，降级
      }
    }

    await startAudio();

    state.statStart = performance.now();
    state.statFrames = 0;
    state.bytesWindow = 0;
    scheduleTick();

    el('toggle').textContent = '停止共享';
    el('toggle').className = 'stop';
    setStatus('已连接 · ' + state.mode.toUpperCase(), 'on');
    startStats();
  }

  function stop() {
    if (!state.running) return;
    state.running = false;
    cancelTick();

    if (state.encoder) {
      try { state.encoder.close(); } catch (e) { /* 忽略 */ }
      state.encoder = null;
    }
    if (state.workletNode) {
      try { state.workletNode.port.postMessage({ type: 'stop' }); } catch (e) { /* 忽略 */ }
      try { state.workletNode.disconnect(); } catch (e) { /* 忽略 */ }
      state.workletNode = null;
    }
    if (state.audioSource) {
      try { state.audioSource.disconnect(); } catch (e) { /* 忽略 */ }
      state.audioSource = null;
    }
    if (state.audioCtx) {
      try { state.audioCtx.close(); } catch (e) { /* 忽略 */ }
      state.audioCtx = null;
    }
    if (state.stream) {
      state.stream.getTracks().forEach(function (t) { t.stop(); });
      state.stream = null;
    }
    if (state.videoEl) { state.videoEl.srcObject = null; }
    if (state.wsVideo) { try { state.wsVideo.close(); } catch (e) { /* 忽略 */ } state.wsVideo = null; }
    if (state.wsAudio) { try { state.wsAudio.close(); } catch (e) { /* 忽略 */ } state.wsAudio = null; }

    el('toggle').textContent = '开始共享';
    el('toggle').className = '';
    setStatus('已停止');
  }

  // ------------------------------------------------------------------ //
  // 统计
  // ------------------------------------------------------------------ //

  var statsTimer = 0;
  function startStats() {
    if (statsTimer) clearInterval(statsTimer);
    statsTimer = setInterval(function () {
      var now = performance.now();
      var elapsed = (now - state.statStart) / 1000;
      if (elapsed <= 0) return;
      el('statFps').textContent = (state.statFrames / elapsed).toFixed(0);
      el('statBw').textContent = (state.bytesWindow * 8 / elapsed / 1e6).toFixed(1) + ' Mbps';
      el('statDrop').textContent = String(state.dropped);
      state.statStart = now;
      state.statFrames = 0;
      state.bytesWindow = 0;
    }, 1000);
  }

  // ------------------------------------------------------------------ //
  // 初始化
  // ------------------------------------------------------------------ //

  function init() {
    el('host').value = location.hostname || '';

    var warn = el('capWarn');
    var notes = [];
    if (!window.isSecureContext) {
      notes.push('当前不是安全上下文，摄像头不可用。请通过 https:// 访问本页。');
    }
    if (typeof VideoEncoder === 'undefined') {
      notes.push('浏览器不支持 WebCodecs，视频将降级为 JPEG 抓帧（带宽与帧率较低）。');
    }
    if (typeof AudioWorkletNode === 'undefined') {
      notes.push('浏览器不支持 AudioWorklet，麦克风音频不可用。');
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      notes.push('浏览器不支持 getUserMedia，无法采集摄像头。');
    }
    if (notes.length) {
      warn.style.display = 'block';
      warn.innerHTML = notes.join('<br>');
    }

    el('toggle').addEventListener('click', function () {
      if (state.running) stop(); else start();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
