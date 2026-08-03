"""UDP 音频通道集成测试：验证 AudioStreamReceiver 能正确分帧解析音频包。

运行方式:
    python tests/test_audio_stream.py
"""
import asyncio
import logging
import os
import socket
import sys
import struct

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "desktop", "src"))
from audio_stream import (
    AudioStreamReceiver, AudioFormat,
    HEADER_SIZE, MAGIC, pack_header,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test():
    received = []

    receiver = AudioStreamReceiver(
        host="127.0.0.1", port=0,  # 0 = 随机可用端口
        on_packet=lambda pcm, sr, ch, fmt, seq: received.append((pcm, sr, ch, fmt, seq)),
    )

    # 用 0 端口启动后，从 transport 拿到实际绑定端口
    await receiver.start()
    sock = receiver._transport.get_extra_info("socket")
    port = sock.getsockname()[1]
    logger.info("AudioStreamReceiver bound to 127.0.0.1:%d", port)

    # 构造 3 个测试包：48kHz / mono / PCM16
    sample_rate = 48000
    channels = 1
    audio_format = AudioFormat.PCM16_LE

    test_payloads = [
        b"\x00\x01" * 100,  # 200 字节
        b"\x10\x20" * 200,  # 400 字节
        b"\xff\x7f" * 50,   # 100 字节
    ]

    async def send_packets():
        # 给 receiver 一点时间进入收包状态
        await asyncio.sleep(0.05)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i, payload in enumerate(test_payloads):
            header = pack_header(i, sample_rate, channels, audio_format, len(payload))
            s.sendto(header + payload, ("127.0.0.1", port))
            logger.info("Sent packet %d: %d bytes payload", i, len(payload))
            await asyncio.sleep(0.01)
        s.close()
        # 等待最后一个包被处理
        await asyncio.sleep(0.1)

    await send_packets()

    await receiver.stop()

    assert len(received) == 3, f"Expected 3 packets, got {len(received)}"
    for i, (pcm, sr, ch, fmt, seq) in enumerate(received):
        assert pcm == test_payloads[i], f"Packet {i} payload mismatch"
        assert sr == sample_rate
        assert ch == channels
        assert fmt == audio_format
        assert seq == i
        logger.info("Packet %d OK: %d bytes, seq=%d", i, len(pcm), seq)

    logger.info("UDP 音频通道测试通过")


if __name__ == "__main__":
    asyncio.run(test())
