"""USB 直连桥接：基于 pymobiledevice3 的 usbmuxd 协议。

iOS App 监听 127.0.0.1:5000 (TCP 视频) + 127.0.0.1:5001 (UDP 音频)。
本模块在桌面端创建 usbmuxd 端口转发，把 PC 的 127.0.0.1:<port> 透明桥接到
iOS 设备的 127.0.0.1:<port>，无需 Wi-Fi，无需个人热点。
"""
import asyncio
import logging
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)

# 默认端口
DEFAULT_TCP_PORT = 5000
DEFAULT_UDP_PORT = 5001

# pymobiledevice3 的接口可能因版本变化，按需 try/except
try:
    from pymobiledevice3.tcp_forwarder import TcpForwarder as _Pm3TcpForwarder
    from pymobiledevice3.usbmuxd import (
        connect as usbmuxd_connect,
        list_devices as usbmuxd_list_devices,
    )
    _PM3_AVAILABLE = True
except ImportError as e:
    logger.warning("pymobiledevice3 not available: %s", e)
    _PM3_AVAILABLE = False
except Exception as e:  # usbmuxd 后端未启动等
    logger.warning("pymobiledevice3 import failed: %s", e)
    _PM3_AVAILABLE = False


def is_usb_available() -> bool:
    """pymobiledevice3 是否可用（包含 usbmuxd 后端）。"""
    return _PM3_AVAILABLE


async def list_ios_devices() -> list[dict]:
    """通过 usbmuxd 列出所有已 USB 接入的 iOS 设备。

    Returns:
        [{'udid': 'xxx', 'connection_type': 'USB', 'product_id': 4776, ...}, ...]
    """
    if not _PM3_AVAILABLE:
        return []
    try:
        # pymobiledevice3 3.x 同步 API，丢到 default executor
        loop = asyncio.get_running_loop()
        devs = await loop.run_in_executor(None, lambda: list(usbmuxd_list_devices()))
        out = []
        for d in devs:
            try:
                out.append({
                    "udid": getattr(d, "udid", None) or getattr(d, "serial", None),
                    "connection_type": "USB",
                    "product_id": getattr(d, "product_id", None),
                })
            except Exception:
                continue
        return out
    except Exception as e:
        logger.debug("list_ios_devices failed: %s", e)
        return []


class UsbBridge:
    """一条 USB TCP 端口转发（PC 端 <--桥接--> iOS 端）。

    使用方式：
        bridge = UsbBridge(udid, host_port=5000, device_port=5000)
        await bridge.start()    # 后台跑，转发开始
        ...
        await bridge.stop()
    """

    def __init__(self, udid: str, host_port: int, device_port: int,
                 on_state_change: Optional[Callable[[str], None]] = None):
        self.udid = udid
        self.host_port = host_port
        self.device_port = device_port
        self.on_state_change = on_state_change or (lambda s: None)
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self.state = "idle"  # idle / starting / running / stopped / error

    def _set_state(self, s: str):
        self.state = s
        try:
            self.on_state_change(s)
        except Exception:
            pass

    async def start(self):
        if not _PM3_AVAILABLE:
            self._set_state("error")
            raise RuntimeError("pymobiledevice3 not installed")
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._set_state("starting")
        self._task = asyncio.create_task(self._run(), name=f"usb-bridge-{self.host_port}")

    async def stop(self):
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._set_state("stopped")

    async def _run(self):
        """实际转发循环。pymobiledevice3 的 TcpForwarder 阻塞运行，
        在独立线程跑并监听 stop_event。"""
        self._set_state("running")
        loop = asyncio.get_running_loop()

        def _run_forwarder():
            try:
                with _Pm3TcpForwarder(self.udid, self.host_port, self.device_port):
                    # 阻塞，直到 stop_event 或异常
                    while not self._stop_event.is_set():
                        import time
                        time.sleep(0.2)
            except Exception as e:
                logger.error("USB bridge forwarder error: %s", e)
                self._set_state("error")

        try:
            await loop.run_in_executor(None, _run_forwarder)
        except asyncio.CancelledError:
            pass
        if self.state not in ("error", "stopped"):
            self._set_state("stopped")


