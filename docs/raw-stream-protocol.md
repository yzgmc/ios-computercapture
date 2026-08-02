# 原画质传输协议（Raw Stream Protocol）

本协议用于在局域网内通过 UDP 传输未压缩的原始像素帧（默认 BGRA），适用于对画质
有无损要求的场景（如 iPhone 摄像头预览到桌面端的虚拟摄像头输出）。

> 实现位置：
> - 桌面端接收：`desktop/src/raw_stream/`（`protocol.py`、`transport.py`）
> - iOS 端发送：`ios/PhoneCam/RawStreamServer.swift`
> - 桌面端渲染入口：`desktop/src/app.py` 的 `_display_raw_frame`

## 设计目标

- 无压缩：直接传输采集到的像素缓冲，避免 H.264 编解码延迟与画质损失
- 简单可靠：基于 UDP 分片，无重传；丢帧由下一帧自然覆盖
- 低延迟：接收方组装完成立即回调，不做帧缓冲对齐

带宽需求参考（仅视频净载荷）：

| 分辨率   | 帧率 | 单帧大小 (BGRA) | 估算码率 |
|----------|------|-----------------|----------|
| 720p     | 30   | 1280×720×4 ≈ 3.5MB | ≈ 265 Mbps |
| 1080p    | 30   | 1920×1080×4 ≈ 7.9MB | ≈ 566 Mbps |
| 720p     | 60   | 3.5MB            | ≈ 530 Mbps |

> 仅在千兆及以上有线/802.11ac Wi-Fi 网络下可用，普通家用 Wi-Fi 易丢包卡顿。

## 帧头布局

每个 UDP 数据报由一个 **32 字节固定帧头** + 可变 payload 组成。帧头采用大端序
（network byte order）。

```
偏移  长度  字段            类型   说明
 0     4    magic          char[4] 固定标识 'RAW1'（0x52 0x41 0x57 0x31）
 4     4    frame_id       uint32  帧序号，单调递增；同一帧的所有分片共享
 8     4    chunk_idx      uint32  当前分片索引（从 0 开始）
12     4    total_chunks   uint32  该帧总分片数
16     4    width          uint32  像素宽
20     4    height         uint32  像素高
24     4    format         uint32  像素格式，见下表
28     4    bytes_per_row  uint32  每行字节数（含 stride 对齐填充）
```

对应 Python 结构格式串：`">4sIIIIIII"`（见 `protocol.py`）。

### 像素格式（format 字段）

| 值 | 名称     | 说明 |
|----|----------|------|
| 0  | BGRA     | Blue/Green/Red/Alpha，每像素 4 字节，iOS 默认 |
| 1  | RGBA     | Red/Green/Blue/Alpha，每像素 4 字节 |
| 2  | YUV422   | (U Y V Y) 打包，每像素平均 2 字节 |

## 分片与重组

- 发送方将单帧像素缓冲按 `MAX_PAYLOAD_SIZE`（1400 字节，留出 MTU 余量）切片，
  逐片写入 `chunk_idx` / `total_chunks`，附带同一 `frame_id`。
- 接收方按 `frame_id` 维护 `_FrameAssembler`，收到分片写入 `chunks[chunk_idx]`。
- 收齐 `total_chunks` 个分片后按索引顺序拼接，调用 `on_frame` 回调。
- 同一 `frame_id` 的不完整分片组存活超过 `_FRAME_TTL`（1 秒）即被清理，
  避免丢片导致的内存泄漏。

## 端口与寻址

- 默认 UDP 端口：`5000`（桌面端常量 `RAW_STREAM_PORT`，iOS 端可配置）
- 桌面端绑定 `0.0.0.0:5000`，等待 iOS 端主动推流
- iOS 端需配置桌面端的局域网 IP（见 `ContentView.rawStreamCard`）

## iOS 端发送流程

1. `CaptureManager.captureOutput` 收到 `CMSampleBuffer`
2. 转发至 `RawStreamServer.processSampleBuffer`
3. `convertToBGRA` 用 vImage 将 420v/420f 转 BGRA
4. 按行扫描切片为 ≤1400 字节分片
5. 通过 `NWConnection`（UDP）发送到桌面端 IP:5000

## 桌面端接收流程

1. `app.py` 启动时 `RawStreamReceiver.start()` 绑定 `0.0.0.0:5000`
2. `_DatagramProtocol.datagram_received` 回调 `_handle_packet`
3. 校验 `MAGIC` → 解析帧头 → 写入对应 `_FrameAssembler`
4. 组装完成 → `on_frame(raw, w, h, format, bytes_per_row)`
5. 信号转发到主线程 → `_display_raw_frame` 用 OpenCV/Qt 渲染

## 与 WebRTC 的关系

| 通道    | 用途                       | 协议 |
|---------|----------------------------|------|
| WebRTC  | 视频预览、音频、控制信令   | SRTP over DTLS |
| Raw Stream | 原画质视频（无音频/控制） | UDP（本协议） |

两者并存：WebRTC 提供低带宽兼容性，Raw Stream 提供无损画质。当桌面端 IP 可达
且 iOS 端开关打开时，Raw Stream 作为附加视频源，覆盖 WebRTC 预览画面。

## 限制与未来工作

- 无重传机制：丢包率高的链路上会出现画面撕裂或丢帧
- 无序号回传：发送方无法感知接收方丢包率
- 单目标：当前仅支持 1 对 1 推送
- 未来可考虑：FEC 前向纠错、接收端 NACK 反馈、多目标组播
