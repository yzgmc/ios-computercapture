"""USB 直连桥接：基于 pymobiledevice3 的 usbmuxd 协议。

iOS App 监听 127.0.0.1:5000 (TCP 视频) + 127.0.0.1:5001 (UDP 音频)。
本模块在桌面端创建 usbmuxd 端口转发，把 PC 的 127.0.0.1:<port> 透明桥接到
iOS 设备的 127.0.0.1:<port>，无需 Wi-Fi，无需个人热点。

pymobiledevice3 10.x 接口（已验证）：
- pymobiledevice3.tcp_forwarder.UsbmuxTcpForwarder(serial, dst_port, src_port, listening_event=...)
- pymobiledevice3.usbmux.list_devices() -> async，返回 list[MuxDevice]
- MuxDevice 字段：devid, serial, connection_type（注意：是 serial 不是 udid）
"""
import asyncio
import logging
import socket
import sys
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)

# 默认端口
DEFAULT_TCP_PORT = 5000       # 视频（TCP）
DEFAULT_UDP_PORT = 5001       # 音频（UDP，仅 LAN / SRT）
DEFAULT_AUDIO_TCP_PORT = 5002  # 音频（TCP，仅 USB 直连；usbmuxd 不转发 UDP）

# pymobiledevice3 的接口可能因版本变化，按需 try/except
# 新版 (3.x+) 模块叫 usbmux（无 d），旧版叫 usbmuxd
# 新版 list_devices 是 async，旧版是 sync
try:
    from pymobiledevice3.tcp_forwarder import UsbmuxTcpForwarder as _Pm3TcpForwarder
    _PM3_FORWARDER_NEW = True  # UsbmuxTcpForwarder(serial, dst_port, src_port, listening_event=...)
except ImportError:
    try:
        from pymobiledevice3.tcp_forwarder import TcpForwarder as _Pm3TcpForwarder
        _PM3_FORWARDER_NEW = False  # TcpForwarder(udid, src_port, dst_port)
    except ImportError as e:
        logger.warning("pymobiledevice3 tcp_forwarder not available: %s", e)
        _Pm3TcpForwarder = None
        _PM3_FORWARDER_NEW = False

# usbmux 模块：新版叫 usbmux，旧版叫 usbmuxd
_PM3_USBMUX_ASYNC = False
try:
    from pymobiledevice3.usbmux import list_devices as _usbmux_list_devices  # 新版 async
    import inspect as _inspect
    _PM3_USBMUX_ASYNC = _inspect.iscoroutinefunction(_usbmux_list_devices)
except ImportError:
    try:
        from pymobiledevice3.usbmuxd import list_devices as _usbmux_list_devices  # 旧版 sync
        _PM3_USBMUX_ASYNC = False
    except ImportError as e:
        logger.warning("pymobiledevice3 usbmux not available: %s", e)
        _usbmux_list_devices = None

# usbmuxd 后端地址（Windows: iTunes/AMDS 的 TCP 27015；Linux/macOS: /var/run/usbmuxd）
_USBMUX_ADDRESS: Optional[str] = None
try:
    from pymobiledevice3.osu.os_utils import get_os_utils as _get_os_utils
    _ou = _get_os_utils()
    _usbmux_addr_raw = getattr(_ou, "usbmux_address", None)
    if isinstance(_usbmux_addr_raw, tuple) and _usbmux_addr_raw:
        # (('127.0.0.1', 27015), AF_INET) on Windows
        first = _usbmux_addr_raw[0]
        if isinstance(first, tuple) and len(first) >= 2:
            _USBMUX_ADDRESS = f"{first[0]}:{first[1]}"
        elif isinstance(first, str):
            _USBMUX_ADDRESS = first
    if _USBMUX_ADDRESS is None:
        # 兜底：Windows 默认 iTunes 端口
        from pymobiledevice3.usbmux import ITUNES_HOST as _ITUNES_HOST
        if _ITUNES_HOST:
            _USBMUX_ADDRESS = f"{_ITUNES_HOST[0]}:{_ITUNES_HOST[1]}"
except Exception as e:
    logger.debug("Failed to detect usbmux address: %s", e)

_PM3_AVAILABLE = _Pm3TcpForwarder is not None and _usbmux_list_devices is not None


def is_usb_available() -> bool:
    """pymobiledevice3 是否可用（包含 usbmuxd 后端模块）。"""
    return _PM3_AVAILABLE


