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

    /// 实际采集参数（由 CMSampleBuffer 动态检测后更新，桌面端据此配置 PyAudio 流）
    private var sampleRate: UInt32 = 48000
    private var channels: UInt8 = 1
    private var format: UInt8 = AudioStreamServer.formatPCM16LE

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

    /// 处理一帧采集音频：从 CMSampleBuffer 抽取 PCM 字节、必要时转换格式、分片 UDP 发送。
    /// iOS 上 AVCaptureAudioDataOutput.audioSettings 不可用，因此格式由 sample buffer 动态检测。
    func processSampleBuffer(_ sampleBuffer: CMSampleBuffer) {
        guard isRunning else { return }

        // 1. 从 sample buffer 的 format description 检测实际音频格式
        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer) else { return }
        let avFormat = AVAudioFormat(cmAudioFormatDescription: formatDesc)

        let detectedSampleRate: Double = avFormat?.sampleRate ?? 44100
        let detectedChannels: AVAudioChannelCount = avFormat?.channelCount ?? 1
        let pcmFormat = avFormat?.pcmFormat ?? .int16

        // 2. 提取原始 PCM 字节
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

        // CMBlockBufferGetDataLength 新版 API 直接返回 Int，不再需要 totalLengthOut 参数
        let totalLength = CMBlockBufferGetDataLength(bb)
        guard totalLength > 0 else { return }

        var rawData = Data(count: totalLength)
        let readStatus = rawData.withUnsafeMutableBytes { (ptr: UnsafeMutableRawBufferPointer) -> OSStatus in
            guard let base = ptr.baseAddress else { return -1 }
            return CMBlockBufferCopyDataBytes(bb, atOffset: 0, dataLength: totalLength, destination: base)
        }
        guard readStatus == kCMBlockBufferNoErr else {
            print("AudioStream: copy data bytes failed: \(readStatus)")
            return
        }

        // 3. 格式转换：统一为 PCM16LE，确保桌面端兼容
        let pcmData: Data
        let outputFormat: UInt8

        switch pcmFormat {
        case .int16:
            // 已是 PCM16，直接使用（但需注意字节序和交错方式）
            pcmData = rawData
            outputFormat = Self.formatPCM16LE

        case .int32:
            // 32-bit PCM → 16-bit
            pcmData = convertInt32ToInt16(rawData)
            outputFormat = Self.formatPCM16LE

        case .float:
            // Float32 → Int16
            pcmData = convertFloatToInt16(rawData)
            outputFormat = Self.formatPCM16LE

        @unknown default:
            print("AudioStream: unsupported PCM format: \(pcmFormat.rawValue)")
            return
        }

        // 4. 更新协议字段（首次检测后固定，避免桌面端频繁重建 PyAudio 流）
        if self.sampleRate != UInt32(detectedSampleRate) ||
           self.channels != UInt8(detectedChannels) ||
           self.format != outputFormat {
            self.sampleRate = UInt32(detectedSampleRate)
            self.channels = UInt8(detectedChannels)
            self.format = outputFormat
            print("AudioStream: format detected — \(detectedSampleRate)Hz / \(detectedChannels)ch / PCM16LE")
        }

        // 5. 分片发送（每片独立 seq，便于桌面端诊断丢包）
        let chunkSize = AudioStreamServer.maxPayloadBytes
        var offset = 0
        while offset < pcmData.count {
            let end = min(offset + chunkSize, pcmData.count)
            let chunk = pcmData.subdata(in: offset..<end)
            send(chunk: chunk)
            offset = end
        }
    }

    // MARK: - 格式转换工具

    private func convertFloatToInt16(_ data: Data) -> Data {
        let floats: [Float] = data.withUnsafeBytes { ptr in
            Array(ptr.bindMemory(to: Float.self))
        }
        let int16s = floats.map { Int16(max(-32768, min(32767, $0 * 32768.0))) }
        return int16s.withUnsafeBufferPointer { Data(buffer: $0) }
    }

    private func convertInt32ToInt16(_ data: Data) -> Data {
        let int32s: [Int32] = data.withUnsafeBytes { ptr in
            Array(ptr.bindMemory(to: Int32.self))
        }
        let int16s = int32s.map { Int16(max(-32768, min(32767, $0 >> 16))) }
        return int16s.withUnsafeBufferPointer { Data(buffer: $0) }
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
