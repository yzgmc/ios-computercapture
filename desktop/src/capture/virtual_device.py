import logging
import queue
import threading
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class VirtualCameraOutput:
    """通过 pyvirtualcam 将接收到的视频帧输出为虚拟摄像头。

    支持 Windows (OBS Virtual Camera) 和 macOS (OBS Virtual Camera / UnityCapture)。
    使用前请确保已安装 OBS 并启用 Virtual Camera 插件。
    """

    def __init__(self, width: int = 1280, height: int = 720, fps: int = 30):
        self.width = width
        self.height = height
        self.fps = fps
        self.enabled = False
        self._cam = None

    def enable(self):
        try:
            import pyvirtualcam
            self._cam = pyvirtualcam.Camera(width=self.width,
                                            height=self.height,
                                            fps=self.fps)
            self.enabled = True
            logger.info("Virtual camera started: %s", self._cam.device)
        except Exception as e:
            logger.error("Failed to start virtual camera: %s", e)
            self.enabled = False

    def disable(self):
        self.enabled = False
        if self._cam:
            self._cam.close()
            self._cam = None
            logger.info("Virtual camera stopped")

    def send_frame(self, frame):
        if not self.enabled or self._cam is None:
            return
        try:
            # aiortc VideoFrame 转换为 RGB numpy 数组
            img = frame.to_ndarray(format="rgb24")
            # 缩放到虚拟摄像头目标尺寸
            if img.shape[0] != self.height or img.shape[1] != self.width:
                import cv2
                img = cv2.resize(img, (self.width, self.height))
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            self._cam.send(img)
            self._cam.sleep_until_next_frame()
        except Exception as e:
            logger.error("Virtual camera send frame error: %s", e)

    def update_format(self, width: int, height: int, fps: int):
        need_restart = self.enabled and (width != self.width or height != self.height or fps != self.fps)
        self.width = width
        self.height = height
        self.fps = fps
        if need_restart:
            self.disable()
            self.enable()


class VirtualAudioOutput:
    """将接收到的音频帧通过 PyAudio 输出到虚拟音频设备。

    用户需预先安装：
    - Windows: VB-Cable (https://vb-audio.com/Cable/)
    - macOS: BlackHole (https://existential.audio/blackhole/)
    """

    def __init__(self, device_index: Optional[int] = None,
                 sample_rate: int = 48000, channels: int = 2):
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.channels = channels
        self.enabled = False
        self._stream = None
        self._pa = None
        self._frame_queue = queue.Queue(maxsize=10)
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def enable(self):
        try:
            import pyaudio
            self._pa = pyaudio.PyAudio()
            self._running = True
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                output_device_index=self.device_index,
                frames_per_buffer=960,
                stream_callback=self._audio_callback,
            )
            self.enabled = True
            logger.info("Virtual audio output enabled on device %s", self.device_index)
        except Exception as e:
            logger.error("Failed to enable virtual audio: %s", e)
            self.enabled = False

    def disable(self):
        self.enabled = False
        self._running = False
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa:
            self._pa.terminate()
            self._pa = None
        # 清空队列
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

    def _audio_callback(self, in_data, frame_count, time_info, status):
        try:
            data = self._frame_queue.get_nowait()
            return (data, pyaudio.paContinue)
        except queue.Empty:
            return (b'\x00' * frame_count * self.channels * 2, pyaudio.paContinue)

    def send_frame(self, frame):
        if not self.enabled:
            return
        try:
            # aiortc AudioFrame 转换为 16bit PCM
            # to_ndarray 返回 shape (channels, samples) 的 float32 planar 数据
            audio_array = frame.to_ndarray()

            # 调整音量
            audio_array = np.clip(audio_array * 32767, -32768, 32767).astype(np.int16)

            # 单/双声道适配
            if audio_array.shape[0] == 1 and self.channels == 2:
                audio_array = np.repeat(audio_array, 2, axis=0)
            elif audio_array.shape[0] == 2 and self.channels == 1:
                audio_array = audio_array.mean(axis=0, keepdims=True).astype(np.int16)
            elif audio_array.shape[0] != self.channels:
                # 其他情况，取前 N 个声道或复制第一个声道
                if audio_array.shape[0] > self.channels:
                    audio_array = audio_array[:self.channels]
                else:
                    audio_array = np.repeat(audio_array[self.channels - 1:self.channels],
                                            self.channels - audio_array.shape[0] + 1, axis=0)

            # planar (channels, samples) -> interleaved (samples, channels)
            audio_array = audio_array.T
            pcm_data = audio_array.tobytes()
            self._frame_queue.put_nowait(pcm_data)
        except Exception as e:
            logger.error("Virtual audio send frame error: %s", e)


def list_audio_devices():
    """列出所有音频输出设备。"""
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        devices = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxOutputChannels", 0) > 0:
                devices.append((i, info["name"]))
        pa.terminate()
        return devices
    except Exception as e:
        logger.error("Failed to list audio devices: %s", e)
        return []


def list_virtual_audio_devices():
    """列出可能的虚拟音频设备（VB-Cable / BlackHole）。"""
    devices = list_audio_devices()
    return [(idx, name) for idx, name in devices
            if any(keyword in name.lower() for keyword in ["cable", "blackhole", "virtual", "vb-audio"])]


def find_default_virtual_audio_device() -> Optional[int]:
    """查找默认虚拟音频设备索引。"""
    virtual_devices = list_virtual_audio_devices()
    return virtual_devices[0][0] if virtual_devices else None
