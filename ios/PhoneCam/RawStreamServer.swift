import Foundation
import Network
import CoreMedia
import CoreVideo

/// 原画质无压缩视频传输：将采集到的 CVPixelBuffer 以 BGRA 像素通过 UDP 分片发送到桌面端。
///
/// 协议帧头（大端序，28 字节）：
/// magic(4B)="RAW1" | frame_id(4B) | chunk_idx(4B) | total_chunks(4B)
/// | width(4B) | height(4B) | format(4B=0 BGRA)
///
/// 适用场景：局域网内对画质有无损要求；USB tethering 场景改用 TCP（见 transport 说明）。
final class RawStreamServer {
    static let headerSize = 32
    static let maxPayloadSize = 1400
    private static let magic: [UInt8] = [0x52, 0x41, 0x57, 0x49] // "RAW1"

    private var connection: NWConnection?
    private let queue = DispatchQueue(label: "com.yzg.phonecam.rawstream")
    private var frameID: UInt32 = 0
    private(set) var isRunning = false

    /// 连接到桌面端 UDP 端口。
    func start(host: String, port: UInt16) {
        guard !isRunning else { return }
        let endpoint = NWEndpoint.hostPort(host: NWEndpoint.Host(host),
                                            port: NWEndpoint.Port(integerLiteral: port))
        let params = NWParameters.udp
        let conn = NWConnection(to: endpoint, using: params)
        conn.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                print("RawStream UDP connection ready -> \(host):\(port)")
                self?.isRunning = true
            case .failed(let err):
                print("RawStream UDP connection failed: \(err)")
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

    /// 处理一帧采集数据，转 BGRA 后分片发送。仅在 isRunning 时生效。
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

        let frameBytes = bytesPerRow * height
        let payloadPerChunk = Self.maxPayloadSize
        let totalChunks = UInt32((frameBytes + payloadPerChunk - 1) / payloadPerChunk)

        let currentFrameID = frameID
        frameID &+= 1

        var offset = 0
        var chunkIdx: UInt32 = 0
        while offset < frameBytes {
            let length = min(payloadPerChunk, frameBytes - offset)
            var packet = Data(capacity: Self.headerSize + length)
            packet.append(contentsOf: Self.magic)
            packet.append(contentsOf: withUnsafeBytes(of: currentFrameID.bigEndian) { Array($0) })
            packet.append(contentsOf: withUnsafeBytes(of: chunkIdx.bigEndian) { Array($0) })
            packet.append(contentsOf: withUnsafeBytes(of: totalChunks.bigEndian) { Array($0) })
            packet.append(contentsOf: withUnsafeBytes(of: UInt32(width).bigEndian) { Array($0) })
            packet.append(contentsOf: withUnsafeBytes(of: UInt32(height).bigEndian) { Array($0) })
            packet.append(contentsOf: withUnsafeBytes(of: UInt32(0).bigEndian) { Array($0) }) // format=0 BGRA
            packet.append(contentsOf: withUnsafeBytes(of: UInt32(bytesPerRow).bigEndian) { Array($0) }) // bytes_per_row

            let chunkData = Data(bytes: baseAddress.advanced(by: offset), count: length)
            packet.append(chunkData)

            send(packet)

            offset += length
            chunkIdx &+= 1
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