class UsbBridgeManager:
    """统一管理多条桥接 + 设备插拔检测。

    - 自动检测 iOS 设备
    - 自动为每个设备建立 TCP/UDP 桥接
    - 设备断开时清理
    """

    def __init__(self,
                 tcp_port: int = DEFAULT_TCP_PORT,
                 udp_port: int = DEFAULT_UDP_PORT,
                 on_state: Optional[Callable[[str, str], None]] = None,
                 on_devices_changed: Optional[Callable[[list], Awaitable[None]]] = None):
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        # on_state(level, message): level ∈ info/warn/error
        self.on_state = on_state or (lambda lvl, msg: None)
        # on_devices_changed(devices)
        self.on_devices_changed = on_devices_changed

        self._bridges: dict[str, dict[str, UsbBridge]] = {}
        self._current_udid: Optional[str] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def _log(self, lvl: str, msg: str):
        try:
            self.on_state(lvl, msg)
        except Exception:
            pass

    def get_active_devices(self) -> list[dict]:
        return [{"udid": udid, "bridges": list(self._bridges.get(udid, {}).keys())}
                for udid in self._bridges]

    async def start(self, target_udid: Optional[str] = None):
        """启动监控 + 为目标设备建桥。target_udid=None 则自动选第一台。"""
        if not _PM3_AVAILABLE:
            self._log("error", "pymobiledevice3 未安装，USB 直连不可用")
            return
        self._stop.clear()
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name="usb-monitor")
        if target_udid:
            await self._ensure_bridges(target_udid)

    async def stop(self):
        self._stop.set()
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except Exception:
                pass
            self._monitor_task = None
        for udid, bridges in list(self._bridges.items()):
            for b in list(bridges.values()):
                try:
                    await b.stop()
                except Exception:
                    pass
        self._bridges.clear()
        self._log("info", "USB 直连已停止")

    async def _ensure_bridges(self, udid: str):
        """为指定 udid 建立 TCP 桥接（UDP 走 tethering/Wi-Fi，不做桥接）。"""
        if udid in self._bridges and "tcp" in self._bridges[udid]:
            return
        self._log("info", f"建立 USB 桥接 → {udid[:8]}... (TCP {self.tcp_port}↔{self.tcp_port})")
        bridge = UsbBridge(udid, self.tcp_port, self.tcp_port,
                           on_state_change=lambda s: self._log("info", f"[{udid[:8]}] 桥接状态: {s}"))
        try:
            await bridge.start()
            self._bridges.setdefault(udid, {})["tcp"] = bridge
            self._current_udid = udid
            if self.on_devices_changed:
                try:
                    await self.on_devices_changed(self.get_active_devices())
                except Exception:
                    pass
        except Exception as e:
            self._log("error", f"USB 桥接失败: {e}")

    async def _monitor_loop(self):
        """周期性检测设备插拔。"""
        while not self._stop.is_set():
            try:
                devs = await list_ios_devices()
                current = {d["udid"] for d in devs if d.get("udid")}

                # 移除已断开设备
                for udid in list(self._bridges.keys()):
                    if udid not in current:
                        self._log("warn", f"设备断开: {udid[:8]}...")
                        for b in list(self._bridges[udid].values()):
                            try:
                                await b.stop()
                            except Exception:
                                pass
                        del self._bridges[udid]
                        if self._current_udid == udid:
                            self._current_udid = None
                        if self.on_devices_changed:
                            try:
                                await self.on_devices_changed(self.get_active_devices())
                            except Exception:
                                pass

                if self.on_devices_changed:
                    try:
                        await self.on_devices_changed(self.get_active_devices())
                    except Exception:
                        pass

            except Exception as e:
                self._log("debug", f"监控循环异常: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
