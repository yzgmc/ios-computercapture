import Foundation
import CoreVideo
import CoreMedia
import Darwin
import libsrt

/// UInt32 的大端序 4 字节 Data 视图
private extension UInt32 {
    var bigEndianData: Data {
        var be = self.bigEndian
        return Data(bytes: &be, count: 4)
    }
}

/// SRT 视频流传输（caller 模式）。镜像 RawStreamServer API，遵从 VideoStreamTransport。
///
/// 与 TCP 流式不同，SRT 使用 LIVE + 消息 API：每帧（28B 头 + payload）作为一个 SRT 消息发送，
/// 消息 API 自动保持帧边界，接收方无需按字节流分帧。
///
/// 背压监测：周期读 SRTO_SNDDATA（发送缓冲未确认字节数），映射为 BackpressureLevel，
/// 复用 CaptureManager 既有的自适应码率机制。
///
/// 注意：SRT 不支持 USB 直连（usbmuxd 不转发 SRT 协议），仅用于 LAN / 公网推流。
final class SRTStreamServer: VideoStreamTransport {
    static let headerSize = 28
    private static let magic: [UInt8] = [0x52, 0x41, 0x57, 0x31] // "RAW1"

    // SRT 常量直接使用 libsrt 模块导出的全局符号（SRT_INVALID_SOCK / SRT_ERROR
    // 为 Int32 全局常量；SRTO_* 为 SRT_SOCKOPT 枚举值；SRTT_LIVE 为 SRT_TRANSTYPE 枚举值）

    // MARK: - 状态
    private var socket: Int32 = -1
    private let queue = DispatchQueue(label: "com.yzg.phonecam.srtstream")
    private var frameID: UInt32 = 0
    private(set) var isRunning = false

    // 背压：与 RawStreamServer 一致，3 帧在途支撑 60fps 流水线
    private var pendingFrameCount = 0
    private let maxPendingFrames = 3
    private let pendingLock = NSLock()
    private var lastBackpressureLevel: BackpressureLevel = .idle

    /// 背压等级变化回调（在 queue 上调用）。
    var onBackpressure: ((BackpressureLevel) -> Void)?

    /// 连接就绪回调。CaptureManager 应强制下一帧 IDR。
    var onClientConnected: (() -> Void)?

    // SRTO_SNDDATA 周期监测
    private var backpressureTimer: DispatchSourceTimer?

    // 带宽估计与统计
    private var bytesInWindow: UInt64 = 0
    private var windowStart: Date = Date()
    private(set) var smoothedBytesPerSec: Double = 0
    private var sentCount: UInt64 = 0
    private var droppedCount: UInt64 = 0
    private var lastStatsTime: Date = Date()

    /// SRTO_SNDDATA 字节阈值 → BackpressureLevel 映射。
    /// H.264 1080p 单帧 ~5-50KB；阈值按典型帧大小倍数取。
    /// - idle:   < 50KB   （链路空闲或刚发完一帧）
    /// - light:  < 200KB  （1-4 帧在途，正常）
    /// - medium: < 500KB  （拥堵，预防性下调码率）
    /// - heavy:  >= 500KB （严重拥塞，需快速降码率避免雪崩）
    private let bpThresholdLight: Int32 = 50 * 1024
    private let bpThresholdMedium: Int32 = 200 * 1024
    private let bpThresholdHeavy: Int32 = 500 * 1024

    // MARK: - 生命周期

    deinit {
        stop()
    }

    /// caller 模式：主动连接远端 SRT listener（桌面端）。
    func start(host: String, port: UInt16, onReady: ((Bool) -> Void)? = nil) {
        guard !isRunning else {
            print("SRTStream: start() ignored, already running")
            onReady?(true)
            return
        }

        // srt_startup 全局仅一次（忽略返回值；多次调用安全）
        _ = srt_startup()

        let sock = srt_create_socket()
        if sock == SRT_INVALID_SOCK {
            print("SRTStream: srt_create_socket failed: \(String(cString: srt_getlasterror_str()))")
            onReady?(false)
            return
        }
        socket = sock

        _applySocketOptions(sock)

        // 在串行队列上阻塞连接，避免阻塞调用线程
        queue.async { [weak self] in
            guard let self = self else { return }
            var addr = sockaddr_in()
            addr.sin_family = sa_family_t(AF_INET)
            addr.sin_port = UInt16(port).bigEndian
            addr.sin_addr.s_addr = inet_addr(host)
            guard addr.sin_addr.s_addr != INADDR_NONE else {
                print("SRTStream: invalid host \(host)")
                self._closeSocket()
                onReady?(false)
                return
            }
            let rc = withUnsafePointer(to: &addr) { ptr -> Int32 in
                ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { saPtr in
                    srt_connect(self.socket, saPtr,
                                Int32(MemoryLayout<sockaddr_in>.size))
                }
            }
            if rc == SRT_ERROR {
                print("SRTStream: srt_connect failed: \(String(cString: srt_getlasterror_str()))")
                self._closeSocket()
                onReady?(false)
                return
            }
            print("SRTStream: connected -> \(host):\(port)")
            self.isRunning = true
            self._startBackpressureTimer()
            onReady?(true)
            // 新连接就绪：通知上层强制 IDR
            self.onClientConnected?()
        }
    }

