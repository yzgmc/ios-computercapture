import AVFoundation
import UIKit
import WebRTC

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

class CaptureManager: NSObject, ObservableObject {
    @Published var selectedResolution: CaptureResolution = .p720
    @Published var fps: Int = 30
    @Published var volume: Double = 0.8
    // 摄像头实际输出尺寸（由 activeFormat 决定），用于预览按真实比例渲染
    @Published var actualCaptureSize: CGSize = CGSize(width: 1280, height: 720)

    private let captureSession = AVCaptureSession()
    private var videoDeviceInput: AVCaptureDeviceInput?
    private var videoOutput: AVCaptureVideoDataOutput?
    private var previewLayer: AVCaptureVideoPreviewLayer?

    var videoTrack: RTCVideoTrack?
    var audioTrack: RTCAudioTrack?

    private let videoSource: RTCVideoSource
    private let videoCapturer: RTCVideoCapturer
    private let audioSource: RTCAudioSource
    private let factory: RTCPeerConnectionFactory

    override init() {
        let encoderFactory = RTCDefaultVideoEncoderFactory()
        let decoderFactory = RTCDefaultVideoDecoderFactory()
        factory = RTCPeerConnectionFactory(encoderFactory: encoderFactory,
                                           decoderFactory: decoderFactory)
        videoSource = factory.videoSource()
        videoCapturer = RTCVideoCapturer(delegate: videoSource)
        audioSource = factory.audioSource(with: nil)
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

        do {
            guard let videoDevice = AVCaptureDevice.default(.builtInWideAngleCamera,
                                                            for: .video,
                                                            position: .back) else {
                print("无法获取后置摄像头")
                return
            }

            try videoDevice.lockForConfiguration()
            let dimensions = selectedResolution.size
            let matchingFormats = videoDevice.formats.compactMap { format -> (AVCaptureDevice.Format, CMVideoDimensions)? in
                let size = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
                guard CGFloat(size.width) >= dimensions.width && CGFloat(size.height) >= dimensions.height else { return nil }
                return (format, size)
            }
            var selectedFormat: AVCaptureDevice.Format?
            if let bestFormat = matchingFormats.min(by: { $0.1.width * $0.1.height < $1.1.width * $1.1.height }) {
                selectedFormat = bestFormat.0
                print("Selected format: \(bestFormat.1.width)x\(bestFormat.1.height)")
            } else {
                // 没有完全匹配目标的分辨率，使用设备支持的最高格式
                if let highestFormat = videoDevice.formats.max(by: {
                    let s1 = CMVideoFormatDescriptionGetDimensions($0.formatDescription)
                    let s2 = CMVideoFormatDescriptionGetDimensions($1.formatDescription)
                    return s1.width * s1.height < s2.width * s2.height
                }) {
                    selectedFormat = highestFormat
                    let size = CMVideoFormatDescriptionGetDimensions(highestFormat.formatDescription)
                    print("Fallback to highest format: \(size.width)x\(size.height)")
                }
            }

            if let format = selectedFormat {
                videoDevice.activeFormat = format
                captureSession.sessionPreset = .inputPriority

                // 记录实际采集尺寸，供 UI 按真实比例显示预览
                let dims = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
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
            videoOutput.setSampleBufferDelegate(self, queue: DispatchQueue(label: "videoQueue"))
            if captureSession.canAddOutput(videoOutput) {
                captureSession.addOutput(videoOutput)
                self.videoOutput = videoOutput
            }
        } catch {
            print("视频配置失败: \(error)")
        }

        captureSession.commitConfiguration()

        videoTrack = factory.videoTrack(with: videoSource, trackId: "video0")
        audioTrack = factory.audioTrack(with: audioSource, trackId: "audio0")

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.captureSession.startRunning()
        }
    }

    func stopCapture() async {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.captureSession.stopRunning()
        }
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
        // 取所有范围并集内最接近期望值的可用帧率
        var supported = requestedFps
        var found = false
        for range in ranges {
            let minFps = range.minFrameRate
            let maxFps = range.maxFrameRate
            if requestedFps >= minFps && requestedFps <= maxFps {
                return requestedFps
            }
            // 记录该范围的最大可用值作为候选
            if !found || maxFps < supported {
                supported = maxFps
                found = true
            }
        }
        // 若期望帧率高于所有范围上限，返回最大支持值；否则兜底 30
        if !found { return 30.0 }
        return min(supported, max(requestedFps, 15.0))
    }
}

extension CaptureManager: AVCaptureVideoDataOutputSampleBufferDelegate {

    func captureOutput(_ output: AVCaptureOutput,
                       didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let rtcFrame = RTCVideoFrame(buffer: RTCCVPixelBuffer(pixelBuffer: pixelBuffer),
                                     rotation: ._0,
                                     timeStampNs: Int64(CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer)) * 1_000_000_000))
        videoSource.capturer(videoCapturer, didCapture: rtcFrame)
    }
}
