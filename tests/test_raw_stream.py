"""TCP 视频原画质传输集成测试：验证 RawStreamReceiver 能正确分帧解析视频帧。

运行方式:
    python tests/test_raw_stream.py
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "desktop", "src"))
from raw_stream import RawStreamReceiver, PixelFormat, pack_header

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test():
    received = []

    receiver = RawStreamReceiver(
        host="127.0.0.1", port=0,
        on_frame=lambda raw, w, h, fmt, bpr: received.append((raw, w, h, fmt, bpr)),
    )

    await receiver.start()
    sock = receiver._server.sockets[0]
    port = sock.getsockname()[1]
    logger.info("RawStreamReceiver bound to 127.0.0.1:%d", port)

    # 构造 2 个 4x2 BGRA 帧
    width, height, bpr = 4, 2, 16
    test_frames = [
        b"\x10\x20\x30\x40" * 8,  # 32 字节
        b"\xff\x00\xff\x00" * 8,
    ]

    async def send_frames():
        await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        for i, payload in enumerate(test_frames):
            header = pack_header(i, width, height, PixelFormat.BGRA, bpr, len(payload))
            writer.write(header + payload)
            await writer.drain()
            logger.info("Sent frame %d: %d bytes payload", i, len(payload))
            await asyncio.sleep(0.01)
        writer.close()
        await asyncio.sleep(0.1)

    await send_frames()
    await receiver.stop()

    assert len(received) == 2, f"Expected 2 frames, got {len(received)}"
    for i, (raw, w, h, fmt, bpr_recv) in enumerate(received):
        assert raw == test_frames[i], f"Frame {i} payload mismatch"
        assert w == width
        assert h == height
        assert fmt == PixelFormat.BGRA
        assert bpr_recv == bpr
        logger.info("Frame %d OK: %dx%d bpr=%d %d bytes", i, w, h, bpr_recv, len(raw))

    logger.info("TCP 视频原画质传输测试通过")


if __name__ == "__main__":
    asyncio.run(test())
