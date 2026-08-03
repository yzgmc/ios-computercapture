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
                 on_frame=None, max_buffered_frames: int = 1,
                 on_disconnect=None):
        self.host = host
        self.port = port
        self.on_frame = on_frame
        self.on_disconnect = on_disconnect  # USB 模式连接断开回调
        self._max_buffered = max_buffered_frames
        self._server: asyncio.AbstractServer | None = None
        # 客户端模式（USB 直连）持有的读写流与后台任务
        self._client_reader: asyncio.StreamReader | None = None
        self._client_writer: asyncio.StreamWriter | None = None
        self._client_task: asyncio.Task | None = None
        self._frame_count = 0
        # 接收 fps 统计
        self._recv_t0: float | None = None
        self._recv_n: int = 0

    async def start(self):
        """LAN 模式：作为 TCP 服务器监听 host:port，等待 iOS 主动连接。"""
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port,
        )
        logger.info("RawStreamReceiver listening on %s:%d (TCP)", self.host, self.port)

    async def connect_client(self, host: str, port: int):
        """USB 模式：作为 TCP 客户端连接 host:port。

        数据流：本机 → PC 127.0.0.1:port → usbmuxd → iOS 127.0.0.1:port → iOS listener。
        与 UsbmuxTcpForwarder 配合使用：forwarder 监听 PC 127.0.0.1:port，
        本方法连接到该 forwarder，由 forwarder 转发到 iOS 端的 NWListener。
        """
        logger.info("RawStreamReceiver connecting to %s:%d (TCP client)", host, port)
        self._client_reader, self._client_writer = await asyncio.open_connection(host, port)
        self._client_task = asyncio.create_task(
            self._read_frames(self._client_reader, self._client_writer,
                              peer=f"{host}:{port}"),
            name="raw-stream-client",
        )

    async def stop(self):
        # 关闭服务器模式
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        # 关闭客户端模式
        if self._client_task is not None:
            self._client_task.cancel()
            try:
                await self._client_task
            except (asyncio.CancelledError, Exception):
                pass
            self._client_task = None
        if self._client_writer is not None:
            try:
                self._client_writer.close()
            except Exception:
                pass
            self._client_writer = None
        self._client_reader = None

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        logger.info("RawStream client connected: %s", peer)
        await self._read_frames(reader, writer, peer=peer)

    async def _read_frames(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter, peer):
        """通用帧读取循环：服务器模式与客户端模式共用。

        协议：28B 帧头（含 magic）+ payload_length 字节 payload，循环直至对端关闭。
        """
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
        except asyncio.CancelledError:
            logger.info("RawStream read loop cancelled from %s", peer)
            raise
        except Exception as e:
            logger.error("RawStream client error: %s", e)
        finally:
            try:
                writer.close()
            except Exception:
                pass
            logger.info("RawStream client disconnected: %s", peer)
            # 通知上层（USB 模式可用于触发重连）
            if self.on_disconnect:
                try:
                    self.on_disconnect()
                except Exception:
                    pass
