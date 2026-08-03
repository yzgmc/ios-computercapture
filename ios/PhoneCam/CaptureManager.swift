import AVFoundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers
import UIKit
import VideoToolbox

enum CaptureResolution: CaseIterable, Identifiable {
    case p4K, p2K, p1080, p720, p480, p240

    var id: Self { self }

    var label: String {
        switch self {
        case .p4K: return "4K"
        case .p2K: return "2K"
        case .p1080: return "1080p"
        case .p720: return "720p"
        case .p480: return "480p"
        case .p240: return "240p"
        }
    }

    var size: CGSize {
        switch self {
        case .p4K: return CGSize(width: 3840, height: 2160)
        case .p2K: return CGSize(width: 2560, height: 1440)
        case .p1080: return CGSize(width: 1920, height: 1080)
        case .p720: return CGSize(width: 1280, height: 720)
        case .p480: return CGSize(width: 640, height: 480)
        case .p240: return CGSize(width: 320, height: 240)
        }
    }
}

/// 视频编码方式（决定 RawStreamServer 发送的 format 字段）。
/// - h264: H.264 硬件编码（VTCompressionSession），1080p60 稳定、带宽 ~8Mbps，推荐。
/// - jpeg: JPEG 85 软件编码（ImageIO），中等负载，带宽 ~15-20Mbps。
/// - bgra: BGRA 无压缩，带宽极高（1080p60 ≈ 500MB/s），仅千兆/USB3 可用。
enum VideoCodec: String, CaseIterable, Identifiable {
    case h264, jpeg, bgra
    var id: Self { self }
    var label: String {
        switch self {
        case .h264: return "H.264 硬件编码 (1080p60 推荐)"
        case .jpeg: return "JPEG 85"
        case .bgra: return "BGRA 无压缩"
        }
    }
}

class CaptureManager: NSObject, ObservableObject {
    @Published var selectedResolution: CaptureResolution = .p720 {
        didSet { Task { await reconfigureCapture() } }
    }
    @Published var fps: Int = 60 {
        didSet { Task { await applyFps() } }
    }
    @Published var volume: Double = 1.0
    @Published var flipHorizontal: Bool = false
    @Published var flipVertical: Bool = false
    // 摄像头实际输出尺寸（由 activeFormat 决定），用于预览按真实比例渲染
    @Published var actualCaptureSize: CGSize = CGSize(width: 1280, height: 720)
    /// 视频编码方式：H.264 硬件编码（推荐）/ JPEG / BGRA 无压缩
    @Published var videoCodec: VideoCodec = .h264 {
        didSet { Task { await reconfigureEncoder() } }
    }

    private let captureSession = AVCaptureSession()
    private var videoDeviceInput: AVCaptureDeviceInput?
    private var videoOutput: AVCaptureVideoDataOutput?
    private var audioOutput: AVCaptureAudioDataOutput?
    private var previewLayer: AVCaptureVideoPreviewLayer?

    /// JPEG 编码复用的 CIContext（避免每帧重建）
    private let ciContext = CIContext(options: [.useSoftwareRenderer: false])
    /// H.264 硬件编码器（按实际采集尺寸/fps 配置，懒初始化）
    private let h264Encoder = H264Encoder()
    /// H.264 起流后首帧需强制 IDR，让接收端解码器立即同步
    private var h264NeedsKeyframe = true

    /// 原画质传输模块，启用后每帧视频采样都会转发（BGRA 无压缩 TCP）
    var rawStreamServer: RawStreamServer?
    /// UDP 音频传输模块，启用后每帧音频采样都会转发（PCM 无压缩 UDP）
    var audioStreamServer: AudioStreamServer?

    override init() {
        super.init()
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        UIDevice.current.endGeneratingDeviceOrientationNotifications()
    }

