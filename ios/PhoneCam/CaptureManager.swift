import AVFoundation
import UIKit
import WebRTC

enum CaptureResolution: CaseIterable, Identifiable {
    case p1080, p720, p480, p240

    var id: Self { self }

    var label: String {
        switch self {
        case .p1080: return "1080p"
        case .p720: return "720p"
        case .p480: return "480p"
        case .p240: return "240p"
        }
    }

    var size: CGSize {
        switch self {
        case .p1080: return CGSize(width: 1920, height: 1080)
        case .p720: return CGSize(width: 1280, height: 720)
        case .p480: return CGSize(width: 640, height: 480)
        case .p240: return CGSize(width: 320, height: 240)
        }
    }
}

@MainActor
class CaptureManager: ObservableObject {
    @Published var selectedResolution: CaptureResolution = .p720
    @Published var fps: Int = 30
    @Published var volume: Double = 0.8

    private let captureSession = AVCaptureSession()
    private var videoDeviceInput: AVCaptureDeviceInput?
    private var audioDeviceInput: AVCaptureDeviceInput?
    private var videoOutput: AVCaptureVideoDataOutput?
    private var audioOutput: AVCaptureAudioDataOutput?
    private var previewLayer: AVCaptureVideoPreviewLayer?

    var videoTrack: RTCVideoTrack?
    var audioTrack: RTCAudioTrack?

    private let videoSource: RTCVideoSource
    private let audioSource: RTCAudioSource
    private let factory: RTCPeerConnectionFactory

    init() {
        let encoderFactory = RTCDefaultVideoEncoderFactory()
        let decoderFactory = RTCDefaultVideoDecoderFactory()
        factory = RTCPeerConnectionFactory(encoderFactory: encoderFactory,
                                           decoderFactory: decoderFactory)
        videoSource = factory.videoSource()
        audioSource = factory.audioSource(with: nil)
    }

    func setupPreview(in view: UIView) {
        let previewLayer = AVCaptureVideoPreviewLayer(session: captureSession)
        previewLayer.videoGravity = .resizeAspectFill
        previewLayer.frame = view.bounds
        view.layer.addSublayer(previewLayer)
        self.previewLayer = previewLayer
    }

    func startCapture() async {
        guard await requestAuthorization() else {
            print("未获得摄像头/麦克风权限")
            return
        }

        captureSession.beginConfiguration()

        // 视频输入
        do {
            captureSession.sessionPreset = .high

            guard let videoDevice = AVCaptureDevice.default(.builtInWideAngleCamera,
                                                            for: .video,
                                                            position: .back) else {
                print("无法获取后置摄像头")
                return
            }

            try videoDevice.lockForConfiguration()
            let dimensions = selectedResolution.size
            // 设置分辨率，实际可用格式需遍历 device.formats
            if let format = videoDevice.formats.first(where: {
                let desc = $0.formatDescription
                let size = CMVideoFormatDescriptionGetDimensions(desc)
                return CGFloat(size.width) >= dimensions.width && CGFloat(size.height) >= dimensions.height
            }) {
                videoDevice.activeFormat = format
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

        // 音频输入
        do {
            guard let audioDevice = AVCaptureDevice.default(for: .audio) else {
                print("无法获取麦克风")
                return
            }
            let audioInput = try AVCaptureDeviceInput(device: audioDevice)
            if captureSession.canAddInput(audioInput) {
                captureSession.addInput(audioInput)
                audioDeviceInput = audioInput
            }

            let audioOutput = AVCaptureAudioDataOutput()
            audioOutput.setSampleBufferDelegate(self, queue: DispatchQueue(label: "audioQueue"))
            if captureSession.canAddOutput(audioOutput) {
                captureSession.addOutput(audioOutput)
                self.audioOutput = audioOutput
            }
        } catch {
            print("音频配置失败: \(error)")
        }

        captureSession.commitConfiguration()

        // 创建 WebRTC track
        videoTrack = factory.videoTrack(with: videoSource, trackId: "video0")
        audioTrack = factory.audioTrack(with: audioSource, trackId: "audio0")

        DispatchQueue.global(qos: .userInitiated).async {
            self.captureSession.startRunning()
        }
    }

    func stopCapture() async {
        DispatchQueue.global(qos: .userInitiated).async {
            self.captureSession.stopRunning()
        }
    }

    private func requestAuthorization() async -> Bool {
        let cameraStatus = await AVCaptureDevice.requestAccess(for: .video)
        let audioStatus = await AVCaptureDevice.requestAccess(for: .audio)
        return cameraStatus && audioStatus
    }
}

extension CaptureManager: AVCaptureVideoDataOutputSampleBufferDelegate,
                          AVCaptureAudioDataOutputSampleBufferDelegate {

    nonisolated func captureOutput(_ output: AVCaptureOutput,
                                   didOutput sampleBuffer: CMSampleBuffer,
                                   from connection: AVCaptureConnection) {
        if output == videoOutput {
            guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
            let rtcFrame = RTCVideoFrame(buffer: RTCCVPixelBuffer(pixelBuffer: pixelBuffer),
                                         rotation: ._0,
                                         timeStampNs: Int64(CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer)) * 1_000_000_000))
            Task { @MainActor in
                self.videoSource.capturer(self.videoSource as! RTCVideoCapturer, didCapture: rtcFrame)
            }
        } else if output == audioOutput {
            // 将 CMSampleBuffer 转换为 RTCAudioFrame
            guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
            var audioBufferList = AudioBufferList()
            var dataLength: Int = 0
            CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
                sampleBuffer,
                bufferListSizeNeededOut: &dataLength,
                bufferListOut: &audioBufferList,
                bufferListSize: MemoryLayout<AudioBufferList>.size,
                blockBufferAllocator: nil,
                blockBufferMemoryAllocator: nil,
                flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
                blockBufferOut: nil
            )

            let audioBuffer = audioBufferList.mBuffers
            guard let data = audioBuffer.mData else { return }
            let frame = RTCAudioFrame(buffer: data,
                                      bytesPerSample: audioBuffer.mBytesPerChannel,
                                      sampleCount: audioBuffer.mDataByteSize / audioBuffer.mBytesPerChannel,
                                      channels: audioBuffer.mNumberChannels,
                                      sampleRate: 48000,
                                      absoluteCaptureTimestamp: Int64(CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer)) * 1_000_000_000))

            Task { @MainActor in
                self.audioSource.capturer(self.audioSource as! RTCAudioCapturer, didCapture: frame)
            }
        }
    }
}
