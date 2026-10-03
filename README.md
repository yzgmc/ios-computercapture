# iPhone Camera & Microphone Sharing

把 iPhone（或任意带浏览器的设备）的摄像头和麦克风共享给 Windows/macOS/Linux 电脑，
使其他电脑应用（Zoom、OBS、Teams）把它识别为本地摄像头和麦克风。

## 四种传输方式

| 模式 | 发送端 | 桌面端 | 适用场景 |
|------|--------|--------|----------|
| **局域网 LAN** | iOS App · TCP caller | TCP listener `:5000` | 同一 Wi-Fi，最常用 |
| **USB 直连** | iOS App · TCP listener | TCP client 经 usbmuxd 桥接 | 零配置、低延迟、不依赖 Wi-Fi |
| **SRT 推流** | iOS App · SRT caller | SRT listener `:5000` | 公网 / 不稳定网络 |
| **网页 HTTPS** | 浏览器 getUserMedia | HTTPS + WebSocket `:8443` | 免安装，iPhone/Android/电脑都能用 |

> 注意方向差异：LAN 与 SRT 模式下桌面端是监听方，发送端主动连入；
> **USB 模式相反** —— iOS 端监听，桌面端通过 usbmuxd 端口转发连进去。

## 功能特性

- **三种视频编码**（iOS 端与网页端一致）：
  - **H.264 硬件编码**（推荐）：VideoToolbox / WebCodecs，1080p60 约 8–12 Mbps
  - **JPEG**：软件抓帧，中等带宽，兼容性兜底
  - **BGRA 无压缩**：1080p60 约 500 MB/s，仅千兆有线 / USB3 可用
- **无压缩音频**：48kHz / mono / 16-bit PCM
- **虚拟设备输出**：视频 → OBS Virtual Camera / Unity Capture；音频 → VB-Cable / BlackHole
- **自适应码率**：根据发送背压与设备热状态动态调整，避免卡顿
- **断线自愈**：重连后强制 IDR 关键帧并重置解码器，避免花屏

## 项目结构

```
ios-computercapture/
├── desktop/                # Python + PyQt6 电脑端
│   ├── src/
│   │   ├── main.py         # 入口（Qt + asyncio 事件循环）
│   │   ├── app.py          # 主控制器：四种模式切换、帧分发
│   │   ├── ui/             # PyQt6 主界面
│   │   ├── raw_stream/     # TCP 接收 / SRT 接收 / H.264 解码 / 帧协议
│   │   ├── audio_stream/   # UDP 与 TCP 音频接收 + PyAudio 播放
│   │   ├── capture/        # 虚拟摄像头输出 + 虚拟音频设备查找
│   │   ├── web/            # 网页模式：自签证书 + HTTPS + WebSocket
│   │   │   └── static/     # index.html / app.js / audio-worklet.js
│   │   ├── discovery/      # 局域网自动发现（UDP 50000）
│   │   └── usb/            # usbmuxd 设备检测与端口桥接
│   ├── requirements.txt
│   └── build.py            # PyInstaller 打包
├── ios/                    # Swift iOS 应用
│   └── PhoneCam/
│       ├── CaptureManager.swift      # AVCaptureSession 采集 + 自适应码率
│       ├── H264Encoder.swift         # VTCompressionSession 硬件编码
│       ├── RawStreamServer.swift     # TCP 发送（LAN caller / USB listener）
│       ├── SRTStreamServer.swift     # SRT 发送（caller）
│       ├── AudioStreamServer.swift   # 音频发送（UDP / USB 走 TCP）
│       └── ContentView.swift         # SwiftUI 界面
├── docs/                   # 文档
└── tests/                  # 测试
```

## 快速开始

### 方式一：网页（免安装，推荐先试）

```bash
cd desktop
pip install -r requirements.txt
python src/main.py
```

1. 桌面端左上角模式切到 **网页 (HTTPS)**
2. 界面会显示 `https://192.168.x.x:8443`
3. 手机浏览器打开该地址 —— 首次会提示证书不受信任，选择「继续访问」
4. 点「开始共享」，授权摄像头与麦克风

详见 [docs/web-mode.md](docs/web-mode.md)。

### 方式二：iOS App（LAN）

1. 桌面端保持默认的 **局域网 (LAN)** 模式，监听 TCP 5000 / UDP 5001
2. Xcode 打开 `ios/PhoneCam.xcodeproj`（或用 `xcodegen generate` 重新生成）
3. Signing & Capabilities 选择你的 Team，编译运行到 iPhone
4. App 内填电脑 IP（或点天线图标自动发现），选编码与分辨率，点「开始共享」

> Windows 防火墙需放行 TCP 5000 + UDP 5001 入站。

### 方式三：iOS App（USB 直连）

1. 桌面端切到 **USB 直连**，需要 `pip install pymobiledevice3`
2. iPhone 用数据线接上电脑并解锁
3. App 内传输模式选「USB 直连」，点「开始共享」

桌面端会自动建立两条 usbmuxd 转发：视频 TCP 5000、音频 TCP 5002。
（usbmuxd 只转发 TCP，所以 USB 模式下音频不能走 UDP 5001。）

### 输出虚拟设备

连上画面后，在桌面端点「启动虚拟摄像头」「启动虚拟麦克风」，
即可在 Zoom / OBS / Teams 里选择 OBS Virtual Camera 与 VB-Cable。

## 端口一览

| 端口 | 协议 | 用途 |
|------|------|------|
| 5000 | TCP | 视频（LAN listener / USB 桥接）；SRT 也复用此端口 |
| 5001 | UDP | 音频（LAN / SRT） |
| 5002 | TCP | 音频（USB 桥接专用，usbmuxd 不支持 UDP） |
| 50000 | UDP | 局域网自动发现（iOS App 搜索桌面端） |
| 8443 | HTTPS | 网页模式：页面 + `/ws/video` + `/ws/audio` |

## 协议

视频与音频共用同一套自研帧头，四种传输方式完全一致，桌面端因此只需一套解码链路。

```
视频帧： magic "RAW1"(4) | frame_id(4) | width(4) | height(4)
        | format(4) | bytes_per_row(4) | payload_length(4)      # 28B 大端
        format: 0=BGRA / 10=JPEG / 20=H.264 Annex-B

音频包： magic "AUD1"(4) | seq(4) | sample_rate(4)
        | channels(1) | format(1) | payload_length(2)           # 16B 大端
```

- TCP：先读满帧头，再按 `payload_length` 精确读 payload
- UDP / WebSocket：一条消息即一帧（天然保持边界）

## 架构文档

- [docs/architecture.md](docs/architecture.md) — 整体架构与数据流
- [docs/web-mode.md](docs/web-mode.md) — 网页模式与自签证书
- [docs/raw-stream-protocol.md](docs/raw-stream-protocol.md) — 视频帧协议
- [docs/audio-stream-protocol.md](docs/audio-stream-protocol.md) — 音频包协议
- [docs/usb-direct-connection.md](docs/usb-direct-connection.md) — USB 直连
- [docs/virtual-device-setup.md](docs/virtual-device-setup.md) — 虚拟设备配置

## 打包桌面端

```bash
cd desktop
python build.py    # 输出 dist/PhoneCam/PhoneCam.exe
```

## 安全性

- 协议为明文、无鉴权，仅适用于可信局域网
- 网页模式使用自签证书，请核对桌面端显示的 SHA-256 指纹后再信任
- 需要在不可信网络使用时，请置于 VPN 或加密隧道内

## 许可证

MIT License
