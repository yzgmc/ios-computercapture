import asyncio
import json
import logging
from typing import Callable, Optional

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp

logger = logging.getLogger(__name__)


class _TrackReceiver:
    """包装远端轨道并持续读取帧，回调给上层处理。"""

    def __init__(self, track, callback):
        self.track = track
        self.callback = callback
        self._task: Optional[asyncio.Task] = None
        self._frame_count = 0

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            try:
                frame = await self.track.recv()
            except Exception as e:
                logger.error("%s receive error: %s", self.track.kind, e)
                break
            try:
                if self.callback:
                    self.callback(frame)
                    self._frame_count += 1
                    if self._frame_count % 60 == 0:
                        logger.info("%s track received %s frames", self.track.kind, self._frame_count)
            except Exception as e:
                logger.error("%s callback error: %s", self.track.kind, e)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class WebRTCPeer:
    def __init__(self):
        self.pc: Optional[RTCPeerConnection] = None
        self.send_signaling: Optional[Callable] = None
        self.on_video_frame: Optional[Callable] = None
        self.on_audio_frame: Optional[Callable] = None
        self.on_connection_state_change: Optional[Callable] = None
        self._video_receiver: Optional[_TrackReceiver] = None
        self._audio_receiver: Optional[_TrackReceiver] = None
        self._data_channel = None
        # 最新一次同步的配置快照，连接建立后会自动推送给 iOS 端
        self._last_settings: Optional[dict] = None
        # iOS 端通过 data channel 上报的运行态信息回调
        self.on_remote_info: Optional[Callable] = None

    async def start(self, send_signaling: Callable):
        self.send_signaling = send_signaling
        self.pc = RTCPeerConnection(
            configuration=RTCConfiguration(
                iceServers=[RTCIceServer(urls="stun:stun.l.google.com:19302")]
            )
        )
        self._data_channel = self.pc.createDataChannel("settings")

        @self._data_channel.on("message")
        def on_data_channel_message(message):
            # 接收 iOS 端通过 data channel 上报的运行态信息
            try:
                payload = json.loads(message) if isinstance(message, str) else json.loads(message.decode("utf-8"))
            except Exception as e:
                logger.debug("Non-JSON data channel message: %s", e)
                return
            logger.debug("Data channel message from iOS: %s", payload)
            if self.on_remote_info:
                try:
                    self.on_remote_info(payload)
                except Exception as e:
                    logger.warning("on_remote_info callback error: %s", e)

        @self._data_channel.on("open")
        def on_data_channel_open():
            logger.info("Data channel opened, pushing initial settings")
            asyncio.create_task(self._push_settings_to_remote(force=True))

        @self.pc.on("track")
        def on_track(track):
            logger.info("Receiving %s track", track.kind)
            if track.kind == "video":
                self._video_receiver = _TrackReceiver(track, self.on_video_frame)
                asyncio.create_task(self._video_receiver.start())
            elif track.kind == "audio":
                self._audio_receiver = _TrackReceiver(track, self.on_audio_frame)
                asyncio.create_task(self._audio_receiver.start())

        @self.pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate and self.send_signaling:
                asyncio.create_task(self.send_signaling({
                    "type": "ice",
                    "candidate": candidate.to_sdp(),
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                }))

        @self.pc.on("icegatheringstatechange")
        async def on_icegatheringstatechange():
            logger.info("ICE gathering state: %s", self.pc.iceGatheringState)

        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            ice_state = self.pc.iceConnectionState
            logger.info("ICE connection state: %s", ice_state)
            # ICE 状态变化往往比 connectionState 更早反映连通性
            if self.on_connection_state_change:
                if ice_state in ("completed", "connected"):
                    self.on_connection_state_change("connected")
                elif ice_state == "checking":
                    self.on_connection_state_change("connecting")
                elif ice_state in ("disconnected", "failed", "closed"):
                    self.on_connection_state_change(ice_state)

        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange():
            state = self.pc.connectionState
            logger.info("Connection state: %s", state)
            if self.on_connection_state_change:
                self.on_connection_state_change(state)

    async def handle_signaling_message(self, message: dict):
        if not self.pc:
            logger.warning("PeerConnection not ready, dropping %s message", message.get("type"))
            return

        msg_type = message.get("type")

        if msg_type == "offer":
            logger.info("Received offer, sdp length=%d", len(message.get("sdp", "")))
            await self.pc.setRemoteDescription(
                RTCSessionDescription(sdp=message["sdp"], type="offer")
            )
            logger.info("Remote description set, creating answer...")
            answer = await self.pc.createAnswer()
            await self.pc.setLocalDescription(answer)
            logger.info("Local description set, ICE gathering: %s", self.pc.iceGatheringState)
            if self.send_signaling:
                await self.send_signaling({
                    "type": "answer",
                    "sdp": self.pc.localDescription.sdp,
                })
            logger.info("Sent answer, sdp length=%d", len(self.pc.localDescription.sdp))
        elif msg_type == "answer":
            logger.info("Received answer, sdp length=%d", len(message.get("sdp", "")))
            await self.pc.setRemoteDescription(
                RTCSessionDescription(sdp=message["sdp"], type="answer")
            )
            logger.info("Remote answer description set")
        elif msg_type == "ice":
            logger.info("Received ICE candidate: %s", message.get("candidate"))
            try:
                candidate = candidate_from_sdp(message["candidate"])
                candidate.sdpMid = message.get("sdpMid")
                candidate.sdpMLineIndex = message.get("sdpMLineIndex")
                await self.pc.addIceCandidate(candidate)
            except Exception as e:
                logger.error("Failed to add ICE candidate: %s", e)

    async def update_settings(self, settings: dict):
        """接收上层（UI）配置变更，缓存最新快照并推送到 iOS 端。"""
        self._last_settings = settings
        await self._push_settings_to_remote(force=False)

    async def push_settings(self, settings: dict):
        """主动推送一次配置（用于外部触发，例如重新连接后）。"""
        self._last_settings = settings
        await self._push_settings_to_remote(force=True)

    async def _push_settings_to_remote(self, force: bool):
        """优先通过 data channel 推送；不可用时回退到信令通道。"""
        if not self._last_settings:
            return
        payload = {
            "type": "settings",
            "settings": self._last_settings,
        }
        # 1) 优先 data channel（低延迟、点对点）
        if self._data_channel and self._data_channel.readyState == "open":
            try:
                self._data_channel.send(json.dumps(payload))
                logger.info("Settings synced via data channel: %s", self._last_settings)
                return
            except Exception as e:
                logger.warning("Data channel send failed, fallback to signaling: %s", e)
        # 2) 信令通道兜底（data channel 尚未打开或已关闭）
        if force and self.send_signaling:
            try:
                await self.send_signaling(payload)
                logger.info("Settings synced via signaling channel: %s", self._last_settings)
            except Exception as e:
                logger.warning("Signaling channel send failed: %s", e)

    async def stop(self):
        if self._video_receiver:
            await self._video_receiver.stop()
            self._video_receiver = None
        if self._audio_receiver:
            await self._audio_receiver.stop()
            self._audio_receiver = None
        if self.pc:
            await self.pc.close()
            self.pc = None
        self._data_channel = None
