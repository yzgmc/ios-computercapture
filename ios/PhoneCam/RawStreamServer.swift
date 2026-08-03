import Foundation
import Network
import CoreVideo
import CoreMedia

/// UInt32 的大端序 4 字节 Data 视图
private extension UInt32 {
    var bigEndianData: Data {
        var be = self.bigEndian
        return Data(bytes: &be, count: 4)
    }
}

/// 整型大端序 4 字节 Data 视图
private extension Int32 {
    var bigEndianData: Data {
        var be = self.bigEndian
        return Data(bytes: &be, count: 4)
    }
}

/// 背压等级。CaptureManager 据此调整码率/fps。
enum BackpressureLevel: Int {
    case idle = 0     // pending=0，链路空闲
    case light = 1    // pending=1，正常
    case medium = 2   // pending=2，开始拥堵
    case heavy = 3    // pending=3 (max)，已开始丢帧
}

/// 原画质视频传输：将采集到的 CVPixelBuffer / JPEG / H.264 通过 TCP 发送到桌面端。
///
/// 协议帧头（大端序，28 字节）：
/// magic(4B)="RAW1" | frame_id(4B) | width(4B) | height(4B)
/// | format(4B=0 BGRA / 10 JPEG / 20 H264) | bytes_per_row(4B) | payload_length(4B)
///
/// v2 优化：
/// - 显式启用 TCP_NODELAY + 禁用 ACK 拉伸，降低小包延迟；
/// - 带宽估计器：基于 send completion 的指数移动平均，输出 bytes/sec；
/// - 背压等级回调：让 CaptureManager 触发自适应码率调整；
/// - 客户端连接/断开回调：新客户端接入时强制 IDR 关键帧。
final class RawStreamServer: VideoStreamTransport {
    static let headerSize = 28
    private static let magic: [UInt8] = [0x52, 0x41, 0x57, 0x31] // "RAW1"

