# iOS 端 PhoneCam 应用

## 环境要求

- Xcode 15+
- iOS 15+
- Swift 5.9+
- 无第三方依赖（仅使用 AVFoundation + Network.framework）

## 创建 Xcode 项目

1. 在 `ios/` 目录下运行 `xcodegen generate` 生成 `PhoneCam.xcodeproj`
2. 在 **Signing & Capabilities** 中选择你的 Team
3. 编译并运行到 iPhone 设备

> Info.plist 已声明 `NSCameraUsageDescription` 与 `NSMicrophoneUsageDescription`，
> 无需额外配置权限。

## 使用说明

1. 确保 iPhone 和电脑在同一 Wi-Fi 网络
2. 启动电脑端应用（自动监听 TCP 5000 + UDP 5001）
3. 在 iPhone 应用中输入电脑 IP、TCP 端口（默认 5000）、UDP 端口（默认 5001）
4. 选择分辨率与帧率
5. 点击「开始共享」
