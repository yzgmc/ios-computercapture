import Foundation
import QuartzCore
import VideoToolbox
import CoreVideo
import CoreMedia

/// 编码质量预设。决定 profile / 码率系数 / 熵编码方式。
/// - low: Main profile + CAVLC + 0.05 bpp，带宽紧张场景
/// - medium: Main profile + CABAC + 0.10 bpp，默认平衡
/// - high: High profile + CABAC + 0.15 bpp，高保真（USB/Wi-Fi6 推荐）
enum H264Quality: String, CaseIterable {
    case low, medium, high

    /// bits-per-pixel 系数。最终码率 = width * height * fps * bpp。
    var bpp: Double {
        switch self {
        case .low: return 0.05
        case .medium: return 0.10
        case .high: return 0.15
        }
    }

    var profileLevel: CFString {
        switch self {
        case .low, .medium:
            return kVTProfileLevel_H264_Main_AutoLevel
        case .high:
            return kVTProfileLevel_H264_High_AutoLevel
        }
    }

    var label: String {
        switch self {
        case .low: return "低 (省流)"
        case .medium: return "中 (平衡)"
        case .high: return "高 (高保真)"
        }
    }
}

/// H.264 硬件编码器封装（VTCompressionSession）。
///
/// 输入：AVCaptureVideoDataOutput 输出的 CVPixelBuffer（BGRA）。
/// 输出：Annex-B 格式 H.264 NAL 字节流（每个 encode 调用对应一个 Access Unit，
///       关键帧 AU 包含 SPS+PPS+IDR，P 帧 AU 仅含 P-slice）。
///
/// 设计要点（v2 优化版）：
/// - 实时优先：关闭 B 帧（AllowFrameReordering=false）、RealTime=true、AllowOpenGOP=false；
/// - 硬件加速：显式启用 VideoToolbox 硬件编码器；
/// - 高效压缩：medium/high 使用 CABAC 熵编码（仅 High/Main profile 支持）；
/// - GOP=60 帧（1 秒 @60fps），保证快速重同步与低延迟；
/// - 动态码率：runtime 通过 setBitrate() 调整，无需重建 session；
/// - 强制关键帧：通过 VTEncodeFrameOptionKey_ForceKeyFrame 在流切换/断线重连时立即 IDR；
/// - 输出 AVCC → 转 Annex-B（startCode 00 00 00 01）便于桌面端 PyAV/ffmpeg 解码。
final class H264Encoder {
    private var session: VTCompressionSession?
    private var width: Int = 0
    private var height: Int = 0
    private var fps: Int = 60
    private var quality: H264Quality = .medium
    /// 当前平均码率（bps），用于动态调整与统计。
    private(set) var currentBitrate: Int = 0
    /// 当前 GOP（帧数）。
    private var gopSize: Int = 60

    /// 编码完成回调：data 是一个 Access Unit（Annex-B），isKeyframe 标识是否含 SPS/PPS+IDR。
    var onFrame: ((Data, Bool) -> Void)?

