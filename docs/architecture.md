# PhoneCam 架构设计

## 系统概览

```
┌─────────────────┐   TCP 5000 (BGRA 无压缩视频)   ┌──────────────────────┐
│   iPhone 设备    │ ────────────────────────────► │     电脑端应用        │
│  (PhoneCam App) │   UDP 5001 (PCM 无压缩音频)   │ (Python + PyQt6)     │
└─────────────────┘ ────────────────────────────► └──────────────────────┘
        │                                            │
        │ 捕获视频 (AVCaptureSession · BGRA)          │ 预览渲染 (Qt)
        │ 捕获音频 (AVCaptureSession · PCM16)         │ 虚拟摄像头 (pyvirtualcam)
        │                                            │ 虚拟麦克风 (PyAudio)
        ▼                                            ▼
┌─────────────────┐                          ┌──────────────────────┐
│   局域网 Wi-Fi   │                          │  其他电脑应用         │
│  /有线网络       │                          │ Zoom / OBS / Teams   │
└─────────────────┘                          └──────────────────────┘
```

## 数据流

1. **iOS 端** 使用 `AVCaptureSession` 捕获：
   - 摄像头 → `AVCaptureVideoDataOutput`（BGRA 32-bit）
   - 麦克风 → `AVCaptureAudioDataOutput`（48kHz / mono / 16-bit PCM）
2. **视频** 通过 `RawStreamServer` 整帧写入 TCP 5000 端口（28B 帧头 + BGRA payload）
3. **音频** 通过 `AudioStreamServer` 切片写入 UDP 5001 端口（16B 帧头 + PCM payload）
4. **电脑端** 监听两个端口：
   - `RawStreamReceiver`（asyncio TCP server）按帧头精确读取整帧
   - `AudioStreamReceiver`（asyncio UDP endpoint）按数据报接收音频包
5. 视频帧通过 Qt 信号转发到主线程渲染，同时送 `VirtualCameraOutput` 输出到虚拟摄像头
6. 音频包送 `AudioPlayer`，由 PyAudio 回调式输出到虚拟音频设备（VB-Cable / BlackHole）

## 连接方式

### Wi-Fi 连接（默认）
- iPhone 和电脑在同一局域网
- 桌面端启动后监听 `0.0.0.0:5000`（TCP 视频）和 `0.0.0.0:5001`（UDP 音频）
- iPhone 在 UI 中填写桌面端 IP，点击「开始共享」即可建立连接
- Windows 防火墙需放行 TCP 5000 + UDP 5001 入站

### USB 连接（未来扩展）
- 通过 `usbmuxd` / `libimobiledevice` 建立 TCP over USB 隧道
- 桌面端在 localhost 上监听，iPhone 通过 USB 端口转发访问

## 安全性

- 当前协议为明文无压缩传输，**仅适用于可信局域网**
- 无鉴权 / 加密；如需在不可信网络使用，建议在 VPN 或加密隧道内运行
- UDP 音频不保证可靠性，丢包由桌面端播放器自然吸收

## 性能参考

### 视频带宽（TCP 净载荷）

| 分辨率   | 帧率 | 单帧大小 (BGRA) | 估算码率 |
|----------|------|-----------------|----------|
| 720p     | 30   | 1280×720×4 ≈ 3.5MB | ≈ 265 Mbps |
| 1080p    | 30   | 1920×1080×4 ≈ 7.9MB | ≈ 566 Mbps |
| 720p     | 60   | 3.5MB            | ≈ 530 Mbps |

> 仅在千兆及以上有线/802.11ac Wi-Fi 网络下可用。

### 音频带宽（UDP 净载荷）

| 配置 | 单包大小 | 估算码率 |
|------|----------|----------|
| 48kHz / mono / 16-bit | ~1456B / 15ms | ≈ 768 kbps |

## 低延迟优化

- 视频无编解码：采集到的 BGRA 像素直接整帧写入 TCP，由内核保证可靠性
- 音频无编解码：采集到的 PCM 直接切片写入 UDP，应用层不做重传
- 接收方读到完整一帧立即回调，不做帧缓冲对齐
- 队列满时丢最旧帧，保持实时性
