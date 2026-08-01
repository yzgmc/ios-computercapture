import asyncio
import logging

from PyQt6.QtCore import QObject, pyqtSignal
from qasync import asyncSlot

from ui.main_window import MainWindow
from webrtc.peer import WebRTCPeer
from webrtc.signaling import SignalingClient
from capture.virtual_device import (
    VirtualCameraOutput, VirtualAudioOutput, find_default_virtual_audio_device
)
from discovery.service import DiscoveryService

logger = logging.getLogger(__name__)


class PhoneCamApp(QObject):
    status_changed = pyqtSignal(str)
    video_frame_received = pyqtSignal(object)
    audio_frame_received = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.window = MainWindow()
        self.window.connect_requested.connect(self._on_connect_requested)
        self.window.disconnect_requested.connect(self._on_disconnect_requested)
        self.window.settings_changed.connect(self._on_settings_changed)
        self.window.virtual_camera_toggled.connect(self._on_virtual_camera_toggled)
        self.window.virtual_audio_toggled.connect(self._on_virtual_audio_toggled)

        self.status_changed.connect(self.window.set_status)
        self.video_frame_received.connect(self.window.update_video_frame)

        self.signaling = SignalingClient()
        self.peer = WebRTCPeer()
        self.discovery = DiscoveryService(port=8080)

        self.virtual_camera = VirtualCameraOutput()
        self.virtual_audio = VirtualAudioOutput(
            device_index=find_default_virtual_audio_device()
        )

        self._setup_peer_signals()
        self._start_discovery()

    def _setup_peer_signals(self):
        self.peer.on_video_frame = self._on_video_frame
        self.peer.on_audio_frame = self._on_audio_frame
        self.peer.on_connection_state_change = self._on_connection_state_change

    def _on_video_frame(self, frame):
        self.video_frame_received.emit(frame)
        self.virtual_camera.send_frame(frame)

    def _on_audio_frame(self, frame):
        self.audio_frame_received.emit(frame)
        self.virtual_audio.send_frame(frame)

    def _start_discovery(self):
        try:
            self.discovery.start()
            self.status_changed.emit("设备发现服务已启动")
        except Exception as e:
            logger.warning("Failed to start discovery service: %s", e)

    def show(self):
        self.window.show()

    @asyncSlot(str, str)
    async def _on_connect_requested(self, signaling_url: str, room_id: str):
        logger.info("Connecting to %s / room %s", signaling_url, room_id)
        self.status_changed.emit("正在连接信令服务器...")
        try:
            await self.signaling.connect(signaling_url, room_id)
            self.signaling.on_message = self._on_signaling_message
            self.status_changed.emit("正在建立 WebRTC 连接...")
            await self.peer.start(self.signaling.send)
        except Exception as e:
            logger.error("Connection failed: %s", e)
            self.status_changed.emit(f"连接失败: {e}")

    @asyncSlot()
    async def _on_disconnect_requested(self):
        logger.info("Disconnecting")
        await self.peer.stop()
        await self.signaling.disconnect()
        self.status_changed.emit("已断开连接")

    @asyncSlot(dict)
    async def _on_settings_changed(self, settings: dict):
        logger.info("Settings changed: %s", settings)
        width = settings.get("width", 1280)
        height = settings.get("height", 720)
        fps = settings.get("fps", 30)
        self.virtual_camera.update_format(width, height, fps)
        await self.peer.update_settings(settings)

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
            self.virtual_audio.enable()
            self.status_changed.emit("虚拟麦克风已启动")
        else:
            self.virtual_audio.disable()
            self.status_changed.emit("虚拟麦克风已停止")

    def _on_signaling_message(self, message: dict):
        asyncio.create_task(self.peer.handle_signaling_message(message))

    def _on_connection_state_change(self, state: str):
        self.status_changed.emit(f"连接状态: {state}")
