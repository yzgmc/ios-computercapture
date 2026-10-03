# iOS 端 PhoneCam 应用

## 环境要求

- Xcode 15+
- iOS 15+（部署目标 15.0，因此不能用 iOS 17.4+ 才有的 VideoToolbox API）
- Swift 5.9+
- 依赖：AVFoundation + Network.framework + SRTKit（libsrt v1.5.4 xcframework）

## 创建 Xcode 项目

1. 在 `ios/` 目录下运行 `xcodegen generate` 生成 `PhoneCam.xcodeproj`
2. 在 **Signing & Capabilities** 中选择你的 Team
3. 编译并运行到 iPhone 设备

> Info.plist 已声明 `NSCameraUsageDescription` 与 `NSMicrophoneUsageDescription`，
> 无需额外配置权限。

## 三种传输模式

| 模式 | App 内选项 | 说明 |
|------|-----------|------|
| 局域网 | 局域网 | App 主动连电脑 IP 的 TCP 5000，音频 UDP 5001 |
| USB 直连 | USB 直连 | App 监听本机端口，电脑经 usbmuxd 转发连入 |
| SRT 推流 | SRT 推流 | App 主动连电脑的 SRT listener，适合公网/弱网 |

**USB 模式的端口与其他模式不同**：视频 TCP 5000、音频 **TCP 5002**。
因为 usbmuxd 只转发 TCP，无法转发 UDP，所以 USB 模式下音频不能走 5001。

## 使用说明

1. 确保 iPhone 和电脑在同一 Wi-Fi（USB 模式则插上数据线并解锁手机）
2. 启动电脑端应用
3. App 内填电脑 IP（点天线图标可自动发现，USB 模式无需填写）
4. 选择编码方式、画质、分辨率与帧率
5. 点「开始共享」

## 编码方式

| 编码 | 带宽参考 | 说明 |
|------|---------|------|
| H.264 硬件编码（推荐） | 1080p60 ≈ 8–12 Mbps | `VTCompressionSession`，支持低/中/高三档画质 |
| JPEG 85 | 1080p30 ≈ 15–20 Mbps | ImageIO 编码，兼容性兜底 |
| BGRA 无压缩 | 1080p60 ≈ 500 Mbps | 仅千兆有线或 USB3 可用 |

开启「自适应码率」后，App 会根据发送背压与设备热状态自动调整 H.264 码率。

## 文件说明

| 文件 | 作用 |
|------|------|
| `CaptureManager.swift` | AVCaptureSession 采集、分辨率/帧率协商、自适应码率 |
| `H264Encoder.swift` | VTCompressionSession 封装，AVCC → Annex-B 转换 |
| `RawStreamServer.swift` | TCP 发送（LAN caller / USB listener） |
| `SRTStreamServer.swift` | SRT caller 发送 |
| `AudioStreamServer.swift` | 音频发送（UDP；USB 模式走 TCP 5002） |
| `VideoStreamTransport.swift` | 传输抽象协议，使 TCP / SRT 可运行期切换 |
| `ContentView.swift` | SwiftUI 界面 |
| `DiscoveryClient.swift` | UDP 50000 广播，自动发现桌面端 |
