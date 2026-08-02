"""原画质帧 UDP 接收与分片重组。"""
import asyncio
import logging
import time

from .protocol import HEADER_SIZE, is_valid_header, unpack_header

logger = logging.getLogger(__name__)

# 不完整帧的最大存活时间（秒），超时丢弃避免内存泄漏
_FRAME_TTL = 1.0


class _FrameAssembler:
    """重组单个帧的分片。"""

    def __init__(self, total_chunks: int, width: int, height: int,
                 pixel_format: int, bytes_per_row: int):
        self.total_chunks = total_chunks
        self.width = width
        self.height = height
        self.pixel_format = pixel_format
        self.bytes_per_row = bytes_per_row
        self.chunks: dict[int, bytes] = {}
        self.created_at = time.monotonic()

    def add_chunk(self, idx: int, data: bytes) -> None:
        if idx not in self.chunks:
            self.chunks[idx] = data

    @property
    def complete(self) -> bool:
        return len(self.chunks) == self.total_chunks

    def assemble(self) -> bytes:
        return b"".join(self.chunks[i] for i in range(self.total_chunks))

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > _FRAME_TTL


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self.on_packet = on_packet

    def datagram_received(self, data, addr):
        try:
            self.on_packet(data)
        except Exception as e:
            logger.error("Packet handling error: %s", e)

    def error_received(self, exc):
        logger.error("UDP receive error: %s", exc)


class RawStreamReceiver:
    """UDP 原画质帧接收器。

    监听指定端口，按 frame_id 重组分片，完整帧通过 on_frame 回调输出。
    回调签名: on_frame(raw_bytes, width, height, pixel_format, bytes_per_row)
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 5000,
                 on_frame=None, max_buffered_frames: int = 4):
        self.host = host
        self.port = port
        self.on_frame = on_frame
        # 限制已重组但未被消费的帧数量，避免消费端慢导致内存堆积
        self._max_buffered = max_buffered_frames
        self._transport: asyncio.DatagramTransport | None = None
        self._pending: dict[int, _FrameAssembler] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._frame_count = 0

    async def start(self):
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _DatagramProtocol(self._handle_packet),
            local_addr=(self.host, self.port),
        )
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("RawStreamReceiver listening on %s:%d", self.host, self.port)

    async def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        if self._transport:
            self._transport.close()
            self._transport = None
        self._pending.clear()

    def _handle_packet(self, data: bytes):
        if not is_valid_header(data):
            return
        header = unpack_header(data)
        payload = data[HEADER_SIZE:]
        frame_id = header["frame_id"]
        idx = header["chunk_idx"]

        assembler = self._pending.get(frame_id)
        if assembler is None:
            assembler = _FrameAssembler(
                header["total_chunks"], header["width"], header["height"],
                header["format"], header["bytes_per_row"],
            )
            self._pending[frame_id] = assembler

        assembler.add_chunk(idx, payload)

        if assembler.complete:
            del self._pending[frame_id]
            self._frame_count += 1
            if self.on_frame:
                try:
                    self.on_frame(
                        assembler.assemble(),
                        assembler.width, assembler.height, assembler.pixel_format,
                        assembler.bytes_per_row,
                    )
                except Exception as e:
                    logger.error("on_frame callback error: %s", e)
            if self._frame_count % 60 == 0:
                logger.info("RawStream received %d frames", self._frame_count)

    async def _cleanup_loop(self):
        """定期清理过期的不完整帧。"""
        while True:
            await asyncio.sleep(0.5)
            expired = [fid for fid, asm in self._pending.items() if asm.expired]
            for fid in expired:
                self._pending.pop(fid, None)
            if expired:
                logger.debug("Dropped %d incomplete frames", len(expired))
