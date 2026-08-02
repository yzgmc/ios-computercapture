"""桌面端视频管线集成测试：验证 WebRTCPeer 能正确处理 offer 并接收视频帧。

运行方式:
1. 先安装桌面端依赖: pip install -r desktop/requirements.txt
2. 运行测试: python tests/test_desktop_pipeline.py
"""
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.mediastreams import VideoFrame
from aiortc.sdp import candidate_from_sdp
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "desktop", "src"))
from webrtc.peer import WebRTCPeer
from signaling import SignalingClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

rooms = {}


async def websocket_handler(request):
    room_id = request.match_info["room_id"]
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    rooms.setdefault(room_id, set()).add(ws)
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                for peer in rooms[room_id]:
                    if peer is not ws and not peer.closed:
                        await peer.send_str(msg.data)
    finally:
        rooms[room_id].discard(ws)
    return ws


@asynccontextmanager
async def run_signaling_server(port: int = 18080):
    app = web.Application()
    app.router.add_get("/ws/{room_id}", websocket_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info("Signaling server started on port %d", port)
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


class DummyVideoTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        img[:, :, 0] = 255
        frame = VideoFrame.from_ndarray(img, format="rgb24")
        frame.pts = pts
        frame.time_base = time_base
        return frame


async def test():
    received_frames = []

    async with run_signaling_server() as url:
        desktop = WebRTCPeer()
        desktop.on_video_frame = lambda frame: received_frames.append(frame)

        desktop_signaling = SignalingClient()
        await desktop.start(desktop_signaling.send)
        desktop_signaling.on_message = lambda msg: asyncio.create_task(desktop.handle_signaling_message(msg))
        await desktop_signaling.connect(url, "test_room")

        ios_signaling = SignalingClient()
        ios = RTCPeerConnection()
        ios.addTrack(DummyVideoTrack())

        async def handle_ios_message(msg: dict):
            if msg["type"] == "answer":
                await ios.setRemoteDescription(RTCSessionDescription(sdp=msg["sdp"], type="answer"))
            elif msg["type"] == "ice":
                candidate = candidate_from_sdp(msg["candidate"])
                candidate.sdpMid = msg.get("sdpMid")
                candidate.sdpMLineIndex = msg.get("sdpMLineIndex")
                await ios.addIceCandidate(candidate)

        ios_signaling.on_message = lambda msg: asyncio.create_task(handle_ios_message(msg))
        await ios_signaling.connect(url, "test_room")

        @ios.on("icecandidate")
        async def on_ios_ice(candidate):
            if candidate:
                await ios_signaling.send({
                    "type": "ice",
                    "candidate": candidate.to_sdp(),
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                })

        offer = await ios.createOffer()
        await ios.setLocalDescription(offer)
        await ios_signaling.send({"type": "offer", "sdp": offer.sdp})

        for _ in range(50):
            await asyncio.sleep(0.1)
            if ios.remoteDescription is not None:
                break

        assert ios.remoteDescription is not None, "Desktop did not send answer"

        await asyncio.sleep(3)
        received_count = len(received_frames)
        logger.info("Received %d frames", received_count)

        await ios.close()
        await desktop.stop()
        await desktop_signaling.disconnect()
        await ios_signaling.disconnect()

    assert received_count > 0, "No video frames received"
    logger.info("桌面端视频管线测试通过")


if __name__ == "__main__":
    asyncio.run(test())
