import Foundation
import Network
import AVFoundation
import CoreAudio

/// 独立 UDP 音频通道：将麦克风采集的 PCM 数据通过 UDP 发送到桌面端。
///
/// 协议帧头（大端序，16 字节）：
/// magic(4B)="AUD1" | seq(4B) | sample_rate(4B) | channels(1B)
/// | format(1B=0 PCM16LE) | payload_length(2B)
///
/// UDP 不保证可靠性，丢包由桌面端播放器自然吸收（短暂静音或跳变）。
/// 单包 payload 上限 1456 字节（含 16B 帧头不超 1472B，适配典型 MTU 1500）。
final class AudioStreamServer {
    static let headerSize = 16
    private static let magic: [UInt8] = [0x41, 0x55, 0x44, 0x31]  // "AUD1"
    static let maxPayloadBytes = 1456

    static let formatPCM16LE: UInt8 = 0
    static let formatPCMFloat32LE: UInt8 = 1

    private var connection: NWConnection?
    private let queue = DispatchQueue(label: "com.yzg.phonecam.audiostream")
    private var seq: UInt32 = 0
    private(set) var isRunning = false

    /// 当前 PCM 格式（与 AVCaptureAudioDataOutput.audioSettings 对应）。
    /// 桌面端据此打开 PyAudio 输出流；格式变化时桌面端会重建流。
    private let sampleRate: UInt32 = 48000
    private let channels: UInt8 = 1
    private let format: UInt8 = AudioStreamServer.formatPCM16LE

    /// 连接到桌面端 UDP 端口（默认 5001）。
    func start(host: String, port: UInt16) {
        guard !isRunning else { return }
        let endpoint = NWEndpoint.hostPort(host: NWEndpoint.Host(host),
                                            port: NWEndpoint.Port(integerLiteral: port))
        let params = NWParameters.udp
        let conn = NWConnection(to: endpoint, using: params)
        conn.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                print("AudioStream UDP connection ready -> \(host):\(port)")
                self?.isRunning = true
            case .failed(let err):
                print("AudioStream UDP connection failed: \(err)")
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
        seq = 0
    }

    /// 处理一帧采集音频：从 CMSampleBuffer 抽取 PCM 字节并分片发送。
    /// 假设 audioSettings 已配置为 48kHz / mono / 16-bit PCM interleaved。
    func processSampleBuffer(_ sampleBuffer: CMSampleBuffer) {
        guard isRunning else { return }

        var blockBuffer: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: nil,
            bufferListSize: 0,
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: 0,
            blockBufferOut: &blockBuffer
        )
        guard status == kCMBlockBufferNoErr, let bb = blockBuffer else {
            print("AudioStream: extract block buffer failed: \(status)")
            return
        }

        var totalLength = 0
        let lengthStatus = CMBlockBufferGetDataLength(bb, totalLengthOut: &totalLength)
        guard lengthStatus == kCMBlockBufferNoErr, totalLength > 0 else {
            return
        }

        var data = Data(count: totalLength)
        let readStatus = data.withUnsafeMutableBytes { (ptr: UnsafeMutableRawBufferPointer) -> OSStatus in
            guard let base = ptr.baseAddress else { return -1 }
            return CMBlockBufferCopyDataBytes(bb, atOffset: 0, dataLength: totalLength, destination: base)
        }
        guard readStatus == kCMBlockBufferNoErr else {
            print("AudioStream: copy data bytes failed: \(readStatus)")
            return
        }

        // PCM16 mono 48kHz：每帧 2 字节；MTU 内最多 1456 字节 = 728 采样 ≈ 15ms。
        // 大块按 maxPayloadBytes 切片发送，每片带独立 seq 便于桌面端诊断丢包。
        let chunkSize = AudioStreamServer.maxPayloadBytes
        var offset = 0
        while offset < data.count {
            let end = min(offset + chunkSize, data.count)
            let chunk = data.subdata(in: offset..<end)
            send(chunk: chunk)
            offset = end
        }
    }

    private func send(chunk: Data) {
        guard let connection = connection else { return }

        let currentSeq = seq
        seq &+= 1

        var packet = Data(capacity: AudioStreamServer.headerSize + chunk.count)
        packet.append(contentsOf: AudioStreamServer.magic)
        packet.append(contentsOf: withUnsafeBytes(of: currentSeq.bigEndian) { Array($0) })
        packet.append(contentsOf: withUnsafeBytes(of: sampleRate.bigEndian) { Array($0) })
        packet.append(channels)
        packet.append(format)
        let payloadLength = UInt16(chunk.count)
        packet.append(contentsOf: withUnsafeBytes(of: payloadLength.bigEndian) { Array($0) })
        packet.append(chunk)

        connection.send(content: packet, completion: .contentProcessed { error in
            if let error = error {
                print("AudioStream send error: \(error)")
            }
        })
    }
}
