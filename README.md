# iPhone Camera & Microphone Sharing

将 iPhone 的摄像头和麦克风通过 **Wi-Fi 或 USB** 共享给 Windows/macOS/Linux 电脑使用，
使其他电脑应用（如 Zoom、OBS、Teams）能够识别 iPhone 作为本地摄像头和麦克风。

## 功能特性

- **两种传输模式**:
  - **LAN 模式**: iPhone 通过 Wi-Fi 连接，自动发现桌面端 IP
  - **USB 直连模式**: 通过 usbmuxd 协议走 USB 物理通道，零配置、低延迟
- **原画质视频传输**: iPhone 捕获 BGRA 无压缩像素帧，通过 TCP 整帧传输到电脑
- **无压缩音频传输**: iPhone 捕获 PCM 16-bit 麦克风数据，通过 UDP 实时传输
- **虚拟摄像头输出**: 电脑端将视频输出到 OBS Virtual Camera / UnityCapture
- **虚拟麦克风输出**: 电脑端将音频输出到 VB-Cable / BlackHole 等虚拟音频设备
- **跨平台支持**: 电脑端支持 Windows / macOS / Linux

## 项目结构

```
ios-computercapture/
├── desktop/          # Python + PyQt6 电脑端应用
│   ├── src/
│   │   ├── main.py               # 程序入口
│   │   ├── app.py                # 应用主控制器（含模式切换）
│   │   ├── ui/main_window.py     # PyQt6 主界面（LAN/USB 模式切换 UI）
│   │   ├── raw_stream/           # TCP 视频接收（28B 帧头 + BGRA payload）
│   │   ├── audio_stream/         # UDP 音频接收 + PyAudio 播放
│   │   ├── capture/              # 虚拟摄像头输出 + 虚拟音频设备查找
│   │   ├── discovery/            # LAN 自动发现（UDP 广播）
│   │   └── usb/                  # USB 设备检测 + usbmuxd 桥接（直连）
│   ├── requirements.txt
│   └── build.py                  # PyInstaller 打包脚本
├── ios/              # Swift iOS 应用
│   ├── PhoneCam/
│   │   ├── CaptureManager.swift  # AVCaptureSession 视频+音频采集
│   │   ├── RawStreamServer.swift # TCP 视频发送
│   │   ├── AudioStreamServer.swift # UDP 音频发送
│   │   ├── ContentView.swift     # SwiftUI 主界面
│   │   ├── Info.plist
│   │   └── PhoneCamApp.swift
│   └── project.yml               # xcodegen 配置
├── docs/             # 文档
│   ├── architecture.md
│   ├── raw-stream-protocol.md
│   ├── audio-stream-protocol.md
│   └── virtual-device-setup.md
└── tests/            # 测试
```

## 技术栈

- **iOS 端**: Swift + AVFoundation + Network.framework
- **电脑端**: Python + PyQt6 + asyncio + OpenCV + pyvirtualcam + PyAudio
- **视频传输**: TCP 流式（28B 帧头 + BGRA payload）
- **音频传输**: UDP（16B 帧头 + PCM16 payload）
- **虚拟设备**: OBS Virtual Camera / VB-Cable / BlackHole

## 快速开始

### 1. 启动电脑端应用

```bash
cd desktop
pip install -r requirements.txt
python src/main.py
```

应用启动后会自动监听：
- TCP `0.0.0.0:5000` — 接收视频
- UDP `0.0.0.0:5001` — 接收音频

状态栏会显示本机局域网 IP，供 iPhone 端填写。

> Windows 防火墙需放行 TCP 5000 + UDP 5001 入站。

### 2. 运行 iOS 应用

1. 使用 Xcode 打开 `ios/PhoneCam.xcodeproj`（或用 `xcodegen generate` 重新生成）
2. 在 **Signing & Capabilities** 中选择你的 Team
3. 编译并运行到 iPhone 设备

> 项目无外部 CocoaPods 依赖，无需 `pod install`。

### 3. 连接使用

1. 确保 iPhone 和电脑在同一 Wi-Fi 网络
2. 在 iPhone 应用中填写电脑 IP（如 `192.168.1.100`）、TCP 端口 `5000`、UDP 端口 `5001`
3. 在 iPhone 上选择分辨率与帧率，点击「开始共享」
4. 电脑端将显示 iPhone 摄像头画面
5. 点击「启动虚拟摄像头」和「启动虚拟麦克风」，即可在其他应用中使用

## 打包桌面端

```bash
cd desktop
python build.py
```

打包完成后，可执行文件位于 `dist/PhoneCam/PhoneCam.exe`。

## 虚拟设备配置

详见 [docs/virtual-device-setup.md](docs/virtual-device-setup.md)。

## 架构设计

- [docs/architecture.md](docs/architecture.md) — 整体架构与数据流
- [docs/raw-stream-protocol.md](docs/raw-stream-protocol.md) — TCP 视频协议
- [docs/audio-stream-protocol.md](docs/audio-stream-protocol.md) — UDP 音频协议

## 许可证

MIT License