    func setupPreview(in view: UIView) {
        let previewLayer = AVCaptureVideoPreviewLayer(session: captureSession)
        previewLayer.videoGravity = .resizeAspect
        previewLayer.frame = view.bounds
        previewLayer.connection?.videoOrientation = currentVideoOrientation()
        view.layer.addSublayer(previewLayer)
        self.previewLayer = previewLayer

        UIDevice.current.beginGeneratingDeviceOrientationNotifications()
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(orientationDidChange),
            name: UIDevice.orientationDidChangeNotification,
            object: nil
        )
    }

    @objc private func orientationDidChange() {
        guard let previewLayer = previewLayer else { return }
        previewLayer.connection?.videoOrientation = currentVideoOrientation()
    }

    private func currentVideoOrientation() -> AVCaptureVideoOrientation {
        let orientation = UIDevice.current.orientation
        switch orientation {
        case .portrait: return .portrait
        case .portraitUpsideDown: return .portraitUpsideDown
        case .landscapeLeft: return .landscapeRight
        case .landscapeRight: return .landscapeLeft
        default: return .portrait
        }
    }

    func updatePreviewFrame(to bounds: CGRect) {
        guard let previewLayer = previewLayer else { return }
        previewLayer.frame = bounds
    }

    func startCapture() async {
        guard await requestAuthorization() else {
            print("未获得摄像头/麦克风权限")
            return
        }

        captureSession.beginConfiguration()

        // 视频：后置摄像头 + BGRA 输出
        do {
            guard let videoDevice = AVCaptureDevice.default(.builtInWideAngleCamera,
                                                            for: .video,
                                                            position: .back) else {
                print("无法获取后置摄像头")
                return
            }

            try videoDevice.lockForConfiguration()
            let dimensions = selectedResolution.size
            if let format = Self.selectBestFormat(in: videoDevice.formats,
                                                   targetSize: dimensions,
                                                   targetFps: Double(fps)) {
                videoDevice.activeFormat = format
                captureSession.sessionPreset = .inputPriority

                // 记录实际采集尺寸，供 UI 按真实比例显示预览
                let dims = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
                print("Selected format: \(dims.width)x\(dims.height)")
                await MainActor.run {
                    self.actualCaptureSize = CGSize(width: CGFloat(dims.width), height: CGFloat(dims.height))
                }

                // 将期望 fps 钳制到当前 activeFormat 支持的帧率范围，避免 4K/2K + 60fps 崩溃
                let clampedFps = Self.clampFps(Double(fps), format: format)
                if clampedFps != Double(fps) {
                    print("FPS clamped: requested \(fps) -> supported \(clampedFps)")
                    await MainActor.run {
                        self.fps = Int(clampedFps)
                    }
                }
                let frameDuration = CMTime(value: 1, timescale: CMTimeScale(clampedFps))
                videoDevice.activeVideoMinFrameDuration = frameDuration
                videoDevice.activeVideoMaxFrameDuration = frameDuration
            }
            videoDevice.unlockForConfiguration()

            let videoInput = try AVCaptureDeviceInput(device: videoDevice)
            if captureSession.canAddInput(videoInput) {
                captureSession.addInput(videoInput)
                videoDeviceInput = videoInput
            }

            let videoOutput = AVCaptureVideoDataOutput()
            // 统一输出 BGRA，供原画质 TCP 模块直接发送
            videoOutput.videoSettings = [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
            ]
            // 丢帧策略：实时性优先，背压由 RawStreamServer 处理
            videoOutput.alwaysDiscardsLateVideoFrames = true
            // 专用串行队列处理视频帧，避免阻塞主线程
            videoOutput.setSampleBufferDelegate(self, queue: DispatchQueue(label: "videoQueue", qos: .userInitiated))
            if captureSession.canAddOutput(videoOutput) {
                captureSession.addOutput(videoOutput)
                self.videoOutput = videoOutput
            }
        } catch {
            print("视频配置失败: \(error)")
        }

        // 音频：麦克风 + 48kHz mono 16-bit PCM 输出（供 UDP 模块直接发送）
        do {
            guard let audioDevice = AVCaptureDevice.default(for: .audio) else {
                print("无法获取麦克风")
                return
            }
            let audioInput = try AVCaptureDeviceInput(device: audioDevice)
            if captureSession.canAddInput(audioInput) {
                captureSession.addInput(audioInput)
            }

            let audioOutput = AVCaptureAudioDataOutput()
            // iOS 上 audioSettings 不可用（仅 macOS 支持），格式由 AudioStreamServer 动态检测与转换
            audioOutput.setSampleBufferDelegate(self, queue: DispatchQueue(label: "audioQueue"))
            if captureSession.canAddOutput(audioOutput) {
                captureSession.addOutput(audioOutput)
                self.audioOutput = audioOutput
            }
        } catch {
            print("音频配置失败: \(error)")
        }

        captureSession.commitConfiguration()

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.captureSession.startRunning()
        }
    }

    /// 分辨率变更时重新配置采集格式。仅在采集已启动时生效。
    private func reconfigureCapture() async {
        guard let input = videoDeviceInput, captureSession.isRunning else { return }
        let device = input.device
        do {
            try device.lockForConfiguration()
            let dimensions = selectedResolution.size
            if let format = Self.selectBestFormat(in: device.formats,
                                                   targetSize: dimensions,
                                                   targetFps: Double(fps)) {
                device.activeFormat = format
                let clampedFps = Self.clampFps(Double(fps), format: format)
                let frameDuration = CMTime(value: 1, timescale: CMTimeScale(clampedFps))
                device.activeVideoMinFrameDuration = frameDuration
                device.activeVideoMaxFrameDuration = frameDuration
                let dims = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
                await MainActor.run {
                    self.actualCaptureSize = CGSize(width: CGFloat(dims.width), height: CGFloat(dims.height))
                }
                print("Reconfigured capture: \(dims.width)x\(dims.height) @ \(clampedFps)fps")
            }
            device.unlockForConfiguration()
        } catch {
            print("Reconfigure capture failed: \(error)")
        }
    }

    /// 帧率变更时更新帧率；若当前 format 不支持目标帧率则切换 format（如 4K@30 → 4K@60）。
    private func applyFps() async {
        guard let input = videoDeviceInput, captureSession.isRunning else { return }
        let device = input.device
        do {
            try device.lockForConfiguration()
            // 若当前 format 不支持目标帧率，重新选择同分辨率下支持该帧率的 format
            let currentFormat = device.activeFormat
            let supportsFps = currentFormat.videoSupportedFrameRateRanges.contains { range in
                Double(fps) >= range.minFrameRate && Double(fps) <= range.maxFrameRate
            }
            if !supportsFps {
                let dimensions = selectedResolution.size
                if let format = Self.selectBestFormat(in: device.formats,
                                                       targetSize: dimensions,
                                                       targetFps: Double(fps)) {
                    device.activeFormat = format
                    let dims = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
                    await MainActor.run {
                        self.actualCaptureSize = CGSize(width: CGFloat(dims.width), height: CGFloat(dims.height))
                    }
                    print("Switched format for fps: \(dims.width)x\(dims.height)")
                }
            }
            let clampedFps = Self.clampFps(Double(fps), format: device.activeFormat)
            let frameDuration = CMTime(value: 1, timescale: CMTimeScale(clampedFps))
            device.activeVideoMinFrameDuration = frameDuration
            device.activeVideoMaxFrameDuration = frameDuration
            await MainActor.run {
                if clampedFps != Double(self.fps) { self.fps = Int(clampedFps) }
            }
            device.unlockForConfiguration()
        } catch {
            print("Apply fps failed: \(error)")
        }
    }

    func stopCapture() async {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.captureSession.stopRunning()
        }
        // 释放 H.264 编码器；下次 startCapture 后会按新尺寸重新配置
        h264Encoder.teardown()
        h264NeedsKeyframe = true
    }

    private func requestAuthorization() async -> Bool {
        let cameraStatus = await AVCaptureDevice.requestAccess(for: .video)
        let audioStatus = await AVCaptureDevice.requestAccess(for: .audio)
        return cameraStatus && audioStatus
    }

    /// 将期望帧率钳制到指定格式支持的帧率范围。
    /// 高分辨率（4K/2K）通常不支持 60fps，强行设置会导致采集崩溃。
    static func clampFps(_ requestedFps: Double, format: AVCaptureDevice.Format) -> Double {
        let ranges = format.videoSupportedFrameRateRanges
        // 在某个范围内直接返回
        for range in ranges {
            if requestedFps >= range.minFrameRate && requestedFps <= range.maxFrameRate {
                return requestedFps
            }
        }
        // 降级：取所有范围中最大的 maxFrameRate（旧实现误取最小值，导致 4K 锁到 30）
        let maxSupported = ranges.map { $0.maxFrameRate }.max() ?? 30.0
        return min(maxSupported, max(requestedFps, 15.0))
    }

    /// 从设备 formats 中选择最匹配目标的 format。
    /// 优先：分辨率 >= 目标 且 支持目标帧率 且 面积最小（最接近目标分辨率）
    /// 降级 1：分辨率 >= 目标 且 面积最小（不支持目标帧率，由 clampFps 降级）
    /// 降级 2：无分辨率达标，取设备支持的最高分辨率
    static func selectBestFormat(in formats: [AVCaptureDevice.Format],
                                 targetSize: CGSize,
                                 targetFps: Double) -> AVCaptureDevice.Format? {
        let candidates = formats.compactMap { format -> (format: AVCaptureDevice.Format, size: CMVideoDimensions, supportsFps: Bool)? in
            let size = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            guard CGFloat(size.width) >= targetSize.width && CGFloat(size.height) >= targetSize.height else { return nil }
            let supportsFps = format.videoSupportedFrameRateRanges.contains { range in
                targetFps >= range.minFrameRate && targetFps <= range.maxFrameRate
            }
            return (format, size, supportsFps)
        }
        // 优先：支持目标帧率 且 面积最小
        if let best = candidates.filter({ $0.supportsFps })
                                .min(by: { $0.size.width * $0.size.height < $1.size.width * $1.size.height }) {
            return best.format
        }
        // 降级 1：分辨率达标但不支持目标帧率，取面积最小
        if let best = candidates.min(by: { $0.size.width * $0.size.height < $1.size.width * $1.size.height }) {
            return best.format
        }
        // 降级 2：取设备支持的最高分辨率
        return formats.max(by: {
            let s1 = CMVideoFormatDescriptionGetDimensions($0.formatDescription)
            let s2 = CMVideoFormatDescriptionGetDimensions($1.formatDescription)
            return s1.width * s1.height < s2.width * s2.height
        })
    }
}

