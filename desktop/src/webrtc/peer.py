import asyncio
import logging
from typing import Callable, Optional

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from aiortc.contrib.media import MediaPlayer, MediaRelay
from aiortc.mediastreams import MediaStreamTrack

logger = logging.getLogger(__name__)


class VideoReceiver(MediaStreamTrack):
    kind = "video"

    def __init__(self, track, callback):
        super().__init__()
        self.track = track
        self.callback = callback
        self._task = None

    async def start_receiving(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            try:
                frame = await self.track.recv()
                if self.callback:
                    self.callback(frame)
            except Exception as e:
                logger.error("Video receive error: %s", e)
                break

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await super().stop()


class AudioReceiver(MediaStreamTrack):
    kind = "audio"

    def __init__(self, track, callback):
        super().__init__()
        self.track = track
        self.callback = callback
        self._task = None

    async def start_receiving(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            try:
                frame = await self.track.recv()
                if self.callback:
                    self.callback(frame)
            except Exception as e:
                logger.error("Audio receive error: %s", e)
                break

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await super().stop()


class WebRTCPeer:
    def __init__(self):
        self.pc: Optional[RTCPeerConnection] = None
        self.send_signaling: Optional[Callable] = None
        self.on_video_frame: Optional[Callable] = None
        self.on_audio_frame: Optional[Callable] = None
        self.on_connection_state_change: Optional[Callable] = None
        self._video_receiver: Optional[VideoReceiver] = None
        self._audio_receiver: Optional[AudioReceiver] = None
        self._data_channel = None

    async def start(self, send_signaling: Callable):
        self.send_signaling = send_signaling
        self.pc = RTCPeerConnection()
        self._data_channel = self.pc.createDataChannel("settings")

        @self.pc.on("track")
        def on_track(track):
            logger.info("Receiving %s track", track.kind)
            if track.kind == "video":
                self._video_receiver = VideoReceiver(track, self.on_video_frame)
                asyncio.create_task(self._video_receiver.start_receiving())
            elif track.kind == "audio":
                self._audio_receiver = AudioReceiver(track, self.on_audio_frame)
                asyncio.create_task(self._audio_receiver.start_receiving())

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
            return

        msg_type = message.get("type")

        if msg_type == "offer":
            await self.pc.setRemoteDescription(
                RTCSessionDescription(sdp=message["sdp"], type="offer")
            )
            answer = await self.pc.createAnswer()
            await self.pc.setLocalDescription(answer)
            await self.send_signaling({
                "type": "answer",
                "sdp": self.pc.localDescription.sdp,
            })
        elif msg_type == "answer":
            await self.pc.setRemoteDescription(
                RTCSessionDescription(sdp=message["sdp"], type="answer")
            )
        elif msg_type == "ice":
            candidate = candidate_from_sdp(message["candidate"])
            candidate.sdpMid = message.get("sdpMid")
            candidate.sdpMLineIndex = message.get("sdpMLineIndex")
            await self.pc.addIceCandidate(candidate)

    async def update_settings(self, settings: dict):
        # 通过 data channel 发送设置到 iOS 端
        if self.pc and self.pc.datachannel:
            self.pc.datachannel.send(settings)

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
