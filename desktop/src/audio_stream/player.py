"""UDP 音频播放器：基于 PyAudio 的回调式输出。

收到 PCM 数据后入队，PyAudio 回调从队列取数据写入虚拟/物理输出设备。
设备格式（采样率/声道/格式）由首个到达的音频包决定；如后续包格式变化会重建流。
"""
import logging
import queue
import threading

import numpy as np

logger = logging.getLogger(__name__)


class AudioPlayer:
    """PCM 数据 → PyAudio 输出流。

    使用 pyaudio 回调式输出：底层需要数据时从 _queue 取，队列空时输出静音。
    """

    def __init__(self, output_device_index=None):
        self.output_device_index = output_device_index
        self._pa = None
        self._stream = None
        self._queue: queue.Queue = queue.Queue(maxsize=60)
        self._lock = threading.Lock()
        self._running = False
        # 当前流的格式；为 None 表示尚未收到任何音频包
        self._sample_rate = None
        self._channels = None
        self._sample_bytes = None  # 每个采样点字节数（如 PCM16=2, Float32=4）
        # 接收统计
        self._packets = 0
        self._underruns = 0
        # 软件音量 0.0~1.0，应用到 PCM 后再写入队列
        self._volume = 1.0

    def start(self):
        """初始化 PyAudio，但不立即打开输出流（等首个包到达再开）。"""
        try:
            import pyaudio
            self._pa = pyaudio.PyAudio()
            self._running = True
            logger.info("AudioPlayer initialized (output_device=%s)",
                        self.output_device_index)
        except Exception as e:
            logger.error("Failed to init PyAudio: %s", e)
            self._running = False

    def stop(self):
        """停止播放并释放资源。"""
        self._running = False
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception as e:
                    logger.warning("Audio stream close error: %s", e)
                self._stream = None
            if self._pa is not None:
                try:
                    self._pa.terminate()
                except Exception as e:
                    logger.warning("PyAudio terminate error: %s", e)
                self._pa = None
            self._sample_rate = None
            self._channels = None
            self._sample_bytes = None
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    def set_volume(self, volume: float):
        """设置软件音量 0.0~1.0，应用到后续入队的 PCM 数据。"""
        self._volume = max(0.0, min(1.0, float(volume)))

    def feed(self, pcm_data: bytes, sample_rate: int, channels: int,
             audio_format: int):
        """将一帧 PCM 数据入队，必要时重建输出流以匹配新格式。"""
        if not self._running:
            return
        try:
            import pyaudio
        except Exception as e:
            logger.error("PyAudio not available: %s", e)
            return

        pa_format, sample_bytes = self._to_pa_format(audio_format)
        if pa_format is None:
            logger.warning("Unsupported audio format: %d", audio_format)
            return

        # 格式变化时重建流
        need_rebuild = (
            self._stream is None
            or sample_rate != self._sample_rate
            or channels != self._channels
            or sample_bytes != self._sample_bytes
        )
        if need_rebuild:
            with self._lock:
                # 二次检查，避免多包同时触发重建
                if (self._stream is None
                        or sample_rate != self._sample_rate
                        or channels != self._channels
                        or sample_bytes != self._sample_bytes):
                    self._rebuild_stream(sample_rate, channels, pa_format,
                                         sample_bytes)

        self._packets += 1
        if self._packets == 1:
            logger.info("AudioPlayer first packet: %dHz %dch format=%d",
                        sample_rate, channels, audio_format)

        # 应用软件音量（PCM16 / Float32 都按浮点缩放后回写）
        out = self._apply_volume(pcm_data, audio_format)
        try:
            self._queue.put_nowait(out)
        except queue.Full:
            # 丢最旧帧，保实时
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(out)
            except queue.Empty:
                pass

    def _apply_volume(self, pcm_data: bytes, audio_format: int) -> bytes:
        """按当前 _volume 缩放 PCM 数据。volume=1.0 时直接返回原数据。"""
        if self._volume >= 0.999:
            return pcm_data
        if self._volume <= 0.001:
            return b"\x00" * len(pcm_data)
        from .protocol import AudioFormat
        try:
            if audio_format == AudioFormat.PCM16_LE:
                arr = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
                arr = np.clip(arr * self._volume, -32768, 32767).astype(np.int16)
                return arr.tobytes()
            if audio_format == AudioFormat.PCM_FLOAT32_LE:
                arr = np.frombuffer(pcm_data, dtype=np.float32)
                arr = np.clip(arr * self._volume, -1.0, 1.0).astype(np.float32)
                return arr.tobytes()
        except Exception as e:
            logger.warning("apply_volume failed: %s", e)
        return pcm_data

    def _rebuild_stream(self, sample_rate: int, channels: int, pa_format: int,
                        sample_bytes: int):
        """关闭旧流并按新格式打开输出流。调用方须持有 _lock。"""
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception as e:
                logger.warning("Old audio stream close error: %s", e)
            self._stream = None

        if self._pa is None:
            return

        try:
            self._stream = self._pa.open(
                format=pa_format,
                channels=channels,
                rate=sample_rate,
                output=True,
                output_device_index=self.output_device_index,
                frames_per_buffer=1024,
                stream_callback=self._audio_callback,
            )
            self._sample_rate = sample_rate
            self._channels = channels
            self._sample_bytes = sample_bytes
            logger.info("Audio stream opened: %dHz %dch sample_bytes=%d",
                        sample_rate, channels, sample_bytes)
        except Exception as e:
            logger.error("Failed to open audio stream: %s", e)
            self._stream = None

    @staticmethod
    def _to_pa_format(audio_format: int):
        """返回 (pyaudio_format, sample_bytes) 或 (None, None)。"""
        try:
            import pyaudio
        except Exception:
            return None, None
        from .protocol import AudioFormat
        if audio_format == AudioFormat.PCM16_LE:
            return pyaudio.paInt16, 2
        if audio_format == AudioFormat.PCM_FLOAT32_LE:
            return pyaudio.paFloat32, 4
        return None, None

    def _audio_callback(self, in_data, frame_count, time_info, status):
        try:
            import pyaudio
            data = self._queue.get_nowait()
            bytes_per_frame = self._bytes_per_frame()
            need = frame_count * bytes_per_frame
            if len(data) < need:
                # 不足时补静音
                data = data + b"\x00" * (need - len(data))
            elif len(data) > need:
                # 一包大于所需，截取前 frame_count 帧，多余回填队列
                extra = data[need:]
                data = data[:need]
                try:
                    self._queue.put_nowait(extra)
                except queue.Full:
                    pass
            return (data, pyaudio.paContinue)
        except queue.Empty:
            self._underruns += 1
            return (b"\x00" * frame_count * self._bytes_per_frame(),
                    __import__("pyaudio").paContinue)

    def _bytes_per_frame(self) -> int:
        """单帧（含所有声道）字节数。"""
        if self._sample_bytes is None or self._channels is None:
            return 2
        return self._sample_bytes * self._channels