def is_usbmuxd_reachable(timeout: float = 1.0) -> bool:
    """检测 usbmuxd 后端服务是否可达。

    Windows：检测 127.0.0.1:27015 (Apple Mobile Device Service 监听的 usbmuxd 端口)
    Linux/macOS：检测 /var/run/usbmuxd Unix socket（本函数仅做 TCP 探测，Unix socket 由 pymobiledevice3 自检）

    :return: True=后端可达（不代表有设备，仅代表服务在跑）
    """
    if not _PM3_AVAILABLE:
        return False
    # 仅 Windows 走 TCP 探测；其他平台信任 pymobiledevice3 的 create_mux
    if sys.platform != "win32":
        return True
    if not _USBMUX_ADDRESS or ":" not in _USBMUX_ADDRESS:
        return False
    host, _, port_str = _USBMUX_ADDRESS.partition(":")
    try:
        port = int(port_str)
    except ValueError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def get_windows_amds_status() -> Optional[dict]:
    """查询 Windows 上 Apple Mobile Device Service (AMDS) 状态。

    :return: None=非 Windows 或查询失败；dict={
        'name': str, 'status': str (Running/Stopped/...),
        'start_type': str (Automatic/Manual/Disabled)
    }
    """
    if sys.platform != "win32":
        return None
    try:
        import subprocess
        # sc query 比Get-Service 快且不依赖 PowerShell
        result = subprocess.run(
            ["sc", "query", "Apple Mobile Device Service"],
            capture_output=True, text=True, timeout=3.0,
        )
        info = {"name": "Apple Mobile Device Service", "status": "Unknown", "start_type": "Unknown"}
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("STATE"):
                # "STATE              : 4  RUNNING"
                parts = line.split(":", 1)
                if len(parts) == 2:
                    info["status"] = parts[1].strip().split()[-1].capitalize() if " " in parts[1].strip() else parts[1].strip().capitalize()
        # 查询启动类型
        result2 = subprocess.run(
            ["sc", "qc", "Apple Mobile Device Service"],
            capture_output=True, text=True, timeout=3.0,
        )
        for line in result2.stdout.splitlines():
            line = line.strip()
            if line.startswith("START_TYPE"):
                parts = line.split(":", 1)
                if len(parts) == 2:
                    raw = parts[1].strip()
                    # "2   AUTO_START" / "3   DEMAND_START" / "4   DISABLED"
                    if "AUTO" in raw:
                        info["start_type"] = "Automatic"
                    elif "DEMAND" in raw:
                        info["start_type"] = "Manual"
                    elif "DISABLED" in raw:
                        info["start_type"] = "Disabled"
                    else:
                        info["start_type"] = raw
        return info
    except Exception as e:
        logger.debug("get_windows_amds_status failed: %s", e)
        return None


async def diagnose_usb() -> list[dict]:
    """综合诊断 USB 连接环境，返回问题列表（空列表=一切正常）。

    每个问题：{'level': 'error'|'warn'|'info', 'check': str, 'message': str, 'hint': str}

    常见故障：
    1. pymobiledevice3 未安装 → 提示 pip install pymobiledevice3
    2. Windows AMDS 服务未运行 → 提示启动服务或安装 iTunes
    3. usbmuxd 后端不可达 → 提示 Apple Mobile Device Service 异常
    4. 无设备接入 → 提示插上 iPhone 并在 iPhone 上信任电脑
    """
    issues: list[dict] = []

    # 1. pymobiledevice3 模块检查
    if not _PM3_AVAILABLE:
        issues.append({
            "level": "error",
            "check": "pymobiledevice3",
            "message": "pymobiledevice3 模块未安装或导入失败",
            "hint": "运行: pip install pymobiledevice3",
        })
        return issues

    # 2. Windows AMDS 服务状态
    if sys.platform == "win32":
        amds = get_windows_amds_status()
        if amds is None:
            issues.append({
                "level": "warn",
                "check": "amds_service",
                "message": "无法查询 Apple Mobile Device Service 状态",
                "hint": "请确认已安装 iTunes 或 Apple Mobile Device Service",
            })
        elif amds.get("status", "").lower() != "running":
            issues.append({
                "level": "error",
                "check": "amds_service",
                "message": f"Apple Mobile Device Service 未运行 (状态: {amds.get('status')})",
                "hint": "运行 services.msc 启动 'Apple Mobile Device Service'，或重启 iTunes",
            })
        else:
            # AMDS 运行中，进一步检测端口
            if not is_usbmuxd_reachable(timeout=0.8):
                issues.append({
                    "level": "error",
                    "check": "usbmuxd_port",
                    "message": f"AMDS 服务运行中但 usbmuxd 端口 ({_USBMUX_ADDRESS}) 不可达",
                    "hint": "请重启 Apple Mobile Device Service 或重新插拔 iPhone",
                })

    # 3. 设备列表检查（实际探测 usbmuxd）
    try:
        devs = await list_ios_devices()
        if not devs:
            issues.append({
                "level": "warn",
                "check": "device_list",
                "message": "usbmuxd 可达但未检测到任何 iOS 设备",
                "hint": "请用数据线连接 iPhone，并在 iPhone 屏幕上信任此电脑",
            })
        else:
            issues.append({
                "level": "info",
                "check": "device_list",
                "message": f"检测到 {len(devs)} 台 iOS 设备: {[d.get('udid', '?')[:8] for d in devs]}",
                "hint": "",
            })
    except Exception as e:
        issues.append({
            "level": "error",
            "check": "device_list",
            "message": f"枚举 iOS 设备失败: {e}",
            "hint": "usbmuxd 后端异常，请重启 Apple Mobile Device Service",
        })

    return issues


