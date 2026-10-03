"""USB 连接检测 + 直连桥接模块。

iOS 未越狱无法在 USB 上启动自定义服务，但可通过 usbmuxd 协议把 iOS App 监听的
127.0.0.1:<port> 透明桥接到桌面端的 127.0.0.1:<port>，无需 Wi-Fi 也不需要个人热点。

模块组成：
- bridge.py: 基于 pymobiledevice3 的 usbmuxd 端口转发（真正的"USB 直连"）。
  视频走 TCP 5000，音频走 TCP 5002 —— usbmuxd 只转发 TCP，
  所以 USB 模式下音频不能沿用 LAN 的 UDP 5001。
- device_detector.py: 通过 pyusb 枚举 Apple USB 设备（仅存在性检测，当前未接线）
- tethering_discovery.py: 在 USB tethering 网段（172.20.10.0/24）发现 iPhone
  （历史遗留：usbmuxd 桥接落地后已不参与主流程，保留备用）
"""
from .device_detector import UsbDeviceDetector, AppleUsbDevice
from .tethering_discovery import TetheringDiscovery, TetheringEndpoint
from .bridge import (
    UsbBridge,
    UsbBridgeManager,
    is_usb_available,
    is_usbmuxd_reachable,
    get_windows_amds_status,
    diagnose_usb,
    list_ios_devices,
    DEFAULT_TCP_PORT as USB_DEFAULT_TCP_PORT,
    DEFAULT_UDP_PORT as USB_DEFAULT_UDP_PORT,
    DEFAULT_AUDIO_TCP_PORT as USB_DEFAULT_AUDIO_TCP_PORT,
)

__all__ = [
    "UsbDeviceDetector", "AppleUsbDevice",
    "TetheringDiscovery", "TetheringEndpoint",
    "UsbBridge", "UsbBridgeManager",
    "is_usb_available", "is_usbmuxd_reachable",
    "get_windows_amds_status", "diagnose_usb",
    "list_ios_devices",
    "USB_DEFAULT_TCP_PORT", "USB_DEFAULT_UDP_PORT", "USB_DEFAULT_AUDIO_TCP_PORT",
]
