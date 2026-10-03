# PhoneCam 架构设计

## 系统概览

```
┌──────────────────┐                                ┌──────────────────────┐
│    采集端          │      视频 (RAW1 28B 头)        │     电脑端应用        │
│  iOS App 或浏览器  │ ─────────────────────────────► │ (Python + PyQt6)     │
│                   │      音频 (AUD1 16B 头)        │                      │
└──────────────────┘ ─────────────────────────────► └──────────────────────┘
        │                                            │
        │ 视频：H.264 硬编 / JPEG / BGRA               │ 分帧 → 解码 → Qt 预览
        │ 音频：48kHz mono PCM16                      │ 虚拟摄像头 (pyvirtualcam)
        ▼                                            ▼ 虚拟麦克风 (PyAudio)
┌──────────────────┐                          ┌──────────────────────┐
│  传输通道          │                          │  其他电脑应用         │
│ LAN/USB/SRT/HTTPS │                          │ Zoom / OBS / Teams   │
└──────────────────┘                          └──────────────────────┘
```

## 数据流

1. **采集端** 采集视频与音频：
   - iOS：`AVCaptureSession` → BGRA 像素 + PCM 采样
   - 浏览器：`getUserMedia` → VideoFrame + AudioWorklet Float32
2. **视频编码**（三选一，运行期可切换）：
   - H.264：iOS 用 `VTCompressionSession`，浏览器用 WebCodecs `VideoEncoder`
   - JPEG：ImageIO / canvas `toBlob`，约 15–20 Mbps
   - BGRA：原始像素，仅千兆有线可用
3. **封装**：加 28B RAW1 头（音频加 16B AUD1 头），大端序
4. **传输**：LAN=TCP、USB=TCP over usbmuxd、SRT=SRT 消息、网页=WebSocket
5. **桌面端解析**：按帧头精确分帧 → H.264 用 PyAV 解码 / JPEG 用 PIL / BGRA 用 numpy
6. **输出**：Qt 信号转发主线程渲染，同时送虚拟摄像头与 PyAudio 虚拟麦克风

## 四种连接方式

### 1. 局域网（LAN）

- iOS App 作为 TCP caller 主动连接桌面端 `0.0.0.0:5000`
- 音频走 UDP `:5001`
- Windows 防火墙需放行 TCP 5000 + UDP 5001 入站
- 桌面端监听 UDP 50000 应答广播，iOS 端可自动发现电脑 IP

### 2. USB 直连

方向与其他模式相反：

```
桌面 TCP client → PC 127.0.0.1:5000 → usbmuxd → iOS 127.0.0.1:5000 → iOS NWListener
桌面 TCP client → PC 127.0.0.1:5002 → usbmuxd → iOS 127.0.0.1:5002 → iOS NWListener(音频)
```

- 由 `pymobiledevice3` 的 `UsbmuxTcpForwarder` 建立转发
- **usbmuxd 只转发 TCP**，因此 USB 模式音频必须走 TCP 5002，不能用 UDP 5001
- 设备插拔由 `UsbBridgeManager` 监控，自动建桥与清理；视频通道断开后 60 秒内自动重连

### 3. SRT 推流

- 桌面端为 listener，iOS 端为 caller 主动连接
- LIVE + 消息 API：每帧作为一条 SRT 消息发送，天然保持帧边界
- 延迟 120ms，支持 `SRTO_TLPKTDROP` 丢包保护，适合公网/弱网
- 桌面端用 ctypes 直连 libsrt，无需 pip 安装

### 4. 网页（HTTPS + WebSocket）

```
浏览器 getUserMedia → WebCodecs H.264 → wss://host:8443/ws/video
                    → AudioWorklet PCM16 → wss://host:8443/ws/audio
```

- 浏览器只在安全上下文授权摄像头，局域网 IP 必须 HTTPS，故使用自签证书
- 协议与 iOS 端完全一致，桌面端解码链路零改动
- 无 WebCodecs 时自动降级为 JPEG 抓帧，再降级为 BGRA

详见 [web-mode.md](web-mode.md)。

## 自适应码率

采集端根据两项观测决定码率乘数，取较小值应用到编码器：

| 观测 | idle/light | medium | heavy / serious | critical |
|------|-----------|--------|-----------------|----------|
| 发送背压（在途帧数 / 发送缓冲） | 1.0x | 0.7x | 0.4x | — |
| 设备热状态 | 1.0x | — | 0.7x | 0.5x |

- 在途帧上限 3 帧：60fps 每帧 16.7ms，TCP 发送完成典型 20–30ms，
  只允许 1 帧在途会把帧率卡在 30fps 左右
- SRT 模式下背压有两路来源（本地在途帧数 + `SRTO_SNDDATA` 未确认字节），
  分别记录后取合成值，避免两套机制互相覆盖造成码率抖动
- 目标码率 = `width × height × fps × bpp`，bpp 由画质预设决定（0.05 / 0.10 / 0.15），
  钳制在 1–25 Mbps

## 断线自愈

H.264 有帧间依赖，断流后解码器会花屏。项目在以下时机强制 IDR 关键帧并重置解码器：

- 起流首帧
- 客户端（重）连接
- 分辨率 / 画质预设切换（会重建 `VTCompressionSession`）
- 桌面端检测到连接断开后重连成功

同时关闭 OpenGOP，保证每个关键帧都是 IDR，不依赖前向参考即可重新同步。

## 性能参考

### 视频带宽

| 编码 | 分辨率 | 帧率 | 估算码率 |
|------|--------|------|----------|
| H.264 (medium) | 1080p | 60 | ≈ 12 Mbps |
| H.264 (low) | 1080p | 60 | ≈ 6 Mbps |
| JPEG 75 | 1080p | 30 | ≈ 15–20 Mbps |
| BGRA | 1080p | 60 | ≈ 500 Mbps |
| BGRA | 720p | 30 | ≈ 265 Mbps |

### 音频带宽

| 配置 | 单包大小 | 估算码率 |
|------|----------|----------|
| 48kHz / mono / 16-bit | ≤1456B | ≈ 768 kbps |

## 安全性

- 协议明文、无鉴权，**仅适用于可信局域网**
- 网页模式自签证书需用户手动信任，桌面端显示 SHA-256 指纹供核对
- 不可信网络请置于 VPN 或加密隧道内
- UDP 音频不保证可靠性，丢包由播放器的静音填充自然吸收