async def list_ios_devices() -> list[dict]:
    """通过 usbmuxd 列出所有已 USB 接入的 iOS 设备。

    Returns:
        [{'udid': 'xxx', 'connection_type': 'USB', 'product_id': 4776, ...}, ...]

    注意：pymobiledevice3 10.x 的 MuxDevice 只有 serial/devid/connection_type 三个字段，
    没有 udid 属性（serial 即为 UDID）。
    """
    if not _PM3_AVAILABLE or _usbmux_list_devices is None:
        return []
    try:
        if _PM3_USBMUX_ASYNC:
            # 新版 pymobiledevice3 3.x+：list_devices 是 async
            devs = await _usbmux_list_devices()
        else:
            # 旧版：sync API，丢到 default executor
            loop = asyncio.get_running_loop()
            devs = await loop.run_in_executor(None, lambda: list(_usbmux_list_devices()))
        out = []
        for d in devs:
            try:
                # MuxDevice 字段：devid, serial, connection_type
                # serial 即 UDID（Apple 设备唯一标识），兼容旧代码字段名 udid
                serial = getattr(d, "serial", None)
                out.append({
                    "udid": serial,  # 兼容字段名
                    "serial": serial,
                    "connection_type": getattr(d, "connection_type", "USB"),
                    "devid": getattr(d, "devid", None),
                    "is_usb": getattr(d, "is_usb", True),
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
        self._forwarder = None  # 持有 TcpForwarderBase 实例，用于 stop()
        self._listening_event: Optional[asyncio.Event] = None  # forwarder 端口绑定就绪
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
        # TcpForwarderBase.stop() 是同步方法，设置 stopped event，
        # 让 start() 中的 await self.stopped.wait() 返回
        if self._forwarder is not None:
            try:
                self._forwarder.stop()
            except Exception:
                pass
            self._forwarder = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._set_state("stopped")

    async def _run(self):
        """实际转发循环。

        pymobiledevice3 10.x 的 UsbmuxTcpForwarder：
            forwarder = UsbmuxTcpForwarder(serial, dst_port, src_port, listening_event=...)
            await forwarder.start()  # 阻塞直到 forwarder.stop() 被调用
            forwarder.stop()         # 同步方法，设置 stopped event
        """
        self._set_state("running")

        try:
            # 新版 API：UsbmuxTcpForwarder(serial, dst_port, src_port, listening_event=...)
            # 旧版 API：TcpForwarder(udid, src_port, dst_port)
            # 注意参数顺序不同！
            listening_event = asyncio.Event()
            if _PM3_FORWARDER_NEW:
                forwarder = _Pm3TcpForwarder(
                    serial=self.udid,
                    dst_port=self.device_port,
                    src_port=self.host_port,
                    listening_event=listening_event,
                )
            else:
                forwarder = _Pm3TcpForwarder(
                    self.udid,
                    self.host_port,    # src_port
                    self.device_port,  # dst_port
                )

            self._forwarder = forwarder
            self._listening_event = listening_event
            self._set_state("running")
            # start() 内部 await self.stopped.wait()，会一直运行直到 stop() 被调用
            await forwarder.start()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("USB bridge forwarder error: %s", e)
            self._set_state("error")
            return

        if self.state not in ("error", "stopped"):
            self._set_state("stopped")

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        """等待 forwarder 真正在 PC 端绑定端口（listening_event 被设置）。"""
        if self._listening_event is None:
            return True  # 旧版 API 无 listening_event，直接返回 True
        try:
            await asyncio.wait_for(self._listening_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


class UsbBridgeManager:
    """统一管理多条桥接 + 设备插拔检测。

    - 自动检测 iOS 设备
    - 自动为每个设备建立 TCP/UDP 桥接
    - 设备断开时清理
    - 桥接就绪时触发回调（让上层连接 receiver）
    """

    def __init__(self,
                 tcp_port: int = DEFAULT_TCP_PORT,
                 udp_port: int = DEFAULT_UDP_PORT,
                 audio_tcp_port: int = DEFAULT_AUDIO_TCP_PORT,
                 on_state: Optional[Callable[[str, str], None]] = None,
                 on_devices_changed: Optional[Callable[[list], Awaitable[None]]] = None,
                 on_bridge_ready: Optional[Callable[[str], Awaitable[None]]] = None):
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        # USB 模式下音频改用 TCP 转发（usbmuxd 不支持 UDP 转发）
        self.audio_tcp_port = audio_tcp_port
        # on_state(level, message): level ∈ info/warn/error
        self.on_state = on_state or (lambda lvl, msg: None)
        # on_devices_changed(devices)
        self.on_devices_changed = on_devices_changed
        # on_bridge_ready(udid): TCP 桥接已就绪，上层可连接 receiver
        self.on_bridge_ready = on_bridge_ready

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

    def has_ready_bridge(self, udid: Optional[str] = None) -> bool:
        """检查指定 udid（或任意设备）的 TCP 桥接是否已就绪。"""
        if udid is None:
            return any("tcp" in b for b in self._bridges.values())
        return udid in self._bridges and "tcp" in self._bridges[udid]

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
        """为指定 udid 建立视频 TCP 桥接 + 音频 TCP 桥接，并等待端口就绪。

        音频必须走 TCP：usbmuxd 只转发 TCP，不转发 UDP。此前仅桥接视频，
        导致 USB 模式下麦克风采集到的数据永远到不了桌面端。

        视频桥接建立后等待 forwarder 真正监听 PC 端口，再通过 on_bridge_ready
        通知上层连接 receiver，避免 connect 时端口尚未就绪。
        """
        if udid not in self._bridges:
            self._bridges[udid] = {}

        if "tcp" not in self._bridges[udid]:
            self._log("info",
                      f"建立 USB 视频桥接 → {udid[:8]}... (TCP {self.tcp_port})")
            bridge = UsbBridge(
                udid, self.tcp_port, self.tcp_port,
                on_state_change=lambda s: self._log("info", f"[{udid[:8]}] 视频桥接: {s}"))
            try:
                await bridge.start()
                self._bridges[udid]["tcp"] = bridge
                self._current_udid = udid

                # 等待 forwarder 真正绑定 PC 端口
                ready = await bridge.wait_ready(timeout=5.0)
                if ready:
                    self._log("info",
                              f"USB 视频桥接就绪 (forwarder 监听 127.0.0.1:{self.tcp_port})")
                    # 通知上层：桥接已就绪，可以连接 receiver
                    if self.on_bridge_ready:
                        try:
                            await self.on_bridge_ready(udid)
                        except Exception as e:
                            self._log("debug", f"on_bridge_ready 回调异常: {e}")
                else:
                    self._log("warn", "USB 视频桥接启动超时（forwarder 未监听）")
            except Exception as e:
                self._log("error", f"USB 视频桥接失败: {e}")

        if self.audio_tcp_port and "audio" not in self._bridges[udid]:
            self._log("info",
                      f"建立 USB 音频桥接 → {udid[:8]}... (TCP {self.audio_tcp_port})")
            audio_bridge = UsbBridge(
                udid, self.audio_tcp_port, self.audio_tcp_port,
                on_state_change=lambda s: self._log("info", f"[{udid[:8]}] 音频桥接: {s}"))
            try:
                await audio_bridge.start()
                self._bridges[udid]["audio"] = audio_bridge
            except Exception as e:
                self._log("error", f"USB 音频桥接失败: {e}")

        if self.on_devices_changed:
            try:
                await self.on_devices_changed(self.get_active_devices())
            except Exception:
                pass

    async def _monitor_loop(self):
        """周期性检测设备插拔。"""
        while not self._stop.is_set():
            try:
                devs = await list_ios_devices()
                current = {d["udid"] for d in devs if d.get("udid")}

                # 自动为新设备建桥（仅当当前无活动桥接时）
                if not self._bridges and devs:
                    first = devs[0]
                    if first.get("udid"):
                        self._log("info", f"检测到设备接入: {first['udid'][:8]}...，自动建桥")
                        await self._ensure_bridges(first["udid"])

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

                # 新接入设备：自动建桥。此前只清理断开设备，导致"先开 USB 模式、
                # 后插线"或中途换设备时始终连不上，必须重启应用。
                for udid in sorted(current):
                    if udid in self._bridges:
                        continue
                    # 端口是固定的，同一时刻只为一台设备建桥
                    if self._current_udid is not None and self._current_udid != udid:
                        continue
                    self._log("info", f"检测到设备: {udid[:8]}...")
                    await self._ensure_bridges(udid)

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
