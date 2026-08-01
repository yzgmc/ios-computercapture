# USB 连接支持（开发中）

USB 连接可以提供比 Wi-Fi 更稳定的传输和更低的延迟，且不受局域网环境影响。

## 实现思路

### 方案一：usbmuxd / libimobiledevice

`usbmuxd` 是苹果官方用于 USB 与 iOS 设备通信的守护进程。它可以将 iPhone 上的 TCP 连接通过 USB 线缆转发到电脑。

实现步骤：
1. 在电脑上安装 `libimobiledevice` 和 `usbmuxd`
2. 将 iPhone 上的某个端口（如 8080）通过 USB 转发到电脑的本地端口（如 127.0.0.1:8080）
3. 电脑端信令服务器监听 127.0.0.1:8080
4. iPhone 应用连接 127.0.0.1:8080，实际通过 USB 与电脑通信

命令示例：
```bash
# 列出连接的设备
idevice_id -l

# 将 iPhone 的 8080 端口转发到电脑的 8080 端口
iproxy 8080 8080
```

### 方案二：pymobiledevice3

`pymobiledevice3` 是一个纯 Python 的 usbmuxd 客户端，可以方便地实现端口转发。

```python
from pymobiledevice3.tcp_forwarder import TcpForwarder

forwarder = TcpForwarder(device_udid, src_port=8080, dst_port=8080)
forwarder.start()
```

### 方案三：iOS 网络扩展（Network Extension）

通过 iOS 的 Network Extension 创建本地 TUN 接口，实现更底层的网络隧道。这个方案更复杂，需要申请苹果的特殊权限。

## 集成计划

1. 在桌面端添加 USB 设备检测模块
2. 自动启动端口转发服务
3. iOS 应用优先尝试 USB 连接，失败时回退到 Wi-Fi
4. UI 上显示当前连接方式（USB / Wi-Fi）

## 依赖

- Windows: 安装 iTunes 或 Apple Mobile Device Support 以提供 usbmuxd 驱动
- macOS: 系统自带 usbmuxd
- Linux: 安装 `usbmuxd` 和 `libimobiledevice`
