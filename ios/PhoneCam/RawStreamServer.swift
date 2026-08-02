import Foundation
import Network
import CoreMedia
import CoreVideo

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

    /// 连接到桌面端 TCP 端口。
    func start(host: String, port: UInt16) {
        guard !isRunning else { return }
        let endpoint = NWEndpoint.hostPort(host: NWEndpoint.Host(host),
                                            port: NWEndpoint.Port(integerLiteral: port))
        let params = NWParameters.tcp
        let conn = NWConnection(to: endpoint, using: params)
        conn.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                print("RawStream TCP connection ready -> \(host):\(port)")
                self?.isRunning = true
            case .failed(let err):
                print("RawStream TCP connection failed: \(err)")
                self?.isRunning = false
            case .cancelled:
                self?.isRunning = false
            default:
                break
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
        guard let baseAddress = CVPixelBufferGetBaseAddress(buffer) else { return }

        let payloadLength = bytesPerRow * height
        let currentFrameID = frameID
        frameID &+= 1

        // 单次 send 合并帧头与 payload，避免两次 send 导致接收方读到的字节流交错
        var packet = Data(capacity: Self.headerSize + payloadLength)
        packet.append(contentsOf: Self.magic)
        packet.append(contentsOf: withUnsafeBytes(of: currentFrameID.bigEndian) { Array($0) })
        packet.append(contentsOf: withUnsafeBytes(of: UInt32(width).bigEndian) { Array($0) })
        packet.append(contentsOf: withUnsafeBytes(of: UInt32(height).bigEndian) { Array($0) })
        packet.append(contentsOf: withUnsafeBytes(of: UInt32(0).bigEndian) { Array($0) }) // format=0 BGRA
        packet.append(contentsOf: withUnsafeBytes(of: UInt32(bytesPerRow).bigEndian) { Array($0) })
        packet.append(contentsOf: withUnsafeBytes(of: UInt32(payloadLength).bigEndian) { Array($0) })

        let chunkData = Data(bytes: baseAddress, count: payloadLength)
        packet.append(chunkData)

        send(packet)
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
