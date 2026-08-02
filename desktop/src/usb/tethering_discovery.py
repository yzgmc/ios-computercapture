"""USB tethering 网段的 iPhone 信令端口探测。

iPhone 开启「个人热点」通过 USB 共享网络时，电脑会获得 172.20.10.x 网段地址，
iPhone 自身为 172.20.10.1（也可为该网段内其他地址）。iOS 应用监听 8080 信令端口
时，桌面端可在该网段内并发探测，定位 iPhone 的信令地址。

注意：tethering 网段地址在 iOS 设备上可能随热点重启变化，发现结果应缓存但需
允许失效重扫。
"""
import asyncio
import logging
import socket
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# iPhone USB tethering 默认网段
DEFAULT_TETHERING_NET = "172.20.10.0/24"
# iPhone 在 tethering 网段的常见地址（热点网关）
TETHERING_GATEWAY_CANDIDATES = ("172.20.10.1",)
DEFAULT_SIGNALING_PORT = 8080
# 并发探测的连接超时（秒）
PROBE_TIMEOUT = 0.5
# 最大并发探测数
MAX_CONCURRENT_PROBES = 32


@dataclass
class TetheringEndpoint:
    """一个被探测到的信令端点。"""
    ip: str
    port: int
    latency_ms: float

    def to_dict(self) -> dict:
        return {
            "ip": self.ip,
            "port": self.port,
            "latency_ms": round(self.latency_ms, 2),
        }

    def ws_url(self) -> str:
        return f"ws://{self.ip}:{self.port}"

    def __repr__(self) -> str:
        return f"TetheringEndpoint(ip={self.ip!r} port={self.port} latency={self.latency_ms:.1f}ms)"


class TetheringDiscovery:
    """在 tethering 网段内并发探测 iPhone 信令端口。"""

    def __init__(self, network: str = DEFAULT_TETHERING_NET,
                 port: int = DEFAULT_SIGNALING_PORT):
        self.network = network
        self.port = port

    def _iter_hosts(self) -> list[str]:
        """展开网段为候选主机列表。"""
        try:
            import ipaddress
            net = ipaddress.ip_network(self.network, strict=False)
            return [str(h) for h in net.hosts()]
        except Exception as e:
            logger.warning("Invalid network %r: %s", self.network, e)
            return list(TETHERING_GATEWAY_CANDIDATES)

    async def _probe(self, ip: str, port: int) -> TetheringEndpoint | None:
        """对单个 ip:port 做 TCP 连接探测，成功返回端点。"""
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            fut = asyncio.open_connection(ip, port)
            reader, writer = await asyncio.wait_for(fut, timeout=PROBE_TIMEOUT)
            latency = (loop.time() - start) * 1000.0
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return TetheringEndpoint(ip=ip, port=port, latency_ms=latency)
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
            return None
        except Exception as e:
            logger.debug("probe %s:%d error: %s", ip, port, e)
            return None

    async def discover(self) -> list[TetheringEndpoint]:
        """并发扫描整个 tethering 网段，返回可连通的信令端点列表。"""
        hosts = self._iter_hosts()
        sem = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

        async def bounded(ip: str) -> TetheringEndpoint | None:
            async with sem:
                return await self._probe(ip, self.port)

        logger.info("Scanning %s on port %d (%d hosts)",
                    self.network, self.port, len(hosts))
        results = await asyncio.gather(*(bounded(ip) for ip in hosts))
        found = [r for r in results if r is not None]
        found.sort(key=lambda e: e.latency_ms)
        if found:
            logger.info("Tethering endpoints found: %s", found)
        else:
            logger.info("No tethering endpoint responding on :%d", self.port)
        return found

    async def discover_first(self) -> TetheringEndpoint | None:
        """优先尝试网关地址，未命中再全量扫描。返回首个可达端点。"""
        for ip in TETHERING_GATEWAY_CANDIDATES:
            ep = await self._probe(ip, self.port)
            if ep is not None:
                logger.info("Fast path hit gateway %s", ip)
                return ep
        found = await self.discover()
        return found[0] if found else None

    def is_tethering_active(self) -> bool:
        """本机当前是否处于 tethering 网段（即 USB tethering 已启用）。"""
        try:
            import ipaddress
            target = ipaddress.ip_network(self.network, strict=False)
            for info in socket.getaddrinfo(socket.gethostname(), None,
                                           socket.AF_INET):
                ip = info[4][0]
                try:
                    if ipaddress.ip_address(ip) in target:
                        return True
                except ValueError:
                    continue
        except Exception as e:
            logger.debug("tethering active check failed: %s", e)
        return False
