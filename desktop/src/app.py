import asyncio
import logging
import socket

from PyQt6.QtCore import QObject, pyqtSignal
from qasync import asyncSlot

from ui.main_window import MainWindow
from webrtc.peer import WebRTCPeer
from signaling import EmbeddedSignalingServer, SignalingClient
from capture.virtual_device import (
    VirtualCameraOutput, VirtualAudioOutput, find_default_virtual_audio_device
)
from discovery.service import DiscoveryService

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8080
DEFAULT_ROOM = "room1"


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

        # 内嵌信令服务器，随桌面客户端启动
        self.signaling_server = EmbeddedSignalingServer(host="0.0.0.0", port=DEFAULT_PORT)
        # 信令客户端，连到自己内嵌的服务器
        self.signaling = SignalingClient()
        self.peer = WebRTCPeer()
        self.discovery = DiscoveryService(port=DEFAULT_PORT)

        self.virtual_camera = VirtualCameraOutput()
        self.virtual_audio = VirtualAudioOutput(
            device_index=find_default_virtual_audio_device()
        )

        self._pending_messages: list[dict] | None = []
        self._auto_started = False

        self._setup_peer_signals()

    def _setup_peer_signals(self):
        self.peer.on_video_frame = self._on_video_frame
        self.peer.on_audio_frame = self._on_audio_frame
        self.peer.on_connection_state_change = self._on_connection_state_change

    def _on_video_frame(self, frame):
        self.video_frame_received.emit(frame)
        self.virtual_camera.send_frame(frame)
        logger.debug("Video frame forwarded: %dx%d", frame.width, frame.height)

    def _on_audio_frame(self, frame):
        self.audio_frame_received.emit(frame)
        self.virtual_audio.send_frame(frame)

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
        """应用启动时自动拉起信令服务器、mDNS 发现并进入等待连接状态。"""
        # 1. 启动内嵌信令服务器
        try:
            await self.signaling_server.start()
        except Exception as e:
            logger.error("Failed to start embedded signaling server: %s", e)
            self.status_changed.emit(f"信令服务器启动失败: {e}")
            return

        local_ip = self._get_local_ip()
        ws_url = f"ws://{local_ip}:{DEFAULT_PORT}"
        self.window.set_server_address(ws_url)
        self.status_changed.emit(f"信令服务器已启动: {ws_url}")
        logger.info("Local signaling URL: %s", ws_url)

        # 2. 启动 mDNS 发现
        try:
            self.discovery.start()
        except Exception as e:
            logger.warning("Failed to start discovery service: %s", e)

        # 3. 自动建立 PeerConnection 并连入本地信令房间，等待 iOS offer
        await self._auto_connect_local(ws_url, DEFAULT_ROOM)

    async def _auto_connect_local(self, ws_url: str, room_id: str):
        """自动连接本地信令服务器并就绪 PeerConnection。"""
        try:
            await self.peer.start(self.signaling.send)
            self.signaling.on_message = self._on_signaling_message
            await self.signaling.connect(ws_url, room_id)
            self._auto_started = True
            self.window.set_connect_state(connected=False, auto_mode=True)
            self.status_changed.emit("等待 iPhone 连接...")
            await self._flush_pending_messages()
        except Exception as e:
            logger.error("Auto connect failed: %s", e)
            self.status_changed.emit(f"自动连接失败: {e}")

    def show(self):
        self.window.show()

    @asyncSlot(str, str)
    async def _on_connect_requested(self, signaling_url: str, room_id: str):
        """手动连接（可指向远程信令服务器或重连本地）。"""
        logger.info("Connecting to %s / room %s", signaling_url, room_id)
        self.status_changed.emit("正在连接信令服务器...")
        self._pending_messages = []
        try:
            await self.peer.start(self.signaling.send)
            self.signaling.on_message = self._on_signaling_message
            await self.signaling.connect(signaling_url, room_id)
            self.status_changed.emit("正在建立 WebRTC 连接...")
            await self._flush_pending_messages()
        except Exception as e:
            logger.error("Connection failed: %s", e)
            self.status_changed.emit(f"连接失败: {e}")
            if self._pending_messages is not None:
                self._pending_messages.clear()

    @asyncSlot()
    async def _on_disconnect_requested(self):
        logger.info("Disconnecting")
        await self.peer.stop()
        await self.signaling.disconnect()
        self.status_changed.emit("已断开连接")
        self.window.set_connect_state(connected=False, auto_mode=self._auto_started)

    @asyncSlot(dict)
    async def _on_settings_changed(self, settings: dict):
        logger.info("Settings changed: %s", settings)
        width = settings.get("width", 1280)
        height = settings.get("height", 720)
        fps = settings.get("fps", 30)
        self.virtual_camera.update_format(width, height, fps)
        self.virtual_camera.update_flip(
            settings.get("flip_horizontal", False),
            settings.get("flip_vertical", False),
        )
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
        if self._pending_messages is not None:
            self._pending_messages.append(message)
        else:
            asyncio.create_task(self.peer.handle_signaling_message(message))

    async def _flush_pending_messages(self):
        pending = self._pending_messages
        self._pending_messages = None
        if pending:
            for message in pending:
                await self.peer.handle_signaling_message(message)

    def _on_connection_state_change(self, state: str):
        if state == "connected":
            self.status_changed.emit("已连接，正在接收视频")
            self.window.set_connect_state(connected=True, auto_mode=self._auto_started)
            # 连接建立后主动推送一次当前配置，确保 iOS 端拿到最新参数
            last = self._collect_current_settings()
            if last:
                asyncio.create_task(self.peer.push_settings(last))
        elif state in ("disconnected", "failed", "closed"):
            self.status_changed.emit(f"连接{state}")
            self.window.set_connect_state(connected=False, auto_mode=self._auto_started)
        else:
            self.status_changed.emit(f"连接状态: {state}")

    def _collect_current_settings(self) -> dict | None:
        """从 UI 当前控件状态收集配置快照。"""
        try:
            return self.window.collect_settings()
        except Exception as e:
            logger.warning("Collect settings failed: %s", e)
            return None

    async def shutdown(self):
        """应用退出时清理所有资源。"""
        try:
            await self.peer.stop()
        except Exception as e:
            logger.warning("Peer stop error: %s", e)
        try:
            await self.signaling.disconnect()
        except Exception as e:
            logger.warning("Signaling disconnect error: %s", e)
        try:
            await self.signaling_server.stop()
        except Exception as e:
            logger.warning("Signaling server stop error: %s", e)
        try:
            self.discovery.stop()
        except Exception as e:
            logger.warning("Discovery stop error: %s", e)
        try:
            self.virtual_camera.disable()
            self.virtual_audio.disable()
        except Exception as e:
            logger.warning("Virtual device disable error: %s", e)
