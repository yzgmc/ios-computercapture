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
        previewLayer.videoGravity = .resizeAspectFill
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
            if let bestFormat = matchingFormats.min(by: { $0.1.width * $0.1.height < $1.1.width * $1.1.height }) {
                videoDevice.activeFormat = bestFormat.0
                captureSession.sessionPreset = .inputPriority
                print("Selected format: \(bestFormat.1.width)x\(bestFormat.1.height)")
            } else {
                // 没有完全匹配目标的分辨率，使用设备支持的最高格式
                if let highestFormat = videoDevice.formats.max(by: {
                    let s1 = CMVideoFormatDescriptionGetDimensions($0.formatDescription)
                    let s2 = CMVideoFormatDescriptionGetDimensions($1.formatDescription)
                    return s1.width * s1.height < s2.width * s2.height
                }) {
                    videoDevice.activeFormat = highestFormat
                    captureSession.sessionPreset = .inputPriority
                    let size = CMVideoFormatDescriptionGetDimensions(highestFormat.formatDescription)
                    print("Fallback to highest format: \(size.width)x\(size.height)")
                }
            }
            videoDevice.activeVideoMinFrameDuration = CMTime(value: 1, timescale: CMTimeScale(fps))
            videoDevice.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: CMTimeScale(fps))
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