    private var connection: NWConnection?
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "com.yzg.phonecam.rawstream")
    private var frameID: UInt32 = 0
    private(set) var isRunning = false

    // 背压：允许 3 帧在途，支撑 60fps 流水线发送。
    // 60fps 每帧 16.7ms，TCP send completion 典型 ~20-30ms，
    // maxPendingFrames=1 时上限仅 ~33fps（正好对应实测 30 多帧）。
    // 3 帧在途 = 最多 50ms 延迟，实时视频可接受。
    private var pendingFrameCount = 0
    private let maxPendingFrames = 3
    private let pendingLock = NSLock()
    private var lastBackpressureLevel: BackpressureLevel = .idle

    /// 背压等级变化回调（在 queue 上调用）。
    /// CaptureManager 据此动态调整码率：heavy 时下调，idle 时回升。
    var onBackpressure: ((BackpressureLevel) -> Void)?

    /// 客户端（重）连接回调。CaptureManager 应强制下一帧为 IDR 关键帧，
    /// 让新接入的解码器立即同步。
    var onClientConnected: (() -> Void)?

    // 带宽估计：基于 send completion 累计字节，1s 窗口指数移动平均。
    private var bytesInWindow: UInt64 = 0
    private var windowStart: Date = Date()
    /// 平滑后的发送吞吐量（bytes/sec），1Hz 更新。
    private(set) var smoothedBytesPerSec: Double = 0
    /// 上一次计算的瞬时吞吐量（bytes/sec）。
    private(set) var instantaneousBytesPerSec: Double = 0

    // 统计：每秒打印一次 fps 与丢帧
    private var sentCount: UInt64 = 0
    private var droppedCount: UInt64 = 0
    private var lastStatsTime: Date = Date()

    /// 构建 TCP 参数：启用 TCP_NODELAY、禁用 ACK 拉伸、连接超时 5s。
    /// 这些参数降低小帧（H.264 P 帧 ~5-20KB）的发送延迟。
    private static func makeTcpParameters() -> NWParameters {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true                  // 禁用 Nagle，立即发送小包
        tcp.disableAckStretching = true     // 禁用 ACK 拉伸，降低交互延迟（牺牲少量吞吐）
        tcp.connectionTimeout = 5           // 5s 连接超时
        tcp.keepaliveIdle = 30              // 30s 空闲后开始 keepalive 探测
        tcp.keepaliveInterval = 10
        tcp.keepaliveCount = 3
        let params = NWParameters(tls: nil, tcp: tcp)
        params.allowFastOpen = true         // TCP Fast Open：首帧可随 SYN 携带
        params.includePeerToPeer = false
        return params
    }

    /// 连接到桌面端 TCP 端口。onReady 在 NWConnection.state == .ready 时回调。
    func start(host: String, port: UInt16, onReady: ((Bool) -> Void)? = nil) {
        guard !isRunning else {
            print("RawStream: start() ignored, already running")
            onReady?(true)
            return
        }
        let endpoint = NWEndpoint.hostPort(host: NWEndpoint.Host(host),
                                            port: NWEndpoint.Port(integerLiteral: port))
        let params = Self.makeTcpParameters()
        let conn = NWConnection(to: endpoint, using: params)
        var isFirstReady = true
        conn.stateUpdateHandler = { [weak self] state in
            switch state {
            case .setup:
                print("RawStream: setup -> \(host):\(port)")
            case .preparing:
                print("RawStream: preparing")
            case .ready:
                print("RawStream: TCP ready -> \(host):\(port)")
                self?.isRunning = true
                if isFirstReady {
                    isFirstReady = false
                    onReady?(true)
                    // 新连接就绪：通知上层强制 IDR，让接收端解码器立即同步
                    self?.onClientConnected?()
                }
            case .waiting(let err):
                print("RawStream: waiting (\(err))")
                onReady?(false)
            case .failed(let err):
                print("RawStream: failed (\(err))")
                self?.isRunning = false
                onReady?(false)
            case .cancelled:
                print("RawStream: cancelled")
                self?.isRunning = false
                onReady?(false)
            @unknown default:
                print("RawStream: unknown state")
            }
        }
        conn.start(queue: queue)
        connection = conn
    }

    /// USB 模式：作为 TCP 服务器监听 port，等待桌面端通过 usbmuxd 桥接访问。
    /// 数据流：桌面客户端 → PC 127.0.0.1:port → usbmuxd → iOS 127.0.0.1:port → 此 listener。
    /// onReady 在 listener.state == .ready 时回调（表示端口已开始监听）。
    func startServer(port: UInt16, onReady: ((Bool) -> Void)? = nil) {
        guard !isRunning else {
            print("RawStream: startServer() ignored, already running")
            onReady?(true)
            return
        }
        do {
            let params = Self.makeTcpParameters()
            let listener = try NWListener(using: params, on: NWEndpoint.Port(integerLiteral: port))
            var isFirstReady = true
            listener.stateUpdateHandler = { [weak self] state in
                switch state {
                case .setup:
                    print("RawStream listener: setup on :\(port)")
                case .ready:
                    print("RawStream listener: ready on :\(port)")
                    self?.isRunning = true
                    if isFirstReady {
                        isFirstReady = false
                        onReady?(true)
                    }
                case .waiting(let err):
                    print("RawStream listener: waiting (\(err))")
                    onReady?(false)
                case .failed(let err):
                    print("RawStream listener: failed (\(err))")
                    self?.isRunning = false
                    onReady?(false)
                case .cancelled:
                    print("RawStream listener: cancelled")
                    self?.isRunning = false
                    onReady?(false)
                @unknown default:
                    print("RawStream listener: unknown state")
                }
            }
            listener.newConnectionHandler = { [weak self] conn in
                print("RawStream listener: accepted connection from \(String(describing: conn.endpoint))")
                // USB 模式只接受一条连接（桌面端唯一客户端）
                // 重置背压计数：之前无连接时 processFrame 增加的 pending 已被 send() 释放，
                // 但为防御性，此处强制清零，确保客户端连接后帧能正常流出。
                self?.pendingLock.lock()
                self?.pendingFrameCount = 0
                self?.lastBackpressureLevel = .idle
                self?.pendingLock.unlock()
                // 重置带宽估计窗口
                self?.bytesInWindow = 0
                self?.windowStart = Date()
                self?.connection = conn
                conn.stateUpdateHandler = { [weak self] state in
                    switch state {
                    case .ready:
                        // 新客户端接入：通知上层强制 IDR
                        self?.onClientConnected?()
                    default:
                        break
                    }
                }
                conn.start(queue: self?.queue ?? .global())
            }
            listener.start(queue: queue)
            self.listener = listener
        } catch {
            print("RawStream listener: create failed (\(error))")
            onReady?(false)
        }
    }

    func stop() {
        connection?.cancel()
        connection = nil
        listener?.cancel()
        listener = nil
        isRunning = false
        frameID = 0
        pendingLock.lock()
        pendingFrameCount = 0
        lastBackpressureLevel = .idle
        pendingLock.unlock()
    }

    /// 处理一帧采集数据，转 BGRA 后整帧发送。仅在 isRunning 时生效。
    func processSampleBuffer(_ sampleBuffer: CMSampleBuffer,
                             requiresBGRAConversion: Bool = false) {
        guard isRunning, let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        if !tryAcquirePendingSlot() { return }

        let buffer: CVPixelBuffer
        if requiresBGRAConversion {
            guard let converted = convertToBGRA(pixelBuffer) else {
                self._decrementPending()
                return
            }
            buffer = converted
        } else {
            buffer = pixelBuffer
        }

        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }

        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        let bytesPerRow = CVPixelBufferGetBytesPerRow(buffer)
        guard let basePtr = CVPixelBufferGetBaseAddress(buffer) else {
            self._decrementPending()
            return
        }

        let payloadLength = bytesPerRow * height
        let currentFrameID = frameID
        frameID &+= 1

        var packet = Data(capacity: Self.headerSize + payloadLength)
        packet.append(contentsOf: Self.magic)
        packet.append(UInt32(currentFrameID).bigEndianData)
        packet.append(UInt32(width).bigEndianData)
        packet.append(UInt32(height).bigEndianData)
        packet.append(UInt32(0).bigEndianData)              // format=0 BGRA
        packet.append(UInt32(bytesPerRow).bigEndianData)
        packet.append(UInt32(payloadLength).bigEndianData)
        packet.append(basePtr.assumingMemoryBound(to: UInt8.self), count: payloadLength)

        send(packet)
        sentCount &+= 1
        _maybePrintStats(payloadLen: payloadLength, isKeyframe: false, format: "BGRA")
    }

    private func _decrementPending() {
        pendingLock.lock()
        pendingFrameCount -= 1
        // 背压等级更新：发送完成后可能回到更低等级
        let newLevel = BackpressureLevel(rawValue: min(pendingFrameCount, maxPendingFrames)) ?? .idle
        let oldLevel = lastBackpressureLevel
        lastBackpressureLevel = newLevel
        pendingLock.unlock()
        if newLevel != oldLevel {
            onBackpressure?(newLevel)
        }
    }

    /// 发送 JPEG 压缩帧。format=10, bytes_per_row=0, payload=JPEG 字节流。
    func processJPEGFrame(_ jpegData: Data, width: Int, height: Int) {
        guard isRunning else { return }
        if !tryAcquirePendingSlot() { return }

        let currentFrameID = frameID
        frameID &+= 1

        var packet = Data(capacity: Self.headerSize + jpegData.count)
        packet.append(contentsOf: Self.magic)
        packet.append(UInt32(currentFrameID).bigEndianData)
        packet.append(UInt32(width).bigEndianData)
        packet.append(UInt32(height).bigEndianData)
        packet.append(UInt32(10).bigEndianData)              // format=10 JPEG
        packet.append(UInt32(0).bigEndianData)               // bytes_per_row=0
        packet.append(UInt32(jpegData.count).bigEndianData)
        packet.append(jpegData)

        send(packet)
        sentCount &+= 1
        _maybePrintStats(payloadLen: jpegData.count, isKeyframe: false, format: "JPEG")
    }

    /// 发送 H.264 编码帧。format=20, bytes_per_row=0, payload=Annex-B Access Unit。
    func processH264Frame(_ h264Data: Data, width: Int, height: Int, isKeyframe: Bool) {
        guard isRunning else { return }
        if !tryAcquirePendingSlot() { return }

        let currentFrameID = frameID
        frameID &+= 1

        var packet = Data(capacity: Self.headerSize + h264Data.count)
        packet.append(contentsOf: Self.magic)
        packet.append(UInt32(currentFrameID).bigEndianData)
        packet.append(UInt32(width).bigEndianData)
        packet.append(UInt32(height).bigEndianData)
        packet.append(UInt32(20).bigEndianData)               // format=20 H264
        packet.append(UInt32(0).bigEndianData)                // bytes_per_row=0
        packet.append(UInt32(h264Data.count).bigEndianData)
        packet.append(h264Data)

        send(packet)
        sentCount &+= 1
        _maybePrintStats(payloadLen: h264Data.count, isKeyframe: isKeyframe, format: "H264")
    }

    /// 尝试获取一个背压槽位。成功返回 true（调用方稍后必须 _decrementPending），
    /// 失败（已满）返回 false 并累计丢帧统计。
    private func tryAcquirePendingSlot() -> Bool {
        pendingLock.lock()
        if pendingFrameCount >= maxPendingFrames {
            droppedCount &+= 1
            pendingLock.unlock()
            return false
        }
        pendingFrameCount += 1
        // 等级提升通知
        let newLevel = BackpressureLevel(rawValue: min(pendingFrameCount, maxPendingFrames)) ?? .idle
        let oldLevel = lastBackpressureLevel
        lastBackpressureLevel = newLevel
        pendingLock.unlock()
        if newLevel != oldLevel {
            onBackpressure?(newLevel)
        }
        return true
    }

    /// 每秒打印一次统计 + 更新带宽估计。
    private func _maybePrintStats(payloadLen: Int, isKeyframe: Bool, format: String) {
        let now = Date()
        // 带宽估计窗口（1s EMA）
        bytesInWindow &+= UInt64(payloadLen + Self.headerSize)
        let windowElapsed = now.timeIntervalSince(windowStart)
        if windowElapsed >= 1.0 {
            instantaneousBytesPerSec = Double(bytesInWindow) / windowElapsed
            // EMA α=0.5：兼顾平滑与响应速度
            if smoothedBytesPerSec == 0 {
                smoothedBytesPerSec = instantaneousBytesPerSec
            } else {
                smoothedBytesPerSec = smoothedBytesPerSec * 0.5 + instantaneousBytesPerSec * 0.5
            }
            bytesInWindow = 0
            windowStart = now
        }
        // fps / 丢帧统计
        if now.timeIntervalSince(lastStatsTime) >= 1.0 {
            let elapsed = now.timeIntervalSince(lastStatsTime)
            let sendFps = Double(sentCount) / elapsed
            let dropFps = Double(droppedCount) / elapsed
            print(String(format: "RawStream(%@): %.1f fps sent, %.1f fps dropped (pending=%d, bw=%.1f Mbps%@)",
                         format, sendFps, dropFps, pendingFrameCount,
                         smoothedBytesPerSec * 8 / 1_000_000,
                         isKeyframe ? " [key]" : ""))
            sentCount = 0
            droppedCount = 0
            lastStatsTime = now
        }
    }

    private func send(_ data: Data) {
        guard let conn = connection else {
            // USB 服务器模式：暂无客户端连接，立即释放背压名额，
            // 避免 pendingFrameCount 卡在 maxPendingFrames 导致后续帧全丢。
            _decrementPending()
            return
        }
        conn.send(content: data, completion: .contentProcessed { [weak self] error in
            // 发送完成才允许下一帧进入（背压）
            self?._decrementPending()
            if let error = error {
                print("RawStream send error: \(error)")
            }
        })
    }

    /// 将任意格式的 CVPixelBuffer 转换为 BGRA。仅在采集格式非 BGRA 时使用。
    private func convertToBGRA(_ source: CVPixelBuffer) -> CVPixelBuffer? {
        let srcFormat = CVPixelBufferGetPixelFormatType(source)
        if srcFormat == kCVPixelFormatType_32BGRA {
            return source
        }
        print("RawStreamServer: non-BGRA pixel format \(srcFormat) not supported, dropping frame")
        return nil
    }
}