    /// SRT 不支持 listener 模式（本工程中桌面端为 listener）。
    func startServer(port: UInt16, onReady: ((Bool) -> Void)?) {
        print("SRTStream: startServer not supported (SRT listener is on desktop side)")
        onReady?(false)
    }

    func stop() {
        // 立即标记停止，让 processXxx 不再接受新帧
        isRunning = false
        // 停背压监测
        backpressureTimer?.cancel()
        backpressureTimer = nil
        // 关闭 socket：在 queue 上同步执行，确保与 pending send 串行化
        // （srt_close 与 srt_send 在同一 socket 上并发不安全）
        let s = socket
        socket = SRT_INVALID_SOCK
        queue.sync {
            if s != SRT_INVALID_SOCK {
                _ = srt_close(s)
            }
        }
        frameID = 0
        pendingLock.lock()
        pendingFrameCount = 0
        lastBackpressureLevel = .idle
        pendingLock.unlock()
        // 不调用 srt_cleanup()：它是全局引用计数，进程退出时由 OS 回收
    }

    // MARK: - 帧发送

    func processSampleBuffer(_ sampleBuffer: CMSampleBuffer,
                             requiresBGRAConversion: Bool = false) {
        guard isRunning, let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        if !tryAcquirePendingSlot() { return }

        let buffer: CVPixelBuffer
        if requiresBGRAConversion {
            guard let converted = convertToBGRA(pixelBuffer) else {
                _decrementPending()
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
            _decrementPending()
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

    // MARK: - 内部

    /// 应用 LIVE 低延迟 socket 选项。
    private func _applySocketOptions(_ sock: Int32) {
        var yes: Int32 = 1
        var latency: Int32 = 120        // ms
        var conntimeo: Int32 = 3000     // ms
        var peeridle: Int32 = 5000      // ms
        var sndtimeo: Int32 = 1000      // ms，便于 stop 检查 + 防止 send 永久阻塞
        var transtype: SRT_TRANSTYPE = SRTT_LIVE

        _ = _setOpt(sock, SRTO_TRANSTYPE, &transtype)
        _ = _setOpt(sock, SRTO_TSBPDMODE, &yes)
        _ = _setOpt(sock, SRTO_LATENCY, &latency)
        _ = _setOpt(sock, SRTO_TLPKTDROP, &yes)
        _ = _setOpt(sock, SRTO_CONNTIMEO, &conntimeo)
        _ = _setOpt(sock, SRTO_PEERIDLETIMEO, &peeridle)
        _ = _setOpt(sock, SRTO_SNDTIMEO, &sndtimeo)
        _ = _setOpt(sock, SRTO_REUSEADDR, &yes)
        // SRTO_TRANSTYPE=LIVE 已默认启用消息 API，显式设置仅为防御
        _ = _setOpt(sock, SRTO_MESSAGEAPI, &yes)
    }

    @discardableResult
    private func _setOpt<T>(_ sock: Int32, _ opt: SRT_SOCKOPT, _ val: inout T) -> Int32 {
        let rc = withUnsafePointer(to: &val) { ptr -> Int32 in
            srt_setsockflag(sock, opt, ptr, Int32(MemoryLayout<T>.size))
        }
        if rc == SRT_ERROR {
            print("SRTStream: setsockflag opt=\(opt) failed: \(String(cString: srt_getlasterror_str()))")
        }
        return rc
    }

    private func _closeSocket() {
        let s = socket
        socket = SRT_INVALID_SOCK
        if s != SRT_INVALID_SOCK {
            _ = srt_close(s)
        }
    }

    /// 周期读 SRTO_SNDDATA，映射为 BackpressureLevel 并回调。
    private func _startBackpressureTimer() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + .milliseconds(100),
                       repeating: .milliseconds(100))
        timer.setEventHandler { [weak self] in
            self?._pollBackpressure()
        }
        timer.resume()
        backpressureTimer = timer
    }

    private func _pollBackpressure() {
        guard socket != SRT_INVALID_SOCK else { return }
        var snddata: Int32 = 0
        var optlen: Int32 = Int32(MemoryLayout<Int32>.size)
        let rc = withUnsafeMutablePointer(to: &snddata) { ptr -> Int32 in
            srt_getsockflag(socket, SRTO_SNDDATA, ptr, &optlen)
        }
        guard rc != SRT_ERROR else { return }

        let newLevel: BackpressureLevel
        if snddata < bpThresholdLight {
            newLevel = .idle
        } else if snddata < bpThresholdMedium {
            newLevel = .light
        } else if snddata < bpThresholdHeavy {
            newLevel = .medium
        } else {
            newLevel = .heavy
        }
        let oldLevel = lastBackpressureLevel
        lastBackpressureLevel = newLevel
        if newLevel != oldLevel {
            print("SRTStream: backpressure=\(newLevel.rawValue) (snddata=\(snddata)B)")
            onBackpressure?(newLevel)
        }
    }

    private func tryAcquirePendingSlot() -> Bool {
        pendingLock.lock()
        if pendingFrameCount >= maxPendingFrames {
            droppedCount &+= 1
            pendingLock.unlock()
            return false
        }
        pendingFrameCount += 1
        let newLevel = BackpressureLevel(rawValue: min(pendingFrameCount, maxPendingFrames)) ?? .idle
        let oldLevel = lastBackpressureLevel
        lastBackpressureLevel = newLevel
        pendingLock.unlock()
        if newLevel != oldLevel {
            onBackpressure?(newLevel)
        }
        return true
    }

    private func _decrementPending() {
        pendingLock.lock()
        pendingFrameCount -= 1
        let newLevel = BackpressureLevel(rawValue: min(pendingFrameCount, maxPendingFrames)) ?? .idle
        let oldLevel = lastBackpressureLevel
        lastBackpressureLevel = newLevel
        pendingLock.unlock()
        if newLevel != oldLevel {
            onBackpressure?(newLevel)
        }
    }

    private func send(_ data: Data) {
        let sock = socket  // 捕获当前 socket 值
        queue.async { [weak self] in
            // defer 保证无论发送成功与否都释放背压槽位
            defer { self?._decrementPending() }
            // socket 已关闭/替换（stop 被调用过）→ 跳过发送
            guard sock != SRT_INVALID_SOCK else { return }
            let rc = data.withUnsafeBytes { (rawBuf: UnsafeRawBufferPointer) -> Int32 in
                guard let base = rawBuf.baseAddress else { return SRT_ERROR }
                let ptr = base.assumingMemoryBound(to: CChar.self)
                return srt_send(sock, ptr, Int32(data.count))
            }
            if rc == SRT_ERROR {
                let err = String(cString: srt_getlasterror_str())
                if !err.isEmpty {
                    print("SRTStream send error: \(err)")
                }
            }
        }
    }

    private func _maybePrintStats(payloadLen: Int, isKeyframe: Bool, format: String) {
        let now = Date()
        bytesInWindow &+= UInt64(payloadLen + Self.headerSize)
        let windowElapsed = now.timeIntervalSince(windowStart)
        if windowElapsed >= 1.0 {
            let instantaneous = Double(bytesInWindow) / windowElapsed
            if smoothedBytesPerSec == 0 {
                smoothedBytesPerSec = instantaneous
            } else {
                smoothedBytesPerSec = smoothedBytesPerSec * 0.5 + instantaneous * 0.5
            }
            bytesInWindow = 0
            windowStart = now
        }
        if now.timeIntervalSince(lastStatsTime) >= 1.0 {
            let elapsed = now.timeIntervalSince(lastStatsTime)
            let sendFps = Double(sentCount) / elapsed
            let dropFps = Double(droppedCount) / elapsed
            print(String(format: "SRTStream(%@): %.1f fps sent, %.1f fps dropped (bw=%.1f Mbps%@)",
                         format, sendFps, dropFps,
                         smoothedBytesPerSec * 8 / 1_000_000,
                         isKeyframe ? " [key]" : ""))
            sentCount = 0
            droppedCount = 0
            lastStatsTime = now
        }
    }

    private func convertToBGRA(_ source: CVPixelBuffer) -> CVPixelBuffer? {
        let srcFormat = CVPixelBufferGetPixelFormatType(source)
        if srcFormat == kCVPixelFormatType_32BGRA {
            return source
        }
        print("SRTStreamServer: non-BGRA pixel format \(srcFormat) not supported, dropping frame")
        return nil
    }
}