    /// 初始化/重置编码器。调用时机：采集启动后已知实际分辨率时。
    /// - Parameters:
    ///   - quality: 质量预设，决定 profile/bpp/熵编码
    ///   - forceRecreate: 强制重建 session（profile 切换时必须）
    func configure(width: Int, height: Int, fps: Int,
                   quality: H264Quality = .medium,
                   forceRecreate: Bool = false) {
        // 已配置且尺寸/质量未变 → 跳过
        if !forceRecreate,
           self.session != nil,
           self.width == width,
           self.height == height,
           self.quality == quality {
            // fps 变化也只需更新 ExpectedFrameRate
            if self.fps != fps {
                self.fps = fps
                if let s = session {
                    VTSessionSetProperty(s, key: kVTCompressionPropertyKey_ExpectedFrameRate,
                                         value: fps as CFTypeRef)
                }
            }
            return
        }

        // profile 切换必须重建 session（High ↔ Main）
        if self.session != nil && self.quality != quality {
            teardown()
        } else if self.session != nil && (self.width != width || self.height != height) {
            teardown()
        } else if forceRecreate {
            teardown()
        }

        guard width > 0 && height > 0 else { return }
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality

        // 显式要求硬件编码器（VideoToolbox 在支持时自动选择，但显式声明可避免某些设备走软编）。
        // 不指定 RequiredEncoderID：硬编码 ID 在不同设备/iOS 版本可能不一致，
        // 让 VideoToolbox 自行挑选硬件编码器更稳健；不可用时下方会回退到默认 spec。
        let encoderSpec: [CFString: Any] = [
            kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder: true,
        ]
        var session: VTCompressionSession?
        let status = VTCompressionSessionCreate(
            allocator: kCFAllocatorDefault,
            width: Int32(width),
            height: Int32(height),
            codecType: kCMVideoCodecType_H264,
            encoderSpecification: encoderSpec as CFDictionary,
            imageBufferAttributes: nil,
            compressedDataAllocator: nil,
            outputCallback: nil,
            refcon: nil,
            compressionSessionOut: &session
        )
        guard status == noErr, let session = session else {
            // 硬件编码器不可用时回退到默认（让 VideoToolbox 自行选择，可能走软编）
            print("H264Encoder: hardware encoder unavailable (\(status)), retry with default")
            let fallbackStatus = VTCompressionSessionCreate(
                allocator: kCFAllocatorDefault,
                width: Int32(width),
                height: Int32(height),
                codecType: kCMVideoCodecType_H264,
                encoderSpecification: nil,
                imageBufferAttributes: nil,
                compressedDataAllocator: nil,
                outputCallback: nil,
                refcon: nil,
                compressionSessionOut: &session
            )
            guard fallbackStatus == noErr, let session = session else {
                print("H264Encoder: VTCompressionSessionCreate failed status=\(fallbackStatus)")
                return
            }
            applyProperties(to: session)
            self.session = session
            print("H264Encoder: configured \(width)x\(height) @ \(fps)fps quality=\(quality.rawValue) bitrate=\(currentBitrate/1000)kbps (fallback)")
            return
        }
        applyProperties(to: session)
        self.session = session
        print("H264Encoder: configured \(width)x\(height) @ \(fps)fps quality=\(quality.rawValue) bitrate=\(currentBitrate/1000)kbps")
    }

