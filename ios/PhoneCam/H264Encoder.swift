import Foundation
import VideoToolbox
import CoreVideo
import CoreMedia

/// H.264 硬件编码器封装（VTCompressionSession）。
///
/// 输入：AVCaptureVideoDataOutput 输出的 CVPixelBuffer（BGRA）。
/// 输出：Annex-B 格式 H.264 NAL 字节流（每个 encode 调用对应一个 Access Unit，
///       关键帧 AU 包含 SPS+PPS+IDR，P 帧 AU 仅含 P-slice）。
///
/// 设计要点：
/// - 实时优先：关闭 B 帧（AllowFrameReordering=false）、开启 RealTime；
/// - 硬件优先：VTCompressionSession 自动选择 VideoToolbox 硬件编码器；
/// - GOP=60 帧（1 秒 @60fps），保证快速重同步与低延迟；
/// - 输出 NAL 用 4 字节长度前缀（AVCC）→ 转换为 Annex-B 起始码 00 00 00 01，
///   便于桌面端 ffmpeg/PyAV 直接解码。
final class H264Encoder {
    private var session: VTCompressionSession?
    private var width: Int = 0
    private var height: Int = 0

    /// 编码完成回调：data 是一个 Access Unit（Annex-B），isKeyframe 标识是否含 SPS/PPS+IDR。
    var onFrame: ((Data, Bool) -> Void)?