extension CaptureManager: AVCaptureVideoDataOutputSampleBufferDelegate, AVCaptureAudioDataOutputSampleBufferDelegate {

    func captureOutput(_ output: AVCaptureOutput,
                       didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        if output is AVCaptureVideoDataOutput {
            switch videoCodec {
            case .h264:
                // H.264 硬件编码路径：1080p60 稳定，带宽 ~8Mbps
                processVideoFrameAsH264(sampleBuffer)
            case .jpeg:
                // JPEG 压缩路径：带宽 ~15-20 MB/s
                processVideoFrameAsJPEG(sampleBuffer)
            case .bgra:
                // BGRA 无压缩路径：带宽极高（1080p60 ≈ 500 MB/s），仅 WiFi 6/USB 3 可用
                rawStreamServer?.processSampleBuffer(sampleBuffer, requiresBGRAConversion: false)
            }
        } else if output is AVCaptureAudioDataOutput {
            // 音频帧转发到 UDP 模块（AudioStreamServer 动态检测格式并转换为 PCM16LE）
            audioStreamServer?.processSampleBuffer(sampleBuffer)
        }
    }

    /// 将 CVPixelBuffer 用 ImageIO 编码为 JPEG，再交给 RawStreamServer 发送。
    /// format=10 (JPEG) 时，width/height 仍是原始像素尺寸，bytes_per_row=0，
    /// payload 为 JPEG 字节流。桌面端据此用 PIL/numpy 解码。
    ///
    /// 性能路径：VTCreateCGImageFromCVPixelBuffer 直接从 CVPixelBuffer 创建 CGImage
    /// （跳过 CIImage→CGImage 的 GPU 转换），再由 ImageIO 编码 JPEG。
    /// 比 CIContext.createCGImage 快 30-50%，是 1080p60 的关键优化。
    private func processVideoFrameAsJPEG(_ sampleBuffer: CMSampleBuffer) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)

        // 1. CVPixelBuffer -> CGImage（VTCreateCGImageFromCVPixelBuffer 直接转换，零拷贝）
        var cgImageOpt: CGImage?
        let status = VTCreateCGImageFromCVPixelBuffer(pixelBuffer, options: nil, imageOut: &cgImageOpt)
        guard status == noErr, let cgImage = cgImageOpt else {
            // 兜底：VT 失败时回退到 CIContext 路径
            let ciImage = CIImage(cvPixelBuffer: pixelBuffer)
            guard let fallback = ciContext.createCGImage(ciImage, from: ciImage.extent) else { return }
            encodeJPEG(fallback, width: width, height: height)
            return
        }

        encodeJPEG(cgImage, width: width, height: height)
    }

    /// CGImage -> JPEG via ImageIO（硬件加速编码）
    private func encodeJPEG(_ cgImage: CGImage, width: Int, height: Int) {
        let mutableData = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(
            mutableData, UTType.jpeg.identifier as CFString, 1, nil
        ) else { return }
        let options: [CFString: Any] = [
            kCGImageDestinationLossyCompressionQuality: 0.75
        ]
        CGImageDestinationAddImage(dest, cgImage, options as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return }

        rawStreamServer?.processJPEGFrame(mutableData as Data, width: width, height: height)
    }

    /// H.264 硬件编码路径：CVPixelBuffer → VTCompressionSession → Annex-B NAL → RawStreamServer。
    /// format=20 (H264) 时，payload 为一个 Access Unit（关键帧含 SPS/PPS+IDR），
    /// 桌面端用 PyAV/ffmpeg 解码。
    private func processVideoFrameAsH264(_ sampleBuffer: CMSampleBuffer) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)

        // 懒配置：首次或尺寸/fps 变化时（re）创建 VTCompressionSession
        h264Encoder.configure(width: width, height: height, fps: fps)

        // 设置编码输出回调（每次都设，确保 rawStreamServer 引用最新）
        h264Encoder.onFrame = { [weak self] data, isKeyframe in
            self?.rawStreamServer?.processH264Frame(data, width: width, height: height,
                                                     isKeyframe: isKeyframe)
        }

        h264Encoder.encode(pixelBuffer)
    }
}

extension CaptureManager {
    /// 编码方式切换时的处理：H.264 编码器需重新配置（下次 processVideoFrameAsH264 触发），
    /// 切换到 H.264 时强制下一帧为 IDR。
    private func reconfigureEncoder() async {
        if videoCodec == .h264 {
            h264NeedsKeyframe = true
            // 用当前已知尺寸/fps 重新配置（若已采集，actualCaptureSize 有值）
            let w = Int(actualCaptureSize.width)
            let h = Int(actualCaptureSize.height)
            if w > 0 && h > 0 {
                h264Encoder.configure(width: w, height: h, fps: fps)
            }
        } else {
            // 切换离开 H.264：释放编码器资源
            h264Encoder.teardown()
            h264NeedsKeyframe = true
        }
    }
}