    /// 应用所有编码属性到 session。configure / 质量切换时调用。
    private func applyProperties(to session: VTCompressionSession) {
        // 实时低延迟配置
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_RealTime,
                             value: kCFBooleanTrue as CFTypeRef)
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_ProfileLevel,
                             value: quality.profileLevel as CFTypeRef)
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_AllowFrameReordering,
                             value: kCFBooleanFalse as CFTypeRef)
        // 关闭 OpenGOP：每个关键帧都是 IDR，断线重连后可立即重同步（不依赖前向参考）
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_AllowOpenGOP,
                             value: kCFBooleanFalse as CFTypeRef)
        // 熵编码：CABAC 比 CAVLC 节省 5-10% 码率（High/Main profile 支持，Baseline 不支持）
        // low 质量仍用 CAVLC 兼容 Baseline-ish 设备，medium/high 用 CABAC
        if quality != .low {
            VTSessionSetProperty(session, key: kVTCompressionPropertyKey_H264EntropyMode,
                                 value: kVTH264EntropyMode_CABAC as CFTypeRef)
        }
        // GOP：60 帧（@60fps = 1 秒），关键帧间隔过短会爆码率，过长断线重连慢
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_MaxKeyFrameInterval,
                             value: gopSize as CFTypeRef)
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration,
                             value: 1.0 as CFTypeRef)
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_ExpectedFrameRate,
                             value: fps as CFTypeRef)
        // 码率计算：bits-per-pixel 模型，按分辨率面积 × fps × bpp 系数
        // 1080p60 medium = 1920*1080*60*0.10 ≈ 12.4 Mbps
        let bitrate = computeBitrate(quality: quality, width: width, height: height, fps: fps)
        currentBitrate = bitrate
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_AverageBitRate,
                             value: bitrate as CFTypeRef)
        // 码率上限：1.5x 平均码率（字节/秒），避免突发导致延迟尖峰
        let limitBytesPerSecond = Int(Double(bitrate) * 1.5 / 8.0)
        let limitArray = [bitrate, limitBytesPerSecond] as CFArray
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_DataRateLimits,
                             value: limitArray as CFTypeRef)
        // 电池优先：选择更省电的编码路径（不影响实时性）
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_MaximizePowerEfficiency,
                             value: kCFBooleanTrue as CFTypeRef)
        // 准备就绪（部分属性在 PrepareToEncodeFrames 后才完全生效）
        VTCompressionSessionPrepareToEncodeFrames(session)
    }

    /// 计算 bpp 模型码率，单位 bps。
    private func computeBitrate(quality: H264Quality, width: Int, height: Int, fps: Int) -> Int {
        let pixels = Double(width * height)
        let raw = pixels * Double(fps) * quality.bpp
        // 钳制：最低 1 Mbps（避免极低分辨率画质崩坏），最高 25 Mbps（避免 Wi-Fi 5 拥塞）
        return Int(max(1_000_000, min(raw, 25_000_000)))
    }

    /// 动态调整码率（无需重建 session）。
    /// 用于带宽自适应：当网络拥塞时下调，空闲时回升。
    func setBitrate(_ bitrate: Int) {
        guard let session = session else { return }
        let clamped = max(500_000, min(bitrate, 30_000_000))
        guard clamped != currentBitrate else { return }
        currentBitrate = clamped
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_AverageBitRate,
                             value: clamped as CFTypeRef)
        // DataRateLimits 同步调整，保持 1.5x 上限
        let limitBytesPerSecond = Int(Double(clamped) * 1.5 / 8.0)
        let limitArray = [clamped, limitBytesPerSecond] as CFArray
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_DataRateLimits,
                             value: limitArray as CFTypeRef)
    }

    /// 返回当前质量预设下的目标码率，自适应回升时不要超过此值。
    func targetBitrate() -> Int {
        return computeBitrate(quality: quality, width: width, height: height, fps: fps)
    }

    /// 下一帧强制输出 IDR 关键帧。
    /// 用于：流切换、客户端（重）连、解码器重置后、分辨率突变。
    func forceKeyframe() {
        pendingForceKeyframe = true
    }

    private var pendingForceKeyframe = false

    /// 编码一帧。pixelBuffer 通常来自 AVCaptureVideoDataOutput，格式 BGRA。
    func encode(_ pixelBuffer: CVPixelBuffer) {
        guard let session = session else { return }
        let pts = CMTime(value: CMTimeValue(CACurrentMediaTime() * 1000), timescale: 1000)

        // 强制关键帧：通过 frameProperties 传入 VTEncodeFrameOptionKey_ForceKeyFrame
        var frameProps: CFDictionary?
        if pendingForceKeyframe {
            let dict: [CFString: Any] = [
                VTEncodeFrameOptionKey_ForceKeyFrame: kCFBooleanTrue as Any
            ]
            frameProps = dict as CFDictionary
            pendingForceKeyframe = false
        }

        let status = VTCompressionSessionEncodeFrame(
            session,
            imageBuffer: pixelBuffer,
            presentationTimeStamp: pts,
            duration: CMTime.invalid,
            frameProperties: frameProps,
            infoFlagsOut: nil,
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
            VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: CMTime.positiveInfinity)
            VTCompressionSessionInvalidate(session)
            self.session = nil
        }
        width = 0
        height = 0
        currentBitrate = 0
        pendingForceKeyframe = false
    }

    deinit {
        teardown()
    }

    // MARK: - 内部

    /// VTCompressionSession 输出回调（在 videoQueue 上）。
    private func handleEncoded(_ sampleBuffer: CMSampleBuffer) {
        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer) else { return }
        let isKeyframe: Bool = {
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
            var totalCount: Int = 0
            var probePtr: UnsafePointer<UInt8>?
            var probeSize: Int = 0
            var probeNalLen: Int32 = 0
            let probeStatus = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                formatDesc,
                parameterSetIndex: 0,
                parameterSetPointerOut: &probePtr,
                parameterSetSizeOut: &probeSize,
                parameterSetCountOut: &totalCount,
                nalUnitHeaderLengthOut: &probeNalLen)
            guard probeStatus == noErr else {
                extractNALs(from: sampleBuffer, into: &annexB)
                if !annexB.isEmpty { onFrame?(annexB, isKeyframe) }
                return
            }
            for i in 0..<totalCount {
                var ptr: UnsafePointer<UInt8>?
                var size: Int = 0
                var count: Int = 0
                var nalLen: Int32 = 0
                let r = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                    formatDesc,
                    parameterSetIndex: i,
                    parameterSetPointerOut: &ptr,
                    parameterSetSizeOut: &size,
                    parameterSetCountOut: &count,
                    nalUnitHeaderLengthOut: &nalLen)
                guard r == noErr, let p = ptr else { break }
                annexB.append(Self.startCode)
                annexB.append(p, count: size)
            }
        }

        extractNALs(from: sampleBuffer, into: &annexB)

        if annexB.isEmpty { return }
        onFrame?(annexB, isKeyframe)
    }

    /// 从 sampleBuffer 的 CMBlockBuffer 提取 AVCC NALs，转换为 Annex-B 追加到 annexB。
    private func extractNALs(from sampleBuffer: CMSampleBuffer, into annexB: inout Data) {
        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
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
    }

    /// Annex-B 起始码：00 00 00 01
    private static let startCode: Data = Data([0x00, 0x00, 0x00, 0x01])
}
