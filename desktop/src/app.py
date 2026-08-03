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
from discovery import DiscoveryService
from usb import (
    UsbBridgeManager, is_usb_available, list_ios_devices,
    USB_DEFAULT_TCP_PORT, USB_DEFAULT_UDP_PORT,
)

logger = logging.getLogger(__name__)

RAW_STREAM_PORT = 5000   # TCP 视频原画质
AUDIO_STREAM_PORT = 5001  # UDP 音频

# 传输模式
MODE_LAN = "lan"          # 通过 Wi-Fi / 局域网 TCP/UDP
MODE_USB = "usb"          # 通过 USB + usbmuxd 桥接


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
        self.window.usb_mode_requested.connect(self.enable_usb_mode)
        self.window.lan_mode_requested.connect(self.enable_lan_mode)

        self.status_changed.connect(self.window.set_status)

        self.virtual_camera = VirtualCameraOutput()
        self.discovery = None  # start() 中创建

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

        # USB 直连管理器（pymobiledevice3 + usbmuxd）
        self.usb_manager: UsbBridgeManager | None = None
        self.usb_devices: list[dict] = []
        self._mode = MODE_LAN  # 当前传输模式
        # 接收器 listen 在 127.0.0.1 时只接受 USB 桥接过来的连接，
        # 在 0.0.0.0 时同时接受 LAN / USB。默认 LAN 全接受。
        self._listen_host = "0.0.0.0"

    @property
    def mode(self) -> str:
        return self._mode

    def _emit_state(self, level: str, msg: str):
        prefix = {"info": "[USB] ", "warn": "[USB] ⚠ ", "error": "[USB] ✗ "}.get(level, "[USB] ")
        self.status_changed.emit(prefix + msg)
        logger.log({"info": logging.INFO, "warn": logging.WARNING,
                    "error": logging.ERROR, "debug": logging.DEBUG}.get(level, logging.INFO),
                   "USB: %s", msg)

    def _emit_devices(self):
        if hasattr(self.window, "set_usb_devices"):
            try:
                self.window.set_usb_devices(self.usb_devices, self._mode)
            except Exception as e:
                logger.debug("set_usb_devices error: %s", e)

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
            # 必须 copy()，否则 QImage 持有的指针在 rgb 被回收后悬空
            qt_image = QImage(rgb.copy(), w, h, 3 * w, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(qt_image)
            scaled = pixmap.scaled(
                self.window.video_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
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

    @asyncSlot()
    async def enable_usb_mode(self):
        """用户点击"切换到 USB 模式"：建桥接，等待 iPhone USB 接入。"""
        if self._mode == MODE_USB:
            return
        if not is_usb_available():
            self.status_changed.emit("USB 直连不可用：未安装 pymobiledevice3")
            return
        self._mode = MODE_USB
        self._emit_state("info", "切换到 USB 直连模式")
        if self.usb_manager is None:
            self.usb_manager = UsbBridgeManager(
                tcp_port=RAW_STREAM_PORT,
                udp_port=AUDIO_STREAM_PORT,
                on_state=lambda lvl, msg: self._emit_state(lvl, msg),
                on_devices_changed=self._on_usb_devices_changed,
            )
        # 启动时自动选第一台设备
        await self.usb_manager.start()
        # 立即尝试获取已接入设备
        devs = await list_ios_devices()
        if devs:
            await self.usb_manager._ensure_bridges(devs[0]["udid"])
        # 关闭 LAN 监听（接收器只接受 127.0.0.1 桥接）
        await self._restart_receivers(listen_host="127.0.0.1")
        self._emit_state("info", "等待 iPhone 通过 USB 连接...")
        self._emit_devices()

    @asyncSlot()
    async def enable_lan_mode(self):
        """用户切换回 LAN 模式：恢复 0.0.0.0 监听 + 关闭 USB 桥接。"""
        if self._mode == MODE_LAN:
            return
        self._mode = MODE_LAN
        self._emit_state("info", "切换到局域网模式")
        if self.usb_manager:
            await self.usb_manager.stop()
        await self._restart_receivers(listen_host="0.0.0.0")
        self._emit_devices()

    async def _restart_receivers(self, listen_host: str):
        """重启接收器，绑定到 listen_host。"""
        try:
            await self.raw_receiver.stop()
        except Exception:
            pass
        try:
            await self.audio_receiver.stop()
        except Exception:
            pass
        self.raw_receiver = RawStreamReceiver(
            host=listen_host, port=RAW_STREAM_PORT, on_frame=self._on_raw_frame
        )
        self.audio_receiver = AudioStreamReceiver(
            host=listen_host, port=AUDIO_STREAM_PORT, on_packet=self._on_audio_packet
        )
        try:
            await self.raw_receiver.start()
        except Exception as e:
            self._emit_state("error", f"重启 TCP 接收器失败: {e}")
        try:
            await self.audio_receiver.start()
        except Exception as e:
            self._emit_state("error", f"重启 UDP 接收器失败: {e}")
        self._listen_host = listen_host

    async def _on_usb_devices_changed(self, devices: list):
        self.usb_devices = devices
        self._emit_devices()
        for d in devices:
            if d.get("bridges") and "tcp" in d["bridges"]:
                self._emit_state("info", f"设备就绪: {d['udid'][:8]}...")
            else:
                self._emit_state("info", f"设备断开: {d['udid'][:8]}...")

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
        if self.usb_manager:
            try:
                await self.usb_manager.stop()
            except Exception as e:
                logger.warning("USB manager stop error: %s", e)
        try:
            await self.raw_receiver.stop()
        except Exception as e:
            logger.warning("Raw receiver stop error: %s", e)
        try:
            await self.audio_receiver.stop()
        except Exception as e:
            logger.warning("Audio receiver stop error: %s", e)
        if self.discovery:
            try:
                await self.discovery.stop()
            except Exception as e:
                logger.warning("Discovery stop error: %s", e)
        try:
            self.audio_player.stop()
        except Exception as e:
            logger.warning("Audio player stop error: %s", e)
        try:
            self.virtual_camera.disable()
        except Exception as e:
            logger.warning("Virtual camera disable error: %s", e)
