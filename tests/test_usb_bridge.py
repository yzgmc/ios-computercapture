"""USB 直连模式测试。"""
import asyncio
import sys
import os
from pathlib import Path

# 让测试可以直接 import desktop.src.usb
_DESKTOP_SRC = Path(__file__).resolve().parent.parent / "desktop" / "src"
if str(_DESKTOP_SRC) not in sys.path:
    sys.path.insert(0, str(_DESKTOP_SRC))

import pytest

from usb.bridge import (
    is_usb_available,
    list_ios_devices,
    UsbBridge,
    UsbBridgeManager,
    DEFAULT_TCP_PORT,
    DEFAULT_UDP_PORT,
)


def test_is_usb_available_returns_bool():
    """is_usb_available 返回 bool，pymobiledevice3 缺失时为 False。"""
    result = is_usb_available()
    assert isinstance(result, bool)


def test_constants():
    assert DEFAULT_TCP_PORT == 5000
    assert DEFAULT_UDP_PORT == 5001


def test_list_ios_devices_returns_list():
    """list_ios_devices 返回 list（无设备或 pymobiledevice3 缺失时为 []）。"""
    result = asyncio.run(list_ios_devices())
    assert isinstance(result, list)


def test_usb_bridge_lifecycle():
    """UsbBridge 构造/状态字段正常。"""
    if not is_usb_available():
        pytest.skip("pymobiledevice3 not available")
    bridge = UsbBridge(udid="FAKE-UDID", host_port=5000, device_port=5000)
    assert bridge.state == "idle"
    assert bridge.udid == "FAKE-UDID"
    assert bridge.host_port == 5000


def test_usb_bridge_manager_lifecycle():
    """UsbBridgeManager 构造/字段正常。"""
    mgr = UsbBridgeManager()
    assert mgr.tcp_port == 5000
    assert mgr.udp_port == 5001
    assert mgr.get_active_devices() == []


@pytest.mark.asyncio
async def test_usb_bridge_manager_start_without_pm3():
    """pymobiledevice3 缺失时，start 优雅降级，不抛异常。"""
    if is_usb_available():
        pytest.skip("pymobiledevice3 available, skip degradation test")
    msgs = []
    mgr = UsbBridgeManager(
        on_state=lambda lvl, msg: msgs.append((lvl, msg)),
    )
    await mgr.start()  # 不应抛异常
    assert any("pymobiledevice3" in m for _, m in msgs)


@pytest.mark.asyncio
async def test_usb_bridge_manager_stop_is_idempotent():
    """stop 重复调用安全。"""
    mgr = UsbBridgeManager()
    await mgr.stop()
    await mgr.stop()  # 第二次不应报错
