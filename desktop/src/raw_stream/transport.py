"""原画质帧 TCP 流式接收。

接收方监听 TCP 端口，每个连接按"28B 帧头 + payload_length 字节 payload"循环分帧。
完整帧通过 on_frame 回调输出，回调签名:
    on_frame(raw_bytes, width, height, pixel_format, bytes_per_row)
"""
import asyncio
import logging
import time

from .protocol import HEADER_SIZE, is_valid_header, unpack_header

logger = logging.getLogger(__name__)

# 单连接读取超时（秒），超时关闭连接避免句柄泄漏
_READ_TIMEOUT = 5.0


class RawStreamReceiver:
    """TCP 原画质帧接收器。"""

    def __init__(self, host: str = "0.0.0.0", port: int = 5000,
                 on_frame=None, max_buffered_frames: int = 1):
        self.host = host
        self.port = port
        self.on_frame = on_frame
        self._max_buffered = max_buffered_frames
        self._server: asyncio.AbstractServer | None = None
        self._frame_count = 0
        # 接收 fps 统计
        self._recv_t0: float | None = None
        self._recv_n: int = 0

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port,
        )
        logger.info("RawStreamReceiver listening on %s:%d (TCP)", self.host, self.port)

    async def stop(self):
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        logger.info("RawStream client connected: %s", peer)
        try:
            while True:
                try:
                    header = await asyncio.wait_for(
                        reader.readexactly(HEADER_SIZE), timeout=_READ_TIMEOUT
                    )
                except asyncio.IncompleteReadError:
                    # 对端正常关闭
                    break
                except asyncio.TimeoutError:
                    logger.warning("RawStream header timeout from %s", peer)
                    break

                if not is_valid_header(header):
                    logger.warning("RawStream invalid magic from %s, len=%d, got bytes=%s, closing",
                                   peer, len(header), header[:8].hex())
                    break
                fields = unpack_header(header)
                payload_length = fields["payload_length"]
                if payload_length == 0 or payload_length > 64 * 1024 * 1024:
                    # 单帧上限 64MB（足够 4K BGRA ≈ 32MB），防御异常包
                    logger.warning("RawStream bad payload_length=%d from %s",
                                   payload_length, peer)
                    break

                try:
                    payload = await asyncio.wait_for(
                        reader.readexactly(payload_length), timeout=_READ_TIMEOUT
                    )
                except asyncio.IncompleteReadError:
                    logger.warning("RawStream payload truncated from %s", peer)
                    break
                except asyncio.TimeoutError:
                    logger.warning("RawStream payload timeout from %s", peer)
                    break

                self._frame_count += 1
                self._recv_n += 1
                now = time.monotonic()
                if self._recv_t0 is None:
                    self._recv_t0 = now
                if self._frame_count == 1:
                    logger.info("RawStream first frame: %dx%d bpr=%d payload=%d",
                                fields["width"], fields["height"],
                                fields["bytes_per_row"], payload_length)
                # 每秒打印一次接收 fps
                elapsed = now - self._recv_t0
                if elapsed >= 1.0:
                    fps = self._recv_n / elapsed
                    logger.info("RawStream recv %.1f fps (total=%d, payload=%d KB)",
                                fps, self._frame_count, payload_length // 1024)
                    self._recv_t0 = now
                    self._recv_n = 0
                if self.on_frame:
                    try:
                        self.on_frame(
                            payload,
                            fields["width"], fields["height"],
                            fields["format"], fields["bytes_per_row"],
                        )
                    except Exception as e:
                        logger.error("on_frame callback error: %s", e)
        except ConnectionResetError:
            logger.info("RawStream connection reset by %s", peer)
        except Exception as e:
            logger.error("RawStream client error: %s", e)
        finally:
            try:
                writer.close()
            except Exception:
                pass
            logger.info("RawStream client disconnected: %s", peer)
