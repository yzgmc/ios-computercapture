# PhoneCam 架构设计

## 系统概览

```
┌─────────────────┐      WebRTC (SRTP)       ┌──────────────────────┐
│   iPhone 设备    │ ◄──────────────────────► │     电脑端应用        │
│  (PhoneCam App) │    信令: WebSocket        │ (Python + PyQt6)     │
└─────────────────┘                          └──────────────────────┘
        │                                            │
        │ 捕获视频 (AVFoundation)                      │ 虚拟摄像头输出
        │ 捕获音频 (AVFoundation)                      │ 虚拟麦克风输出
        │ 编码 (VideoToolbox / WebRTC)                 │ 解码渲染 (aiortc)
        │                                            │
        ▼                                            ▼
┌─────────────────┐                          ┌──────────────────────┐
│   信令服务器      │                          │  其他电脑应用         │
│ (aiohttp + WS)  │                          │ Zoom / OBS / Teams   │
└─────────────────┘                          └──────────────────────┘
```

## 数据流

1. **iOS 端** 使用 `AVCaptureSession` 捕获摄像头和麦克风数据
2. 原始帧通过 WebRTC 的 `RTCPeerConnection` 进行编码和加密传输
3. **电脑端** 使用 `aiortc` 接收并解码音视频流
4. 视频帧显示在 PyQt6 预览窗口，同时通过 `pyvirtualcam` 输出到虚拟摄像头
5. 音频帧通过 `PyAudio` 输出到虚拟音频设备（VB-Cable / BlackHole）

## 连接方式

### Wi-Fi 连接
- iPhone 和电脑在同一局域网
- 通过信令服务器交换 SDP 和 ICE candidate
- WebRTC 尝试 P2P 连接，失败时通过 TURN 服务器中继

### USB 连接（未来扩展）
- 通过 `usbmuxd` / `libimobiledevice` 建立 TCP over USB 隧道
- 信令服务器运行在 localhost，iPhone 通过 USB 端口转发访问

## 安全性

- WebRTC 媒体流使用 SRTP 加密
- 信令通道建议启用 WSS (WebSocket Secure)
- 房间 ID 作为简单的访问隔离

## 低延迟优化

- 使用 H.264 硬件编码（iOS VideoToolbox）
- 启用 WebRTC 的 low-latency 模式
- 减少缓冲，使用 UDP 传输
- 合理设置视频分辨率和帧率
