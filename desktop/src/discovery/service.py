import logging
import socket
from typing import Optional

from zeroconf import ServiceInfo, Zeroconf

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_phonecam._tcp.local."
SERVICE_NAME = "PhoneCam Signaling Server"


class DiscoveryService:
    """通过 mDNS/Bonjour 广播信令服务器地址，供 iOS 端自动发现。"""

    def __init__(self, port: int = 8080, name: str = SERVICE_NAME):
        self.port = port
        self.name = name
        self._zeroconf: Optional[Zeroconf] = None
        self._info: Optional[ServiceInfo] = None

    def _get_local_ip(self) -> str:
        try:
            # 通过 UDP 连接获取本机 IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0)
            try:
                s.connect(("10.254.254.254", 1))
                ip = s.getsockname()[0]
            except Exception:
                ip = "127.0.0.1"
            finally:
                s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def start(self):
        ip = self._get_local_ip()
        self._zeroconf = Zeroconf()
        self._info = ServiceInfo(
            type_=SERVICE_TYPE,
            name=f"{self.name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=self.port,
            properties={
                "path": "/ws",
                "version": "1.0",
            },
        )
        self._zeroconf.register_service(self._info)
        logger.info("mDNS service registered: %s:%d (%s)", ip, self.port, SERVICE_TYPE)

    def stop(self):
        if self._zeroconf and self._info:
            self._zeroconf.unregister_service(self._info)
            self._zeroconf.close()
            self._zeroconf = None
            self._info = None
            logger.info("mDNS service unregistered")
