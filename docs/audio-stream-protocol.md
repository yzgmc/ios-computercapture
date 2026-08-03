# UDP 音频传输协议（Audio Stream Protocol）

本协议用于在局域网内通过 UDP 传输未压缩的 PCM 音频数据，配合 [Raw Stream Protocol](raw-stream-protocol.md)
（TCP 视频）共同构成 iPhone → 桌面端的音视频传输通道。

> 实现位置：
> - 桌面端接收：`desktop/src/audio_stream/`（`protocol.py`、`transport.py`、`player.py`）
> - iOS 端发送：`ios/PhoneCam/AudioStreamServer.swift`
> - 桌面端播放入口：`desktop/src/app.py` 的 `_on_audio_packet` → `AudioPlayer.feed`

## 设计目标

- 无压缩：直接传输 PCM 采样，避免 AAC/Opus 编解码延迟
- 容许丢包：UDP 不重传，丢包由桌面端播放器自然吸收（短暂跳变或静音）
- 简单：固定 16B 帧头，应用层无分片重组

## 帧头布局

每个 UDP 数据报 = **16 字节固定帧头** + 可变 payload。帧头采用大端序。

```
偏移  长度  字段            类型   说明
 0     4    magic          char[4] 固定标识 'AUD1'（0x41 0x55 0x44 0x31）
 4     4    seq            uint32  帧序号，单调递增；用于诊断丢包/乱序
 8     4    sample_rate    uint32  采样率（Hz，当前固定 48000）
12     1    channels       uint8   声道数（当前固定 1 = mono）
13     1    format         uint8   PCM 格式，见下表
14     2    payload_length uint16  payload 字节数（应等于 UDP 数据报长度 - 16）
```

对应 Python 结构格式串：`">4sIIBBH"`（见 `audio_stream/protocol.py`）。

### PCM 格式（format 字段）

| 值 | 名称             | 说明 |
|----|------------------|------|
| 0  | PCM16_LE         | 16-bit signed little-endian PCM（当前唯一使用） |
| 1  | PCM_FLOAT32_LE   | 32-bit float little-endian PCM（预留） |

## MTU 与切片

- 典型 MTU 1500 字节 → IP+UDP 头 28B → 应用层可用 1472B
- 帧头 16B → payload 上限 1456B
- iOS 端 `AudioStreamServer.maxPayloadBytes = 1456`，大于此值的 PCM 块按切片发送，
  每片独立 seq，便于桌面端诊断丢包

## 端口与寻址

- 默认 UDP 端口：`5001`（桌面端常量 `AUDIO_STREAM_PORT`）
- 桌面端绑定 `0.0.0.0:5001`，使用 `asyncio.DatagramEndpoint` 接收
- iOS 端配置桌面端 IP 与端口（与 TCP 视频通道共用 `rawStreamHost`）

## iOS 端发送流程

1. `CaptureManager` 配置 `AVCaptureAudioDataOutput.audioSettings`：
   `48kHz / mono / 16-bit PCM / 小端 / 交错`
2. `captureOutput` 收到 `CMSampleBuffer`，转交 `AudioStreamServer.processSampleBuffer`
3. 用 `CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer` 抽取 PCM 字节
4. 按 1456 字节切片，每片构造 16B 帧头 + payload 的 `Data`
5. 通过 `NWConnection`（UDP）`send` 写入桌面端 IP:5001

## 桌面端接收与播放流程

1. `app.py` 启动时 `AudioStreamReceiver.start()` 监听 `0.0.0.0:5001`
2. `asyncio.DatagramProtocol.datagram_received` 收到数据报 → `_handle_datagram`
3. 校验 magic + 解析帧头 → 提取 payload → 调用 `on_packet`
4. `on_packet` = `AudioPlayer.feed(pcm, sample_rate, channels, format)`
5. `AudioPlayer` 按格式重建 PyAudio 输出流（格式变化时自动重建）
6. PCM 入队 → PyAudio 回调从队列取数据写入虚拟音频设备（VB-Cable / BlackHole）

## 容错

- 丢包：UDP 自然丢弃，桌面端 PyAudio 回调队列空时输出静音
- 乱序：当前不处理，可能产生短暂跳变；如需处理可按 seq 重排
- 格式变化：`AudioPlayer` 检测到 sample_rate / channels / sample_bytes 变化时
  关闭旧流并按新格式重建 PyAudio 输出流
- 软件音量：`AudioPlayer.set_volume(0.0~1.0)` 在入队前对 PCM 做浮点缩放

## 限制

- 单接收方：桌面端 UDP 端口只接收一个 iOS 端的数据流
- 无加密鉴权：明文 PCM，仅适用于可信局域网
- 无重传：丢包不可恢复，弱网下会有 audible 跳变
