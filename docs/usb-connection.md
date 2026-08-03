# USB 连接支持（基于 Tethering 网络）

## 背景与约束

iOS 在未越狱状态下，**无法在 USB 上启动自定义服务**。第三方 App 没有权限直接
通过 USB 通道与桌面端通信。因此本项目 USB 连接采用 **tethering 网络方案**：
利用 iPhone 的「个人热点」USB 共享功能，让 iPhone 与电脑之间建立一条 172.20.10.x
网段的点对点 IP 链路，所有媒体流仍走标准 TCP/UDP 协议。

> 实现位置：
> - 桌面端检测：`desktop/src/usb/`（`device_detector.py`、`tethering_discovery.py`）
> - 依赖：`pyusb>=1.2.1`（见 `desktop/requirements.txt`）

## 工作原理

```
[iPhone App]
   │  主动连接 桌面IP:5000 (TCP 视频) + 桌面IP:5001 (UDP 音频)
   │
   │  USB 线缆 + 个人热点 (USB 模式)
   ▼
[172.20.10.x 网段]
   │  iPhone 通常为 172.20.10.1（热点网关）
   │  电脑由 DHCP 获得 172.20.10.x
   ▼
[桌面端]
   │  监听 0.0.0.0:5000 (TCP 视频) + 0.0.0.0:5001 (UDP 音频)
   │  device_detector: 确认 Apple USB 设备已接入
   │  tethering_discovery: 扫描 172.20.10.0/24 找到桌面端
```

## 使用流程

### 1. iPhone 端开启个人热点

设置 → 个人热点 → 打开「允许其他人加入」→「最大兼容性」（强制 USB 模式）。

### 2. USB 连接电脑

用数据线连接 iPhone 与电脑，首次连接时 iPhone 会弹出「信任此电脑」，选择信任。
电脑会自动识别为网络适配器并获得 172.20.10.x 地址（macOS/Windows 自带驱动）。

### 3. 桌面端启动

桌面端启动后自动监听 `0.0.0.0:5000`（TCP 视频）和 `0.0.0.0:5001`（UDP 音频）。
状态栏会显示本机在 tethering 网段的地址（如 `172.20.10.2`）。

### 4. iOS 端配置

在 iOS App 中将桌面端 IP 填为电脑在 tethering 网段的地址（如 `172.20.10.2`），
TCP 端口 `5000`、UDP 端口 `5001`，点击「开始共享」即可走 USB tethering 链路。

## 模块说明

### `usb/device_detector.py`

```python
from usb import UsbDeviceDetector

detector = UsbDeviceDetector()
if detector.available:
    devices = detector.list_apple_devices()
    # [AppleUsbDevice(vid=0x05AC pid=0x12A8 name='iPhone' serial='...')]
```

- pyusb 未安装或后端缺失时 `available=False`，`list_apple_devices()` 返回空列表
- 仅做存在性检测，无法直接取得 iOS UDID（需 libimobiledevice）
- Windows 需安装 iTunes / Apple Mobile Device Support 以提供 USB 驱动

### `usb/tethering_discovery.py`

```python
import asyncio
from usb import TetheringDiscovery

async def find():
    discovery = TetheringDiscovery()  # 默认 172.20.10.0/24:8080
    endpoint = await discovery.discover_first()
    if endpoint:
        print(endpoint.ws_url())  # ws://172.20.10.1:8080
```

- `discover()`：全量并发扫描网段，返回按延迟排序的可达端点列表
- `discover_first()`：优先探测网关 172.20.10.1，未命中再全量扫描
- `is_tethering_active()`：本机是否已获得 tethering 网段地址
- 并发数受 `MAX_CONCURRENT_PROBES`（32）限制，单次探测超时 0.5s

## 与其他方案的对比

| 方案 | 可行性 | 说明 |
|------|--------|------|
| **USB tethering（本项目）** | ✅ 无需越狱 | 走标准 IP 协议，复用现有媒体栈 |
| usbmuxd / libimobiledevice | ⚠️ 需配合 | 可做端口转发（iproxy），但 iOS App 仍需监听端口 |
| pymobiledevice3 | ⚠️ 需配合 | 纯 Python usbmuxd 客户端，本质同上 |
| Network Extension | ❌ 需特权 | 苹果特殊权限，普通开发者无法申请 |
| 自定义 USB 服务 | ❌ 需越狱 | 未越狱 iOS 不允许 |

> usbmuxd 端口转发（`iproxy 5000 5000`）可在未开启个人热点时使用，但需要额外
> 安装 libimobiledevice 工具链。本项目优先采用零依赖的 tethering 方案。

## 依赖

- 桌面端：`pyusb>=1.2.1`（仅用于检测，未安装时优雅降级）
- Windows：安装 iTunes 或独立的 Apple Mobile Device Support（提供 USB 驱动）
- macOS：系统自带 Apple USB 驱动
- Linux：安装 `usbmuxd` 与 `libimobiledevice`

## 限制

- tethering 网段地址可能随热点重启变化，发现结果不应长期缓存
- 部分运营商合约机可能禁用个人热点的 USB 共享
- 原画质 TCP 传输码率高（720p≈265Mbps），USB tethering 链路带宽充足可承载
