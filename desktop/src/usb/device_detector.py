"""Apple USB 设备枚举。

通过 pyusb 枚举 VID=0x05AC 的 Apple 设备，用于在桌面端判断 iPhone 是否通过
USB 物理连接。注意：iOS 未越狱时无法通过 USB 启动自定义服务，本模块仅做存在性
检测，实际通信仍走 tethering 网络的 TCP/UDP（见 tethering_discovery.py）。

UDID 无法直接从 pyusb 取得（需 usbmuxd/libimobiledevice），此处返回序列号字段
作为设备标识，调用方可结合 tethering 发现结果做匹配。pyusb 未安装时返回空列表，
上层逻辑应优雅降级为纯 Wi-Fi 模式。
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

APPLE_VENDOR_ID = 0x05AC


class AppleUsbDevice:
    """一个被检测到的 Apple USB 设备的描述信息。"""

    def __init__(self, vendor_id: int, product_id: int,
                 name: str = "", serial: str = ""):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.name = name
        # USB 序列号字段（与 iOS UDID 不同，但可用于区分多台接入设备）
        self.serial = serial

    @property
    def is_ios_device(self) -> bool:
        """粗略判断是否为 iPhone/iPad。

        Apple 的 iPhone/iPad 的 USB Product ID 范围较广且随机型变化，
        无法通过 PID 精确判定。此处一律视为可能的 iOS 设备，由上层结合
        tethering 探测结果确认。
        """
        return True

    def to_dict(self) -> dict:
        return {
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "name": self.name,
            "serial": self.serial,
            "is_ios_device": self.is_ios_device,
        }

    def __repr__(self) -> str:
        return (
            f"AppleUsbDevice(vid=0x{self.vendor_id:04X} "
            f"pid=0x{self.product_id:04X} name={self.name!r} "
            f"serial={self.serial!r})"
        )


class UsbDeviceDetector:
    """枚举已接入的 Apple USB 设备。

    pyusb 依赖后端（Windows 上通常为 libusb），若后端未安装则枚举返回空列表，
    不影响桌面端启动。
    """

    def __init__(self, vendor_id: int = APPLE_VENDOR_ID):
        self.vendor_id = vendor_id
        self._usb = None
        try:
            import usb.core  # noqa: F401
            import usb.util  # noqa: F401
            self._usb_available = True
        except ImportError:
            self._usb_available = False
            logger.info("pyusb not installed, USB device detection disabled")
        except Exception as e:  # 后端缺失等情况
            self._usb_available = False
            logger.info("pyusb backend unavailable: %s", e)

    @property
    def available(self) -> bool:
        """pyusb 及其后端是否可用。"""
        return self._usb_available

    def list_apple_devices(self) -> list[AppleUsbDevice]:
        """枚举所有 VID 匹配的 Apple USB 设备。"""
        if not self._usb_available:
            return []
        try:
            import usb.core
            import usb.util
        except Exception as e:
            logger.warning("pyusb import failed: %s", e)
            return []

        devices: list[AppleUsbDevice] = []
        try:
            for dev in usb.core.find(find_all=True, idVendor=self.vendor_id):
                name = self._read_string(dev, dev.iProduct)
                serial = self._read_string(dev, dev.iSerialNumber)
                devices.append(AppleUsbDevice(
                    vendor_id=dev.idVendor,
                    product_id=dev.idProduct,
                    name=name,
                    serial=serial,
                ))
        except Exception as e:
            logger.warning("USB enumeration failed: %s", e)
        return devices

    @staticmethod
    def _read_string(dev, index: Optional[int]) -> str:
        """安全读取 USB 字符串描述符，失败返回空串。"""
        if not index:
            return ""
        try:
            import usb.util
            return usb.util.get_string(dev, index)
        except Exception:
            return ""

    def has_apple_device(self) -> bool:
        """是否存在任意 Apple USB 设备。"""
        return bool(self.list_apple_devices())
