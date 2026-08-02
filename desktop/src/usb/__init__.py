"""USB 连接检测模块。

iOS 未越狱无法启动自定义 USB 服务，本模块仅用于：
1. 检测是否有 Apple USB 设备接入（device_detector.py，基于 pyusb）
2. 在 USB tethering 网段（172.20.10.0/24）发现 iPhone 信令端口（tethering_discovery.py）

实际数据通道走 tethering 网络的 TCP/UDP，与 Wi-Fi 链路同协议，无独立 USB 通道。
"""
from .device_detector import UsbDeviceDetector, AppleUsbDevice
from .tethering_discovery import TetheringDiscovery, TetheringEndpoint

__all__ = [
    "UsbDeviceDetector", "AppleUsbDevice",
    "TetheringDiscovery", "TetheringEndpoint",
]
