# USB 直连模式

## 背景

LAN 模式依赖 Wi-Fi，存在以下限制：
- 占用 Wi-Fi 带宽（影响其他设备）
- 距离受限（信号穿墙、距离）
- 延迟不稳定
- 公网/校园网环境无法使用

**USB 直连模式**通过苹果私有协议 **usbmuxd** 把 iOS App 监听的 `127.0.0.1:<port>` 透明桥接到 PC 端 `127.0.0.1:<port>`，完全走 USB 物理通道，**无需 Wi-Fi、无需个人热点**。

## 架构

```
┌────────────────────┐                ┌────────────────────┐
│  iOS App           │                │  Desktop App       │
│                    │                │                    │
│  RawStreamServer   │                │  RawStreamReceiver │
│  (127.0.0.1:5000)  │                │  (127.0.0.1:5000)  │
│         ▲          │                │         ▲          │
│         │          │   usbmuxd      │         │          │
│  AudioStreamServer │   (USB 通道)   │  AudioStreamReceiver│
│  (127.0.0.1:5001)  │ ─────────────► │  (127.0.0.1:5001)  │
│                    │                │                    │
└────────────────────┘                └────────────────────┘
        iPhone                               PC
            ╲                                ╱
             ╲        Lightning/USB-C      ╱
              ╲════════════════════════════╱
                       物理 USB 线
```

**关键点**：
- iOS App 与桌面端**都绑定 127.0.0.1**
- pymobiledevice3 的 `TcpForwarder` 在两端建立 usbmuxd 端口转发
- 桌面端接收器重启绑定 `127.0.0.1`，避免 LAN 上的随机连接
- **无需在桌面端配置任何网络**，无需 IP/端口

## 平台要求

### Windows
- 安装 [iTunes](https://www.apple.com/itunes/) 或独立的 [Apple Mobile Device Support](https://support.apple.com/en-us/HT204095) (提供 usbmuxd 后端驱动)
- `pymobiledevice3>=3.5.0`（纯 Python usbmuxd 客户端）
- ⚠ Windows 防火墙首次可能需要允许 `python.exe` 监听 127.0.0.1:5000/5001

### macOS
- 系统自带 usbmuxd
- `pymobiledevice3>=3.5.0`
- 注意 macOS 上 PyQt 应用需要授予"辅助功能"等权限

### Linux
```bash
sudo apt install usbmuxd libimobiledevice6 libimobiledevice-utils
sudo usbmuxd -v
pip install pymobiledevice3
```
- udev 规则（推荐）：`/etc/udev/rules.d/85-idevice.rules`：
  ```
  SUBSYSTEM=="usb", ATTR{idVendor}=="05ac", ATTR{idProduct}=="12[0-9a-f]{2}", MODE="0666"
  ```
  避免 `pymobiledevice3` 因权限不足看不到设备

## 使用流程

### 1. iPhone 端
- 设置 → 隐私 → 本地网络 → **允许 PhoneCam**（首次需授权）
- 启动 App，选择 **USB 直连**模式
- 用 USB 数据线连接 iPhone 与电脑
- iPhone 会弹出"信任此电脑"，选择信任并输入解锁密码
- App 自动通过 usbmuxd 桥接连接到 127.0.0.1:5000
- 点击「开始共享」

### 2. 桌面端
- 启动桌面端
- 顶部 **连接方式** 下拉框 → 选择 **USB 直连 (USB)**
- 自动桥接到 USB 接入的 iOS 设备
- 状态栏显示：`✓ 已连接: xxxxxxxx...`（绿色）
- 视频预览区立即显示 iPhone 摄像头画面

## 异常处理

| 现象 | 原因 | 解决 |
|------|------|------|
| 模式切换后状态显示"等待 iPhone USB 连接..." | 未插入 iPhone 或未授权 | 插入 USB 线，iPhone 弹窗"信任此电脑" |
| `pymobiledevice3 import failed` | 未安装 | `pip install pymobiledevice3` |
| 状态显示"桥接中: xxxx" 长时间未变 | usbmuxd 后端异常 | Windows：重启 iTunes 服务；macOS：`sudo killall usbmuxd`；Linux：`sudo systemctl restart usbmuxd` |
| 设备已插入但状态一直"未启用" | 模式未切到 USB | 桌面端顶部下拉框选择 USB 直连 |
| 频繁出现"设备断开 → 设备就绪" | USB 线松动 | 换线/重插 |
| iOS App 切到 USB 模式后超时 | iPhone 未授予本地网络权限 | 设置 → PhoneCam → 启用本地网络 |

## 与 LAN 模式的差异

| 特性 | LAN | USB |
|------|-----|-----|
| 依赖 | 同一 Wi-Fi | 仅 USB 数据线 |
| 带宽 | Wi-Fi 实测（典型 100-500 Mbps） | USB 2.0 = 480 Mbps / USB 3.0 = 5 Gbps |
| 延迟 | 5-50ms（看信号） | < 2ms |
| 公网/校园网 | 通常不可用 | 可用 |
| 多设备 | 可同时多台 | 同时仅 1 台（usbmuxd 单连接） |
| 配置复杂度 | 需输入 IP/端口 | 零配置，插入即用 |

## API 参考

### `usb.is_usb_available() -> bool`
检测 pymobiledevice3 + usbmuxd 后端是否可用。

### `usb.list_ios_devices() -> list[dict]`
列出所有已 USB 接入的 iOS 设备。
```python
devs = await list_ios_devices()
# [{'udid': '00008101-...', 'connection_type': 'USB', 'product_id': 4776}, ...]
```

### `usb.UsbBridge(udid, host_port, device_port, on_state_change=None)`
单条端口转发。
- `udid`: iOS 设备 UDID
- `host_port`: PC 端监听端口
- `device_port`: iOS 端目标端口

### `usb.UsbBridgeManager(tcp_port, udp_port, on_state=None, on_devices_changed=None)`
统一管理多设备 + 自动重连。

## 安全考虑

- 设备间通信通过苹果私有 usbmuxd 协议，**仅当 iPhone 用户在设备上点击"信任"后才允许连接**
- 桌面端需要本地权限，无远程访问风险
- 桥接端口仅在 127.0.0.1 监听，不暴露在 LAN

## 限制

- 同一时间仅支持一台 iPhone 设备（usbmuxd 单连接）
- iPhone 上必须授权本地网络权限（iOS 14+）
- Windows 平台需要安装 iTunes 或 Apple Mobile Device Support