    /// 初始化/重置编码器。调用时机：采集启动后已知实际分辨率时。
    /// - Parameters:
    ///   - width: 像素宽（必须与 CVPixelBuffer 一致）
    ///   - height: 像素高
    ///   - fps: 期望帧率（用于码率估算与 ExpectedFrameRate）
    func configure(width: Int, height: Int, fps: Int) {
        if self.session != nil && self.width == width && self.height == height {
            return  // 已配置且尺寸未变
        }
        teardown()
        guard width > 0 && height > 0 else { return }
        self.width = width
        self.height = height

        var session: VTCompressionSession?
        // outputCallback 传 nil，使用 VTCompressionSessionEncodeFrame 的 outputHandler
        // （Swift 友好的闭包回调，避免 C 函数指针 + Unmanaged 转换的样板代码）。
        let status = VTCompressionSessionCreate(
            allocator: kCFAllocatorDefault,
            width: Int32(width),
            height: Int32(height),
            codecType: kCMVideoCodecType_H264,
            encoderSpecification: nil,
            imageBufferAttributes: nil,
            compressedDataAllocator: nil,
            outputCallback: nil,
            refCon: nil,
            sessionOut: &session
        )
        guard status == noErr, let session = session else {
            print("H264Encoder: VTCompressionSessionCreate failed status=\(status)")
            return
        }

        // 实时低延迟配置
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_RealTime, value: kCFBooleanTrue as CFTypeRef)
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_ProfileLevel,
                             value: kVTProfileLevel_H264_Main_AutoLevel as CFTypeRef)
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_AllowFrameReordering,
                             value: kCFBooleanFalse as CFTypeRef)
        // GOP：60 帧（@60fps = 1 秒），关键帧间隔过短会爆码率，过长断线重连慢
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_MaxKeyFrameInterval,
                             value: 60 as CFTypeRef)
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration,
                             value: 1.0 as CFTypeRef)
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_ExpectedFrameRate,
                             value: fps as CFTypeRef)
        // 码率：1080p60 实时一般 6-10 Mbps，按分辨率面积线性缩放
        let pixels = Double(width * height)
        let bitrate = max(1_000_000, Int(pixels / 2073600.0 * 8_000_000))
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_AverageBitRate,
                             value: bitrate as CFTypeRef)
        // 码率上限：1.5x 平均码率（字节/秒），避免突发导致延迟尖峰
        let limitBytesPerSecond = Int(Double(bitrate) * 1.5 / 8.0)
        let limitArray = [bitrate, limitBytesPerSecond] as CFArray
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_DataRateLimits,
                             value: limitArray as CFTypeRef)

        let prepareStatus = VTSessionPrepareToEncodeFrames(session)
        if prepareStatus != noErr {
            print("H264Encoder: prepare failed status=\(prepareStatus)")
        }

        self.session = session
        print("H264Encoder: configured \(width)x\(height) @ \(fps)fps bitrate=\(bitrate/1000)kbps")
    }

    /// 编码一帧。pixelBuffer 通常来自 AVCaptureVideoDataOutput，格式 BGRA。
    /// - Parameter forceKeyframe: 强制输出 IDR（用于起流/断线重连后首帧）。
    func encode(_ pixelBuffer: CVPixelBuffer, forceKeyframe: Bool = false) {
        guard let session = session else { return }
        if forceKeyframe {
            VTSessionSetProperty(session, key: kVTCompressionPropertyKey_ForceKeyFrame,
                                 value: 1 as CFTypeRef)
        }
        // PTS 用 CACurrentMediaTime 毫秒级单调递增；实时流不依赖 DTS/PTS 严格顺序，
        // 编码器只需时间戳做码率控制与 GOP 时长计算。
        let pts = CMTime(value: CMTimeValue(CACurrentMediaTime() * 1000), timescale: 1000)
        var info = VTEncodeFrameInfoFlags(rawValue: 0)
        let status = VTCompressionSessionEncodeFrame(
            session,
            imageBuffer: pixelBuffer,
            presentationTimeStamp: pts,
            duration: CMTime.invalid,
            frameProperties: nil,
            infoFlagsOut: &info,
            outputHandler: { [weak self] _, _, sampleBuffer in
                guard let sampleBuffer = sampleBuffer else { return }
                self?.handleEncoded(sampleBuffer)
            }
        )
        if status != noErr {
            print("H264Encoder: encode failed status=\(status)")
        }
    }

    /// 释放编码器。下次 configure 后可重新使用。
    func teardown() {
        if let session = session {
            VTCompressionSessionCompleteFrames(session, until: CMTime.positiveInfinity)
            VTCompressionSessionInvalidate(session)
            self.session = nil
        }
        width = 0
        height = 0
    }

    deinit {
        teardown()
    }

    // MARK: - 内部

    /// VTCompressionSession 输出回调（在 videoQueue 上）。
    /// sampleBuffer 包含一个 Access Unit（一个或多个 NAL，4 字节长度前缀 AVCC 格式）。
    private func handleEncoded(_ sampleBuffer: CMSampleBuffer) {
        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer) else { return }
        let isKeyframe: Bool = {
            // kCMSampleAttachmentKey_NotSync=true 表示非关键帧（P/B 帧）
            if let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[CFString: Any]],
               attachments.count > 0 {
                let notSync = attachments[0][kCMSampleAttachmentKey_NotSync] as? Bool ?? false
                return !notSync
            }
            return false
        }()

        var annexB = Data()
        // 关键帧前输出 SPS/PPS（让接收端无需依赖前置参数集）
        if isKeyframe {
            for i in 0..<2 {
                var ptr: UnsafePointer<UInt8>?
                var size: Int = 0
                var type: VideoToolbox.VTFormatDescriptionType = []
                var desc: CMVideoFormatDescription?
                let r = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                    formatDesc,
                    parameterSetIndex: i,
                    parameterSetPointerOut: &ptr,
                    parameterSetSizeOut: &size,
                    parameterSetTypeOut: &type,
                    formatDescriptionOut: &desc)
                guard r == noErr, let p = ptr else { break }
                annexB.append(Self.startCode)
                annexB.append(p, count: size)
            }
        }

        // 从 CMBlockBuffer 提取 AVCC NALs，转换每条为 Annex-B
        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else {
            if !annexB.isEmpty { onFrame?(annexB, isKeyframe) }
            return
        }
        let totalLength = CMBlockBufferGetDataLength(blockBuffer)
        var data = Data(count: totalLength)
        data.withUnsafeMutableBytes { (rawBuf: UnsafeMutableRawBufferPointer) in
            _ = CMBlockBufferCopyDataBytes(blockBuffer, atOffset: 0, dataLength: totalLength,
                                           destination: rawBuf.baseAddress!)
        }

        // AVCC 格式：[4B big-endian length | NAL data] 重复
        var offset = 0
        while offset + 4 <= data.count {
            let length = data.subdata(in: offset..<(offset + 4))
                .withUnsafeBytes { $0.load(as: UInt32.self).bigEndian }
            let nalStart = offset + 4
            let nalEnd = nalStart + Int(length)
            guard nalEnd <= data.count else { break }
            annexB.append(Self.startCode)
            annexB.append(data[nalStart..<nalEnd])
            offset = nalEnd
        }

        if annexB.isEmpty { return }
        onFrame?(annexB, isKeyframe)
    }

    /// Annex-B 起始码：00 00 00 01
    private static let startCode: Data = Data([0x00, 0x00, 0x00, 0x01])
}
