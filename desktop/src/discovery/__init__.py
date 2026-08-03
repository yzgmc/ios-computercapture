"""局域网自动发现服务：监听 UDP 50000，等待 iPhone 端发来的发现请求并回包。

iPhone 端发送: b"PHONECAM_DISCOVER"
桌面端回包:   f"{ip}|{tcp_port}|{udp_port}" (UTF-8)
"""
import asyncio
import logging
import socket
from typing import Optional

logger = logging.getLogger(__name__)

DISCOVERY_PORT = 50000
DISCOVERY_MAGIC = b"PHONECAM_DISCOVER"
BROADCAST_INTERVAL = 10.0  # 秒


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, host_ip: str, tcp_port: int, udp_port: int):
        self.host_ip = host_ip
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self._transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self._transport = transport
        sock = transport.get_extra_info("socket")
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        logger.info("DiscoveryService listening on 0.0.0.0:%d (UDP)", DISCOVERY_PORT)

    def datagram_received(self, data, addr):
        if data == DISCOVERY_MAGIC:
            payload = f"{self.host_ip}|{self.tcp_port}|{self.udp_port}".encode("utf-8")
            try:
                self._transport.sendto(payload, addr)
                logger.info("Discovery: answered %s -> %s", addr, payload.decode())
            except Exception as e:
                logger.warning("Discovery send error: %s", e)

    def error_received(self, exc):
        logger.warning("Discovery error: %s", exc)


class DiscoveryService:
    """UDP 自动发现 + 周期性主动广播。"""

    def __init__(self, host_ip: str, tcp_port: int, udp_port: int):
        self.host_ip = host_ip
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._broadcast_task: Optional[asyncio.Task] = None

    async def start(self):
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
        sock.setblocking(False)
        protocol = DiscoveryProtocol(self.host_ip, self.tcp_port, self.udp_port)
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol, sock=sock
        )
        self._broadcast_task = loop.create_task(self._broadcast_loop())

    async def _broadcast_loop(self):
        """周期性广播，让 iPhone 端无需主动查询。"""
        payload = f"{self.host_ip}|{self.tcp_port}|{self.udp_port}".encode("utf-8")
        try:
            while self._transport is not None:
                try:
                    self._transport.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
                except Exception as e:
                    logger.debug("Discovery broadcast error (ignored): %s", e)
                await asyncio.sleep(BROADCAST_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        if self._broadcast_task:
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except Exception:
                pass
        if self._transport:
            self._transport.close()
            self._transport = None
        logger.info("DiscoveryService stopped")
