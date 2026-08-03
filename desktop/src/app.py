import asyncio
import logging
import socket

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from qasync import asyncSlot

from ui.main_window import MainWindow
from capture.virtual_device import (
    VirtualCameraOutput, find_default_virtual_audio_device
)
from raw_stream import RawStreamReceiver, PixelFormat
from audio_stream import AudioStreamReceiver, AudioPlayer

logger = logging.getLogger(__name__)

RAW_STREAM_PORT = 5000   # TCP 视频原画质
AUDIO_STREAM_PORT = 5001  # UDP 音频


class PhoneCamApp(QObject):
    status_changed = pyqtSignal(str)
    raw_frame_received = pyqtSignal(bytes, int, int, int, int)

    def __init__(self):
        super().__init__()
        self.window = MainWindow()
        self.window.virtual_camera_toggled.connect(self._on_virtual_camera_toggled)
        self.window.virtual_audio_toggled.connect(self._on_virtual_audio_toggled)
        self.window.flip_changed.connect(self._on_flip_changed)
        self.window.volume_changed.connect(self._on_volume_changed)

        self.status_changed.connect(self.window.set_status)

        self.virtual_camera = VirtualCameraOutput()

        # UDP 音频播放器：默认输出到虚拟音频设备（VB-Cable），
        # 用户在 UI 中切换"启动虚拟麦克风"时 start/stop。
        self.audio_player = AudioPlayer(
            output_device_index=find_default_virtual_audio_device()
        )

        # 原画质 TCP 视频接收器（监听 0.0.0.0:5000，等待 iOS 推流）
        self.raw_receiver = RawStreamReceiver(
            host="0.0.0.0", port=RAW_STREAM_PORT, on_frame=self._on_raw_frame
        )

        # UDP 音频接收器（监听 0.0.0.0:5001）
        self.audio_receiver = AudioStreamReceiver(
            host="0.0.0.0", port=AUDIO_STREAM_PORT,
            on_packet=self._on_audio_packet,
        )

        self.raw_frame_received.connect(self._display_raw_frame)

    def _on_raw_frame(self, raw: bytes, width: int, height: int,
                      pixel_format: int, bytes_per_row: int):
        """原画质帧到达（asyncio 线程），通过信号转发到主线程显示。"""
        self.raw_frame_received.emit(raw, width, height, pixel_format, bytes_per_row)

    def _display_raw_frame(self, raw: bytes, width: int, height: int,
                           pixel_format: int, bytes_per_row: int):
        """在主线程将 BGRA 原始帧渲染到预览控件。"""
        try:
            import cv2
            import numpy as np
            if pixel_format != PixelFormat.BGRA:
                logger.warning("Unsupported raw pixel format: %d", pixel_format)
                return
            expected = bytes_per_row * height
            if len(raw) < expected:
                logger.warning("Raw payload truncated: got %d, expected %d (w=%d h=%d bpr=%d)",
                               len(raw), expected, width, height, bytes_per_row)
                return
            if not getattr(self, "_raw_first_frame_logged", False):
                logger.info("Raw stream first frame: %dx%d bpr=%d payload=%d",
                            width, height, bytes_per_row, len(raw))
                self._raw_first_frame_logged = True
                # 首帧到达时同步 UI 上的分辨率显示
                self.window.set_actual_resolution(width, height)
                # 把实际分辨率同步到虚拟摄像头（仅在未启用时生效；
                # 已启用时维持原格式，由 worker 缩放）
                if not self.virtual_camera.enabled:
                    self.virtual_camera.width = width
                    self.virtual_camera.height = height
            arr = np.frombuffer(raw[:expected], dtype=np.uint8)
            stride_bytes = max(bytes_per_row, width * 4)
            arr = arr.reshape(height, stride_bytes)[:, :width * 4].reshape(height, width, 4)
            # BGRA -> BGR (供 OpenCV/Qt 使用)
            bgr = arr[:, :, :3]
            rgb = bgr[:, :, ::-1]
            # 应用翻转（与只读控件状态一致）
            if self.window.flip_horizontal_checkbox.isChecked() and self.window.flip_vertical_checkbox.isChecked():
                rgb = cv2.flip(rgb, -1)
            elif self.window.flip_horizontal_checkbox.isChecked():
                rgb = cv2.flip(rgb, 1)
            elif self.window.flip_vertical_checkbox.isChecked():
                rgb = cv2.flip(rgb, 0)
            rgb = np.ascontiguousarray(rgb)
            # 同步到虚拟摄像头
            if self.virtual_camera.enabled:
                self.virtual_camera.send_ndarray(rgb)
            h, w, _ = rgb.shape
            qt_image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(qt_image)
            scaled = pixmap.scaled(
                self.window.video_label.size(),
                Qt.AlignmentFlag.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.window.video_label.setPixmap(scaled)
        except Exception as e:
            logger.error("Display raw frame error: %s", e)

    def _on_audio_packet(self, pcm: bytes, sample_rate: int, channels: int,
                         audio_format: int, seq: int):
        """UDP 音频包到达，转交 AudioPlayer 播放。在 asyncio 线程中调用。

        AudioPlayer.feed 是线程安全的（内部有锁），可直接调用。
        """
        self.audio_player.feed(pcm, sample_rate, channels, audio_format)

    def _get_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0)
            try:
                s.connect(("10.254.254.254", 1))
                ip = s.getsockname()[0]
            except Exception:
                ip = "127.0.0.1"
            finally:
                s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    async def start(self):
        """应用启动：监听 TCP 5000 视频 + UDP 5001 音频，等待 iOS 连接。"""
        local_ip = self._get_local_ip()
        self.window.set_server_address(f"TCP :{RAW_STREAM_PORT} / UDP :{AUDIO_STREAM_PORT}")

        # 1. 启动原画质 TCP 接收器
        try:
            await self.raw_receiver.start()
            self.status_changed.emit(f"视频监听 :{RAW_STREAM_PORT} (TCP) · 等待 iPhone 连接 {local_ip}")
        except Exception as e:
            logger.error("Failed to start raw stream receiver: %s", e)
            self.status_changed.emit(f"视频端口 {RAW_STREAM_PORT} 启动失败: {e}")

        # 2. 启动 UDP 音频接收器
        try:
            await self.audio_receiver.start()
            self.status_changed.emit(f"音频监听 :{AUDIO_STREAM_PORT} (UDP)")
        except Exception as e:
            logger.error("Failed to start audio stream receiver: %s", e)
            self.status_changed.emit(f"音频端口 {AUDIO_STREAM_PORT} 启动失败: {e}")

    def show(self):
        self.window.show()

    @asyncSlot(bool)
    async def _on_virtual_camera_toggled(self, enabled: bool):
        if enabled:
            self.virtual_camera.enable()
            self.status_changed.emit("虚拟摄像头已启动")
        else:
            self.virtual_camera.disable()
            self.status_changed.emit("虚拟摄像头已停止")

    @asyncSlot(bool)
    async def _on_virtual_audio_toggled(self, enabled: bool):
        if enabled:
            self.audio_player.start()
            self.status_changed.emit("虚拟麦克风已启动")
        else:
            self.audio_player.stop()
            self.status_changed.emit("虚拟麦克风已停止")

    @asyncSlot(bool, bool)
    async def _on_flip_changed(self, flip_h: bool, flip_v: bool):
        self.virtual_camera.update_flip(flip_h, flip_v)

    @asyncSlot(float)
    async def _on_volume_changed(self, volume: float):
        self.audio_player.set_volume(volume)

    async def shutdown(self):
        """应用退出时清理所有资源。"""
        try:
            await self.raw_receiver.stop()
        except Exception as e:
            logger.warning("Raw receiver stop error: %s", e)
        try:
            await self.audio_receiver.stop()
        except Exception as e:
            logger.warning("Audio receiver stop error: %s", e)
        try:
            self.audio_player.stop()
        except Exception as e:
            logger.warning("Audio player stop error: %s", e)
        try:
            self.virtual_camera.disable()
        except Exception as e:
            logger.warning("Virtual camera disable error: %s", e)
