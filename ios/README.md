# iOS 端 PhoneCam 应用

## 环境要求

- Xcode 15+
- iOS 15+
- Swift 5.9+
- WebRTC 框架（建议通过 CocoaPods 或 Swift Package Manager 引入）

## 创建 Xcode 项目

1. 打开 Xcode，选择 **Create New Project**
2. 选择 **iOS → App**，名称填写 **PhoneCam**
3. 将本目录下的 `PhoneCam/*.swift` 和 `Info.plist` 复制到项目中
4. 添加 WebRTC 依赖：
   - 通过 CocoaPods：
     ```ruby
     pod 'GoogleWebRTC'
     ```
   - 或通过 Swift Package Manager 添加 `webrtc-sdk-ios`
5. 在 **Signing & Capabilities** 中选择你的 Team
6. 编译并运行到 iPhone 设备

## 使用说明

1. 确保 iPhone 和电脑在同一 Wi-Fi 网络
2. 启动电脑端信令服务器
3. 在 iPhone 应用中输入电脑 IP 和房间 ID
4. 点击「开始共享」
