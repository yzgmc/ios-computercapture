"""UDP 音频接收器。

监听 UDP 端口，按 16B 帧头解析音频包，调用 on_packet 回调输出 PCM。
UDP 不保证可靠性，丢包直接丢弃对应音频片段，不影响后续。
"""
import asyncio
import logging

from .protocol import HEADER_SIZE, is_valid_header, unpack_header

logger = logging.getLogger(__name__)


class AudioStreamReceiver:
    """UDP 音频包接收器。"""

    def __init__(self, host: str = "0.0.0.0", port: int = 5001,
                 on_packet=None):
        self.host = host
        self.port = port
        self.on_packet = on_packet
        self._transport: asyncio.DatagramTransport | None = None
        self._packets = 0

    async def start(self):
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _AudioProtocol(self),
            local_addr=(self.host, self.port),
        )
        logger.info("AudioStreamReceiver listening on %s:%d (UDP)",
                    self.host, self.port)

    async def stop(self):
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _handle_datagram(self, data: bytes, addr):
        if len(data) < HEADER_SIZE:
            return
        if not is_valid_header(data):
            return
        fields = unpack_header(data)
        payload_length = fields["payload_length"]
        payload = data[HEADER_SIZE:HEADER_SIZE + payload_length]
        if len(payload) != payload_length:
            # 数据报长度与帧头声明不符，丢弃
            return
        self._packets += 1
        if self._packets == 1:
            logger.info("AudioStream first packet from %s: %dHz %dch format=%d",
                        addr, fields["sample_rate"], fields["channels"],
                        fields["format"])
        if self._packets % 1000 == 0:
            logger.info("AudioStream received %d packets", self._packets)
        if self.on_packet:
            try:
                self.on_packet(
                    payload,
                    fields["sample_rate"],
                    fields["channels"],
                    fields["format"],
                    fields["seq"],
                )
            except Exception as e:
                logger.error("on_packet callback error: %s", e)


class _AudioProtocol(asyncio.DatagramProtocol):
    def __init__(self, receiver: AudioStreamReceiver):
        self._receiver = receiver

    def datagram_received(self, data: bytes, addr):
        self._receiver._handle_datagram(data, addr)

    def error_received(self, exc):
        logger.warning("AudioStream UDP error: %s", exc)
