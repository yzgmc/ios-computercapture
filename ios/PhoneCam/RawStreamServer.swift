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

/// 原画质无压缩视频传输：将采集到的 CVPixelBuffer 以 BGRA 像素通过 TCP 整帧发送到桌面端。
///
/// 协议帧头（大端序，28 字节）：
/// magic(4B)="RAW1" | frame_id(4B) | width(4B) | height(4B)
/// | format(4B=0 BGRA) | bytes_per_row(4B) | payload_length(4B)
///
/// 发送方一次性 write(header + payload)，TCP 流由内核分片；
/// 接收方按 payload_length 精确读取整帧，无需重组分片。
/// 相比 UDP 分片方案，TCP 整帧传输不会因丢包导致每帧无法重组。
final class RawStreamServer {
    static let headerSize = 28
    private static let magic: [UInt8] = [0x52, 0x41, 0x57, 0x49] // "RAW1"

    private var connection: NWConnection?
    private let queue = DispatchQueue(label: "com.yzg.phonecam.rawstream")
    private var frameID: UInt32 = 0
    private(set) var isRunning = false

    /// 连接到桌面端 TCP 端口。onReady 在 NWConnection.state == .ready 时回调。
    func start(host: String, port: UInt16, onReady: ((Bool) -> Void)? = nil) {
        guard !isRunning else {
            print("RawStream: start() ignored, already running")
            onReady?(true)
            return
        }
        let endpoint = NWEndpoint.hostPort(host: NWEndpoint.Host(host),
                                            port: NWEndpoint.Port(integerLiteral: port))
        let params = NWParameters.tcp
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

    func stop() {
        connection?.cancel()
        connection = nil
        isRunning = false
        frameID = 0
    }

    /// 处理一帧采集数据，转 BGRA 后整帧发送。仅在 isRunning 时生效。
    /// - Parameters:
    ///   - sampleBuffer: 来自 AVCaptureVideoDataOutput 的采样缓冲
    ///   - requiresBGRAConversion: 若采集格式非 BGRA，需先转换（默认 false，假设已配置 BGRA 输出）
    func processSampleBuffer(_ sampleBuffer: CMSampleBuffer,
                             requiresBGRAConversion: Bool = false) {
        guard isRunning, let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        let buffer: CVPixelBuffer
        if requiresBGRAConversion {
            guard let converted = convertToBGRA(pixelBuffer) else { return }
            buffer = converted
        } else {
            buffer = pixelBuffer
        }

        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }

        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        let bytesPerRow = CVPixelBufferGetBytesPerRow(buffer)
        guard let basePtr = CVPixelBufferGetBaseAddress(buffer) else { return }

        let payloadLength = bytesPerRow * height
        let currentFrameID = frameID
        frameID &+= 1

        // 用 append 方式构造 Data：Data 类型对 append 操作做了正确的
        // copy-on-write 优化，buffer 在闭包外依然有效。
        // 帧头 28B 大端序：magic(4) + frame_id(4) + width(4) + height(4)
        //               + format(4) + bytes_per_row(4) + payload_length(4)
        var packet = Data(capacity: Self.headerSize + payloadLength)
        packet.append(contentsOf: Self.magic)                // magic "RAW1"
        packet.append(UInt32(currentFrameID).bigEndianData) // frame_id BE
        packet.append(UInt32(width).bigEndianData)          // width BE
        packet.append(UInt32(height).bigEndianData)         // height BE
        packet.append(UInt32(0).bigEndianData)              // format=0 BGRA
        packet.append(UInt32(bytesPerRow).bigEndianData)    // bytes_per_row BE
        packet.append(UInt32(payloadLength).bigEndianData)  // payload_length BE
        // payload：直接从 CVPixelBuffer 拷贝
        packet.append(basePtr.assumingMemoryBound(to: UInt8.self),
                      count: payloadLength)

        send(packet)
        // 前 5 帧强制打印（诊断），之后每 60 帧一次
        if currentFrameID < 5 || currentFrameID % 60 == 0 {
            let hex = packet.prefix(28).map { String(format: "%02x", $0) }.joined(separator: " ")
            print("RawStream: sent frame \(currentFrameID) (\(width)x\(height) payload=\(payloadLength)) header=\(hex)")
        }
    }

    private func send(_ data: Data) {
        connection?.send(content: data, completion: .contentProcessed { error in
            if let error = error {
                print("RawStream send error: \(error)")
            }
        })
    }

    /// 将任意格式的 CVPixelBuffer 转换为 BGRA。仅在采集格式非 BGRA 时使用。
    ///
    /// 注意：当前采集管道已配置 BGRA 输出（见 CaptureManager videoSettings），
    /// `processSampleBuffer` 默认以 `requiresBGRAConversion: false` 调用，
    /// 此方法仅为兜底。非 BGRA 输入将返回 nil 并丢弃该帧，避免引入易错的
    /// vImage Swift 桥接调用；如未来确需支持非 BGRA 采集，应在此处实现并
    /// 在真机验证后再合入。
    private func convertToBGRA(_ source: CVPixelBuffer) -> CVPixelBuffer? {
        let srcFormat = CVPixelBufferGetPixelFormatType(source)
        if srcFormat == kCVPixelFormatType_32BGRA {
            return source
        }
        print("RawStreamServer: non-BGRA pixel format \(srcFormat) not supported, dropping frame")
        return nil
    }
}
