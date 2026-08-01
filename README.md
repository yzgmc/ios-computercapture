# iPhone Camera & Microphone Sharing

将 iPhone 的摄像头和麦克风通过 Wi-Fi 或 USB 共享给 Windows/macOS 电脑使用，使其他电脑应用（如 Zoom、OBS、Teams）能够识别 iPhone 作为本地摄像头和麦克风。

## 功能特性

- **实时音视频传输**: iPhone 捕获摄像头和麦克风，通过 WebRTC 低延迟传输到电脑
- **虚拟摄像头输出**: 电脑端将视频输出到 OBS Virtual Camera / UnityCapture
- **虚拟麦克风输出**: 电脑端将音频输出到 VB-Cable / BlackHole 等虚拟音频设备
- **设备自动发现**: 通过 mDNS/Bonjour 自动发现局域网内的电脑
- **参数实时调节**: 分辨率、帧率、音量可通过桌面 UI 调整
- **跨平台支持**: 电脑端支持 Windows 和 macOS

## 项目结构

```
ios-computercapture/
├── desktop/          # Python + PyQt6 电脑端应用
│   ├── src/
│   │   ├── main.py               # 程序入口
│   │   ├── app.py                # 应用主控制器
│   │   ├── ui/main_window.py     # PyQt6 主界面
│   │   ├── webrtc/               # WebRTC 接收与信令
│   │   ├── capture/              # 虚拟摄像头/麦克风输出
│   │   └── discovery/            # mDNS 设备发现
│   ├── requirements.txt
│   └── build.py                  # PyInstaller 打包脚本
├── ios/              # Swift iOS 应用
│   └── PhoneCam/
├── signaling/        # Python WebRTC 信令服务器
│   ├── server.py
│   └── requirements.txt
└── docs/             # 文档
    ├── architecture.md
    └── virtual-device-setup.md
```

## 技术栈

- **iOS 端**: Swift + AVFoundation + WebRTC
- **电脑端**: Python + PyQt6 + aiortc + OpenCV + pyvirtualcam
- **信令**: Python + aiohttp WebSocket
- **虚拟设备**: OBS Virtual Camera / VB-Cable / BlackHole

## 快速开始

### 1. 启动信令服务器

```bash
cd signaling
pip install -r requirements.txt
python server.py
```

信令服务器默认监听 `0.0.0.0:8080`。

### 2. 启动电脑端应用

```bash
cd desktop
pip install -r requirements.txt
python src/main.py
```

### 3. 运行 iOS 应用

1. 使用 Xcode 创建新的 iOS App 项目，命名为 **PhoneCam**
2. 将 `ios/PhoneCam/*.swift` 和 `Info.plist` 复制到项目中
3. 通过 CocoaPods 或 SPM 添加 WebRTC 依赖
4. 在 **Signing & Capabilities** 中选择你的 Team
5. 编译并运行到 iPhone 设备

### 4. 连接使用

1. 确保 iPhone 和电脑在同一 Wi-Fi 网络
2. 在 iPhone 应用中输入电脑 IP 地址（如 `ws://192.168.1.100:8080`）和房间 ID
3. 点击「开始共享」
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

详见 [docs/architecture.md](docs/architecture.md)。

## 开发阶段

1. **MVP** ✅: iOS 捕获音视频 → WebRTC 传输 → 电脑端显示预览
2. **虚拟设备集成** ✅: 将接收到的流输出到虚拟摄像头 / 虚拟音频线
3. **USB 连接**: 通过 usbmuxd 实现 USB 直连（待实现）
4. **优化**: UI 美化、参数调节、安全性、低延迟优化（持续）

## 许可证

MIT License
