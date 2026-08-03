# 原画质传输协议（Raw Stream Protocol）

本协议用于在局域网内通过 TCP 传输未压缩的原始像素帧（默认 BGRA），适用于对画质
有无损要求的场景（如 iPhone 摄像头预览到桌面端的虚拟摄像头输出）。

> 实现位置：
> - 桌面端接收：`desktop/src/raw_stream/`（`protocol.py`、`transport.py`）
> - iOS 端发送：`ios/PhoneCam/RawStreamServer.swift`
> - 桌面端渲染入口：`desktop/src/app.py` 的 `_display_raw_frame`

## 设计目标

- 无压缩：直接传输采集到的像素缓冲，避免 H.264 编解码延迟与画质损失
- 简单可靠：基于 TCP 流式分帧，由内核保证字节序与可靠性，无需应用层重组
- 低延迟：接收方读取到完整一帧立即回调，不做帧缓冲对齐

带宽需求参考（仅视频净载荷）：

| 分辨率   | 帧率 | 单帧大小 (BGRA) | 估算码率 |
|----------|------|-----------------|----------|
| 720p     | 30   | 1280×720×4 ≈ 3.5MB | ≈ 265 Mbps |
| 1080p    | 30   | 1920×1080×4 ≈ 7.9MB | ≈ 566 Mbps |
| 720p     | 60   | 3.5MB            | ≈ 530 Mbps |

> 仅在千兆及以上有线/802.11ac Wi-Fi 网络下可用，普通家用 Wi-Fi 易丢包卡顿。

## 协议演进说明

| 版本 | 传输层 | 帧头 | 现状 |
|------|--------|------|------|
| v1   | UDP 分片（chunk_idx / total_chunks） | 32B | 已废弃：单帧分片过多，丢一包整帧无法重组 |
| v2   | TCP 流式（payload_length 精确读取） | 28B | 当前版本：整帧一次性写入，可靠性由 TCP 保证 |

## 帧头布局

每个 TCP 流中的"一帧"由一个 **28 字节固定帧头** + 可变 payload 组成。帧头采用
大端序（network byte order）。

```
偏移  长度  字段            类型   说明
 0     4    magic          char[4] 固定标识 'RAW1'（0x52 0x41 0x57 0x31）
 4     4    frame_id       uint32  帧序号，单调递增；用于诊断乱序/跳帧
 8     4    width          uint32  像素宽
12     4    height         uint32  像素高
16     4    format         uint32  像素格式，见下表
20     4    bytes_per_row  uint32  每行字节数（含 stride 对齐填充）
24     4    payload_length uint32  紧随其后的 payload 字节数
```

对应 Python 结构格式串：`">4sIIIIII"`（见 `protocol.py`）。

### 像素格式（format 字段）

| 值 | 名称     | 说明 |
|----|----------|------|
| 0  | BGRA     | Blue/Green/Red/Alpha，每像素 4 字节，iOS 默认 |
| 1  | RGBA     | Red/Green/Blue/Alpha，每像素 4 字节 |
| 2  | YUV422   | (U Y V Y) 打包，每像素平均 2 字节 |

## 流式分帧

- 发送方一次性 `write(header + payload)`（iOS 端 `RawStreamServer.processSampleBuffer`）。
- 接收方先 `readexactly(28)` 读满帧头，解析 `payload_length`，再 `readexactly(payload_length)`
  精确读取 payload（桌面端 `RawStreamReceiver._handle_client`）。
- TCP 流由内核保证字节顺序与可靠性，应用层无需关心分片、重组、丢包。
- payload_length 异常（0 或超过 64MB 上限）会关闭连接，防御协议错位。

## 端口与寻址

- 默认 TCP 端口：`5000`（桌面端常量 `RAW_STREAM_PORT`，iOS 端可配置）
- 桌面端绑定 `0.0.0.0:5000`，等待 iOS 端主动连接
- iOS 端需配置桌面端的局域网 IP（见 `ContentView.rawStreamCard`）

## iOS 端发送流程

1. `CaptureManager.captureOutput` 收到 `CMSampleBuffer`
2. 转发至 `RawStreamServer.processSampleBuffer`
3. `convertToBGRA` 兜底非 BGRA 输入（当前采集已配 BGRA，直接通过）
4. 锁定 `CVPixelBuffer` 基址，构造 28B 帧头 + 整帧 payload 的 `Data`
5. 通过 `NWConnection`（TCP）单次 `send` 写入桌面端 IP:5000

## 桌面端接收流程

1. `app.py` 启动时 `RawStreamReceiver.start()` 监听 `0.0.0.0:5000`
2. `asyncio.start_server` 接受连接 → `_handle_client(reader, writer)`
3. 循环 `readexactly(28)` 读帧头 → 校验 `MAGIC` → 解析 `payload_length`
4. `readexactly(payload_length)` 读 payload → 调用 `on_frame`
5. `on_frame` 通过 Qt 信号转发到主线程 → `_display_raw_frame` 用 OpenCV/Qt 渲染

## 与 UDP 音频通道的关系

| 通道       | 用途                | 协议             | 帧头 |
|------------|---------------------|------------------|------|
| Raw Stream | 原画质视频（BGRA）  | TCP（本协议）    | 28B  |
| Audio Stream | 麦克风音频（PCM16） | UDP（独立协议） | 16B  |

视频走 TCP 保证每帧完整可靠，音频走 UDP 容许丢包以保实时性。两条通道独立，
分别监听 5000 / 5001 端口，iOS 端在 `ContentView` 中分别开关。

## 限制与未来工作

- TCP 单连接：当前仅支持 1 对 1 推送；多接收方需在发送端维护连接列表
- 阻塞式背压：发送速率受 TCP 拥塞控制约束，弱网下帧会堆积在发送缓冲
- 无序号回传：发送方无法感知接收方丢帧率（可通过 frame_id 间隔诊断）
- 未来可考虑：多目标分发、基于 QUIC 的不可靠 + 可靠混合传输
