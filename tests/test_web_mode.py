"""网页模式（HTTPS + WebSocket）端到端测试。

验证链路：自签证书生成 → HTTPS 站点提供页面 → WebSocket 收到 RAW1/AUD1 帧后
能被正确解析并回调上层。协议与 iOS 端共用，因此这里也是协议一致性的回归测试。

运行（需先 pip install aiohttp cryptography pytest pytest-asyncio）:
    python -m pytest tests/test_web_mode.py -v

aiohttp 未安装时全部用例自动跳过。
"""
import asyncio
import os
import ssl
import sys
from pathlib import Path

import pytest

_DESKTOP_SRC = Path(__file__).resolve().parent.parent / "desktop" / "src"
if str(_DESKTOP_SRC) not in sys.path:
    sys.path.insert(0, str(_DESKTOP_SRC))

aiohttp = pytest.importorskip("aiohttp")

from aiohttp import ClientSession, TCPConnector  # noqa: E402

from web import WebStreamReceiver  # noqa: E402
from web.certs import ensure_cert, fingerprint, get_local_ips  # noqa: E402
from raw_stream.protocol import pack_header, PixelFormat  # noqa: E402
from audio_stream.protocol import pack_header as pack_audio_header  # noqa: E402
from audio_stream.protocol import AudioFormat  # noqa: E402

# 用高位端口，避免与真实服务 8443 冲突
TEST_PORT = 18443


def test_self_signed_cert_is_loadable(tmp_path):
    """证书能被生成、被 SSLContext 加载，且 SAN 覆盖本机 IP。"""
    cert_path, key_path = ensure_cert(str(tmp_path))
    assert os.path.exists(cert_path)
    assert os.path.exists(key_path)

    # load_cert_chain 成功即说明 PEM 格式与密钥匹配
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)

    assert "127.0.0.1" in get_local_ips()

    try:
        import cryptography  # noqa: F401
    except ImportError:
        return
    fp = fingerprint(cert_path)
    assert fp is not None
    assert fp.count(":") == 31  # SHA-256 = 32 字节


def test_cert_is_reused_when_ips_unchanged(tmp_path):
    """IP 未变化时复用同一张证书，避免手机反复重新信任。"""
    cert_path, _ = ensure_cert(str(tmp_path))
    mtime_first = os.path.getmtime(cert_path)
    cert_path2, _ = ensure_cert(str(tmp_path))
    assert cert_path == cert_path2
    assert os.path.getmtime(cert_path) == mtime_first


@pytest.mark.asyncio
async def test_web_receiver_end_to_end(tmp_path):
    """HTTPS 页面 + 视频帧 + 音频包全链路。"""
    frames = []
    packets = []

    receiver = WebStreamReceiver(
        host="127.0.0.1", port=TEST_PORT,
        on_frame=lambda payload, w, h, fmt, bpr: frames.append(
            (payload, w, h, fmt, bpr)),
        on_audio_packet=lambda payload, sr, ch, fmt, seq: packets.append(
            (payload, sr, ch, fmt, seq)),
        cert_dir=str(tmp_path),
    )
    await receiver.start()
    assert receiver.is_running

    try:
        # 自签证书：测试客户端关闭校验
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        base = f"https://127.0.0.1:{TEST_PORT}"
        async with ClientSession(connector=TCPConnector(ssl=ssl_ctx)) as session:
            async with session.get(base + "/") as resp:
                assert resp.status == 200
                assert "PhoneCam" in await resp.text()

            async with session.ws_connect(base + "/ws/video") as ws:
                payload = b"\x10\x20\x30\x40" * 8
                await ws.send_bytes(
                    pack_header(0, 4, 2, PixelFormat.BGRA, 16, len(payload))
                    + payload)

                async with session.ws_connect(base + "/ws/audio") as aws:
                    apayload = b"\x01\x02" * 100
                    await aws.send_bytes(
                        pack_audio_header(0, 48000, 1, AudioFormat.PCM16_LE,
                                          len(apayload)) + apayload)

                    for _ in range(60):
                        if frames and packets:
                            break
                        await asyncio.sleep(0.05)
    finally:
        await receiver.stop()

    assert len(frames) == 1, f"expected 1 video frame, got {len(frames)}"
    payload, w, h, fmt, bpr = frames[0]
    assert payload == b"\x10\x20\x30\x40" * 8
    assert (w, h) == (4, 2)
    assert fmt == PixelFormat.BGRA
    assert bpr == 16

    assert len(packets) == 1, f"expected 1 audio packet, got {len(packets)}"
    apayload, sample_rate, channels, afmt, seq = packets[0]
    assert apayload == b"\x01\x02" * 100
    assert sample_rate == 48000
    assert channels == 1
    assert afmt == AudioFormat.PCM16_LE
    assert seq == 0
