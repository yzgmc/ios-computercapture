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
        self.flip_horizontal = False
        self.flip_vertical = False
        self.enabled = False
        self._cam = None
        self._queue: queue.Queue = queue.Queue(maxsize=2)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def enable(self):
        try:
            import pyvirtualcam
            self._cam = pyvirtualcam.Camera(width=self.width,
                                            height=self.height,
                                            fps=self.fps)
            self.enabled = True
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
            logger.info("Virtual camera started: %s", self._cam.device)
        except Exception as e:
            logger.error("Failed to start virtual camera: %s", e)
            self.enabled = False

    def disable(self):
        self.enabled = False
        # 清空队列，唤醒工作线程
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._cam:
            self._cam.close()
            self._cam = None
            logger.info("Virtual camera stopped")

    def _worker(self):
        import cv2
        while self.enabled:
            try:
                img = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    flip_h = self.flip_horizontal
                    flip_v = self.flip_vertical
                    target_w = self.width
                    target_h = self.height
                if flip_h and flip_v:
                    img = cv2.flip(img, -1)
                elif flip_h:
                    img = cv2.flip(img, 1)
                elif flip_v:
                    img = cv2.flip(img, 0)
                if img.shape[0] != target_h or img.shape[1] != target_w:
                    img = cv2.resize(img, (target_w, target_h))
                self._cam.send(img)
                self._cam.sleep_until_next_frame()
            except Exception as e:
                logger.error("Virtual camera worker error: %s", e)

    def send_ndarray(self, img: np.ndarray):
        """将 RGB ndarray 帧送入虚拟摄像头队列。调用方负责格式转换。"""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(img)
        except queue.Full:
            # 丢弃最旧帧，保持实时性
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(img)
            except queue.Empty:
                pass

    def update_format(self, width: int, height: int, fps: int):
        need_restart = self.enabled and (width != self.width or height != self.height or fps != self.fps)
        self.width = width
        self.height = height
        self.fps = fps
        if need_restart:
            self.disable()
            self.enable()

    def update_flip(self, horizontal: bool, vertical: bool):
        with self._lock:
            self.flip_horizontal = horizontal
            self.flip_vertical = vertical


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
