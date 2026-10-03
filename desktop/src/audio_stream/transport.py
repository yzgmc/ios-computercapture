"""音频接收器：UDP 数据报（LAN / SRT）+ TCP 流式（USB 桥接）。

两种传输方式共用同一套 16B AUD1 帧头：
- UDP: 一个数据报 = 16B 头 + payload（天然保持消息边界）
- TCP: 发送方连续写入 16B 头 + payload，接收方先读满 16B 头、
  解析出 payload_length 后再精确读 payload_length 字节，得到完整一包。

TCP 通道存在的理由：usbmuxd 只转发 TCP、不能转发 UDP。USB 直连模式下
iOS 端把音频发往本机回环地址时，桌面端只能通过 TCP 端口转发收到，
否则 USB 模式下的麦克风实际上是断的。
"""
import asyncio
import logging

from .protocol import HEADER_SIZE, is_valid_header, unpack_header

logger = logging.getLogger(__name__)

# TCP 单包读取超时（秒）
_TCP_READ_TIMEOUT = 5.0


class AudioStreamReceiver:
    """音频包接收器，支持 UDP 监听与 TCP 服务端/客户端三种形态。

    同一时刻只使用其中一种；切换传输模式时先 stop() 再重新 start()。
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 5001,
                 on_packet=None):
        self.host = host
        self.port = port
        self.on_packet = on_packet
        # UDP
        self._transport: asyncio.DatagramTransport | None = None
        # TCP
        self._tcp_server: asyncio.AbstractServer | None = None
        self._tcp_reader: asyncio.StreamReader | None = None
        self._tcp_writer: asyncio.StreamWriter | None = None
        self._tcp_task: asyncio.Task | None = None
        self._packets = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self):
        """UDP 监听模式（LAN / SRT 使用的默认形态）。"""
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _AudioProtocol(self),
            local_addr=(self.host, self.port),
        )
        logger.info("AudioStreamReceiver listening on %s:%d (UDP)",
                    self.host, self.port)

    async def start_tcp(self):
        """TCP 服务端模式：监听 host:port，等待对端连接。

        用于需要通过反向连接建立音频通道的场景（与 RawStreamReceiver.start 对称）。
        """
        self._tcp_server = await asyncio.start_server(
            self._handle_tcp_client, self.host, self.port,
        )
        logger.info("AudioStreamReceiver listening on %s:%d (TCP)",
                    self.host, self.port)

    async def connect_tcp_client(self, host: str, port: int):
        """TCP 客户端模式（USB 直连）：连接 usbmuxd 转发出来的本机端口。

        数据流：本机 → PC 127.0.0.1:port → usbmuxd → iOS 127.0.0.1:port。
        """
        logger.info("AudioStreamReceiver connecting to %s:%d (TCP client)",
                    host, port)
        self._tcp_reader, self._tcp_writer = await asyncio.open_connection(host, port)
        self._tcp_task = asyncio.create_task(
            self._read_tcp_frames(self._tcp_reader, self._tcp_writer,
                                  peer=f"{host}:{port}"),
            name="audio-stream-tcp-client",
        )

    async def stop(self):
        """关闭 UDP / TCP 全部资源，可重复调用。"""
        if self._transport is not None:
            self._transport.close()
            # Windows 上 UDP socket close 后端口不会立即释放，
            # 让事件循环完成底层关闭，避免紧接着 rebind 同端口报 WinError 10048
            await asyncio.sleep(0.05)
            self._transport = None

        if self._tcp_server is not None:
            self._tcp_server.close()
            try:
                await self._tcp_server.wait_closed()
            except Exception:
                pass
            self._tcp_server = None

        if self._tcp_task is not None:
            self._tcp_task.cancel()
            try:
                await self._tcp_task
            except (asyncio.CancelledError, Exception):
                pass
            self._tcp_task = None

        if self._tcp_writer is not None:
            try:
                self._tcp_writer.close()
            except Exception:
                pass
            self._tcp_writer = None
        self._tcp_reader = None

    # ------------------------------------------------------------------ #
    # 内部：分帧与回调
    # ------------------------------------------------------------------ #

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
        self._emit(payload, fields, peer=str(addr))

    async def _handle_tcp_client(self, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        logger.info("AudioStream TCP client connected: %s", peer)
        await self._read_tcp_frames(reader, writer, peer=peer)

    async def _read_tcp_frames(self, reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter, peer):
        """TCP 流式分帧循环：16B 头 + payload_length 字节 payload。"""
        try:
            while True:
                try:
                    header = await asyncio.wait_for(
                        reader.readexactly(HEADER_SIZE), timeout=_TCP_READ_TIMEOUT
                    )
                except asyncio.IncompleteReadError:
                    break  # 对端正常关闭
                except asyncio.TimeoutError:
                    logger.warning("AudioStream TCP header timeout from %s", peer)
                    break

                if not is_valid_header(header):
                    logger.warning("AudioStream invalid magic from %s, closing", peer)
                    break

                fields = unpack_header(header)
                payload_length = fields["payload_length"]
                if payload_length == 0 or payload_length > 65535:
                    logger.warning("AudioStream bad payload_length=%d from %s",
                                   payload_length, peer)
                    break

                try:
                    payload = await asyncio.wait_for(
                        reader.readexactly(payload_length), timeout=_TCP_READ_TIMEOUT
                    )
                except asyncio.IncompleteReadError:
                    logger.warning("AudioStream TCP payload truncated from %s", peer)
                    break
                except asyncio.TimeoutError:
                    logger.warning("AudioStream TCP payload timeout from %s", peer)
                    break

                self._emit(payload, fields, peer=str(peer))
        except ConnectionResetError:
            logger.info("AudioStream TCP connection reset by %s", peer)
        except asyncio.CancelledError:
            logger.info("AudioStream TCP read loop cancelled from %s", peer)
            raise
        except Exception as e:
            logger.error("AudioStream TCP client error: %s", e)
        finally:
            try:
                writer.close()
            except Exception:
                pass
            logger.info("AudioStream TCP client disconnected: %s", peer)

    def _emit(self, payload: bytes, fields: dict, peer: str):
        self._packets += 1
        if self._packets == 1:
            logger.info("AudioStream first packet from %s: %dHz %dch format=%d",
                        peer, fields["sample_rate"], fields["channels"],
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
