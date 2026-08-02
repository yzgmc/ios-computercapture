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

    async def start(self, send_signaling: Callable):
        self.send_signaling = send_signaling
        self.pc = RTCPeerConnection(
            configuration=RTCConfiguration(
                iceServers=[RTCIceServer(urls="stun:stun.l.google.com:19302")]
            )
        )
        self._data_channel = self.pc.createDataChannel("settings")

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
                await self.send_signaling({
                    "type": "ice",
                    "candidate": candidate.to_sdp(),
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                })

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
            logger.info("Received offer")
            await self.pc.setRemoteDescription(
                RTCSessionDescription(sdp=message["sdp"], type="offer")
            )
            answer = await self.pc.createAnswer()
            await self.pc.setLocalDescription(answer)
            await self.send_signaling({
                "type": "answer",
                "sdp": self.pc.localDescription.sdp,
            })
            logger.info("Sent answer")
        elif msg_type == "answer":
            logger.info("Received answer")
            await self.pc.setRemoteDescription(
                RTCSessionDescription(sdp=message["sdp"], type="answer")
            )
        elif msg_type == "ice":
            logger.debug("Received ICE candidate")
            candidate = candidate_from_sdp(message["candidate"])
            candidate.sdpMid = message.get("sdpMid")
            candidate.sdpMLineIndex = message.get("sdpMLineIndex")
            await self.pc.addIceCandidate(candidate)

    async def update_settings(self, settings: dict):
        # 通过 data channel 发送设置到 iOS 端
        if self._data_channel and self._data_channel.readyState == "open":
            try:
                self._data_channel.send(json.dumps(settings))
            except Exception as e:
                logger.warning("Failed to send settings: %s", e)
        else:
            logger.debug("Data channel not open, skipping settings update")

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
