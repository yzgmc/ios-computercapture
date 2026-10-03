# 网页模式（HTTPS + WebSocket）

不安装任何 App，用手机浏览器把摄像头和麦克风推给电脑。

## 为什么必须 HTTPS

浏览器的 `getUserMedia()` 只在**安全上下文**下可用。安全上下文包括
`https://`、`localhost`、`file://`——**局域网 `http://192.168.x.x` 不算**。
所以网页模式必须跑在 HTTPS 上。

局域网没有公共 CA 能签发证书（Let's Encrypt 无法给内网 IP 验证），
因此本项目使用**自签证书**。代价是浏览器首次访问会告警，需要手动信任。

## 为什么用 WebSocket

浏览器能建立的传输通道只有 HTTP / WebSocket / WebRTC：

| 方案 | 优点 | 缺点 |
|------|------|------|
| WebSocket | 复用 HTTPS 证书、能传二进制、实现简单 | 基于 TCP，弱网下有队头阻塞 |
| WebRTC | UDP + 拥塞控制，弱网更稳 | 需 SRTP/ICE/Signaling，Python 端要额外依赖 |

本模式面向同一局域网内的实时共享，WebSocket 的延迟（通常 < 50ms）完全够用，
且能直接承载既有的 RAW1 / AUD1 帧，桌面端解码链路零改动，因此选它。

## 工作方式

```
手机浏览器
  ├─ getUserMedia 采集视频轨 / 音频轨
  ├─ 视频：WebCodecs VideoEncoder (H.264, Annex-B)
  │        → 28B RAW1 头 + payload → wss://host:8443/ws/video
  └─ 音频：AudioWorklet 取 Float32 → 转 PCM16LE
           → 16B AUD1 头 + payload → wss://host:8443/ws/audio

桌面端 (aiohttp)
  ├─ GET /              返回 index.html
  ├─ GET /app.js        客户端逻辑
  ├─ GET /audio-worklet.js
  └─ GET /ws/*.         解析帧头 → 复用既有解码链路 → 虚拟设备
```

## 使用步骤

1. 桌面端安装依赖：`pip install aiohttp cryptography`
2. 启动桌面端，`python src/main.py`
3. 左上角模式切到 **网页 (HTTPS)**
4. 界面「监听」栏显示 `https://192.168.x.x:8443`，并附带证书指纹
5. 手机（与电脑同一 Wi-Fi）浏览器打开该地址
6. 证书告警页选择继续：
   - iOS Safari：点「显示详细信息」→「访问此网站」
   - Android Chrome：点「高级」→「继续前往」
   - 桌面 Chrome：点「高级」→「继续前往」
7. 页面点「开始共享」，授权摄像头与麦克风

> iOS Safari 对自签证书的限制较严，且要求证书有效期不超过 825 天
> （本项目取 365 天）并包含 `serverAuth` 扩展用途，配置已满足。

## 编码能力降级

| 浏览器能力 | 使用的编码 | 说明 |
|-----------|-----------|------|
| 支持 WebCodecs H.264 | H.264 硬件编码 | 最优，1080p60 约 8–12 Mbps |
| 不支持 WebCodecs | JPEG 抓帧（quality 0.75） | canvas `toBlob`，带宽较高 |
| 手动指定 | BGRA 原始帧 | 带宽极高，仅作兜底 |

WebCodecs 输出的 H.264 可能是 AVCC（长度前缀）而非 Annex-B（起始码），
客户端会自动检测并在需要时转换，同时从 `decoderConfig.description`（avcC）
提取 SPS / PPS 前置到关键帧，保证桌面端 PyAV 能立即解码。

## 已知限制

- **自签证书告警**：无法避免，除非改用公共域名 + 真实证书
- **IP 变化会重新生成证书**：证书 SAN 里写入了本机所有局域网 IP，
  IP 段变化后旧证书不匹配，会自动重新生成，此时手机需要再次信任
- **后台标签页会被节流**：浏览器会把后台标签的视频采集降频，
  共享期间请保持页面在前台（iOS Safari 尤其明显）
- **回声**：手机麦克风可能采集到电脑扬声器声音，建议电脑侧戴耳机

## 安全建议

- 只在可信局域网使用；协议明文、无鉴权
- 首次信任前核对桌面端显示的证书 SHA-256 指纹
- 共享结束后点「停止共享」并关闭页面，释放摄像头

## 文件位置

| 文件 | 作用 |
|------|------|
| `desktop/src/web/certs.py` | 自签证书生成与复用（cryptography，回退 openssl） |
| `desktop/src/web/server.py` | HTTPS 站点 + 两条 WebSocket 通道 + 帧解析 |
| `desktop/src/web/static/index.html` | 手机端页面 |
| `desktop/src/web/static/app.js` | getUserMedia / WebCodecs / WebSocket 客户端 |
| `desktop/src/web/static/audio-worklet.js` | 音频采集处理器 |
| `tests/test_web_mode.py` | 证书 + HTTPS + 帧解析的端到端测试 |

## 排障

| 现象 | 原因与处理 |
|------|-----------|
| 页面打不开 | 确认电脑与手机同网段；Windows 防火墙放行 8443 入站 |
| 提示证书不受信任 | 手动继续访问；若反复出现，删除 `desktop/.phonecam/` 让证书重新生成 |
| 点开始后无画面 | 检查浏览器是否支持 WebCodecs（页面顶部会提示降级到 JPEG） |
| 有画面无声音 | 检查页面「麦克风」是否开启；iOS 需允许麦克风权限 |
| 画面花屏 | 刷新页面重新推流；客户端每 60 帧会强制关键帧，稍等可自动恢复 |
| 桌面端提示 aiohttp 缺失 | `pip install aiohttp` |
