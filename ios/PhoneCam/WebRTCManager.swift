import Foundation
import WebRTC

@MainActor
class WebRTCManager: NSObject, ObservableObject {
    /// 连接状态枚举，便于 UI 层判断
    enum ConnectionState: Equatable {
        case idle
        case connecting
        case connected
        case disconnected
        case failed

        var displayText: String {
            switch self {
            case .idle: return "未连接"
            case .connecting: return "连接中..."
            case .connected: return "Connected"
            case .disconnected: return "已断开"
            case .failed: return "连接失败"
            }
        }
    }

    @Published var statusMessage = "未连接"
    @Published var connectionState: ConnectionState = .idle
    /// 桌面端推送过来的只读配置快照，供 UI 显示
    @Published var remoteSettings: [String: Any] = [:]

    private var peerConnection: RTCPeerConnection?
    private let factory: RTCPeerConnectionFactory
    private var signalingClient: SignalingClient?
    private var targetResolution: CaptureResolution?
    private var remoteDataChannel: RTCDataChannel?

    private let iceServers = [
        RTCIceServer(urlStrings: ["stun:stun.l.google.com:19302"])
    ]

    override init() {
        let encoderFactory = RTCDefaultVideoEncoderFactory()
        let decoderFactory = RTCDefaultVideoDecoderFactory()
        factory = RTCPeerConnectionFactory(encoderFactory: encoderFactory,
                                           decoderFactory: decoderFactory)
        super.init()
    }

    func connect(signalingURL: String, roomID: String) async {
        let client = SignalingClient(url: signalingURL, roomID: roomID)
        client.onMessage = { [weak self] message in
            Task { @MainActor [weak self] in
                await self?.handleSignalingMessage(message)
            }
        }
        do {
            try await client.connect()
            self.signalingClient = client
            statusMessage = "已连接信令服务器"
            connectionState = .connecting
        } catch {
            statusMessage = "信令服务器连接失败"
            connectionState = .failed
        }
    }

    func publish(videoTrack: RTCVideoTrack?, audioTrack: RTCAudioTrack?, targetResolution: CaptureResolution? = nil) async {
        self.targetResolution = targetResolution
        let config = RTCConfiguration()
        config.iceServers = iceServers
        config.sdpSemantics = .unifiedPlan

        let constraints = RTCMediaConstraints(mandatoryConstraints: nil,
                                              optionalConstraints: nil)
        peerConnection = factory.peerConnection(with: config,
                                                constraints: constraints,
                                                delegate: self)

        if let videoTrack = videoTrack {
            peerConnection?.add(videoTrack, streamIds: ["stream0"])
        }
        if let audioTrack = audioTrack {
            peerConnection?.add(audioTrack, streamIds: ["stream0"])
        }

        let offerConstraints = RTCMediaConstraints(mandatoryConstraints: [
            kRTCMediaConstraintsOfferToReceiveAudio: kRTCMediaConstraintsValueTrue,
            kRTCMediaConstraintsOfferToReceiveVideo: kRTCMediaConstraintsValueTrue,
        ], optionalConstraints: nil)

        let offer = await withCheckedContinuation { continuation in
            peerConnection?.offer(for: offerConstraints) { sdp, error in
                continuation.resume(returning: sdp)
            }
        }

        guard let offer = offer else {
            statusMessage = "创建 offer 失败"
            connectionState = .failed
            return
        }

        await withCheckedContinuation { continuation in
            peerConnection?.setLocalDescription(offer) { error in
                continuation.resume()
            }
        }

        await signalingClient?.send(message: [
            "type": "offer",
            "sdp": offer.sdp
        ])
        statusMessage = "等待电脑端响应..."
        connectionState = .connecting
    }

    func handleSignalingMessage(_ message: [String: Any]) async {
        guard let type = message["type"] as? String else { return }

        if type == "answer", let sdp = message["sdp"] as? String {
            let remoteDesc = RTCSessionDescription(type: .answer, sdp: sdp)
            await withCheckedContinuation { continuation in
                peerConnection?.setRemoteDescription(remoteDesc) { error in
                    continuation.resume()
                }
            }
            // 配置视频编码参数：优先保持分辨率，并按目标分辨率设置较高码率
            if let sender = peerConnection?.senders.first(where: { $0.track?.kind == "video" }) {
                let params = sender.parameters
                params.degradationPreference = NSNumber(value: RTCDegradationPreference.maintainResolution.rawValue)
                let targetBitrate: Int
                switch targetResolution {
                case .p4K:
                    targetBitrate = 20_000_000
                case .p2K:
                    targetBitrate = 12_000_000
                case .p1080:
                    targetBitrate = 8_000_000
                case .p720, .p480, .p240:
                    targetBitrate = 5_000_000
                case .none:
                    targetBitrate = 8_000_000
                }
                if params.encodings.isEmpty {
                    print("Warning: no video encodings found")
                } else {
                    for encoding in params.encodings {
                        encoding.maxBitrateBps = NSNumber(value: targetBitrate)
                    }
                    sender.parameters = params
                    print("Configured video max bitrate: \(targetBitrate / 1_000_000) Mbps")
                }
            }
            statusMessage = "WebRTC 连接已建立"
        } else if type == "ice",
                  let candidate = message["candidate"] as? String,
                  let sdpMid = message["sdpMid"] as? String,
                  let sdpMLineIndex = message["sdpMLineIndex"] as? Int32 {
            let iceCandidate = RTCIceCandidate(sdp: candidate,
                                               sdpMLineIndex: sdpMLineIndex,
                                               sdpMid: sdpMid)
            try? await peerConnection?.add(iceCandidate)
        } else if type == "settings" {
            // 信令通道兜底：data channel 未就绪时桌面端通过信令推送配置
            if let settings = message["settings"] as? [String: Any] {
                remoteSettings = settings
                print("Received settings via signaling: \(settings)")
            }
        }
    }

    func disconnect() async {
        peerConnection?.close()
        peerConnection = nil
        remoteDataChannel = nil
        signalingClient?.disconnect()
        signalingClient = nil
        statusMessage = "已断开"
        connectionState = .disconnected
    }
}

extension WebRTCManager: RTCPeerConnectionDelegate {
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {}
    nonisolated func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {}

    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection,
                                    didChange newState: RTCIceGatheringState) {}

    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection,
                                    didChange newState: RTCIceConnectionState) {
        Task { @MainActor in
            switch newState {
            case .connected, .completed:
                // 明确显示为 "Connected"（用户要求）
                self.statusMessage = "Connected"
                self.connectionState = .connected
            case .disconnected, .failed, .closed:
                self.statusMessage = "WebRTC 连接断开"
                self.connectionState = newState == .failed ? .failed : .disconnected
            default:
                break
            }
        }
    }

    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection,
                                    didGenerate candidate: RTCIceCandidate) {
        Task { @MainActor in
            await self.signalingClient?.send(message: [
                "type": "ice",
                "candidate": candidate.sdp,
                "sdpMid": candidate.sdpMid as Any,
                "sdpMLineIndex": candidate.sdpMLineIndex
            ])
        }
    }

    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection,
                                    didOpen dataChannel: RTCDataChannel) {
        Task { @MainActor in
            // 接收桌面端创建的 settings data channel，监听配置推送
            self.remoteDataChannel = dataChannel
            dataChannel.delegate = self
            print("Remote data channel opened: \(dataChannel.label)")
        }
    }

    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection,
                                    didRemove candidates: [RTCIceCandidate]) {}
}

extension WebRTCManager: RTCDataChannelDelegate {
    nonisolated func dataChannelDidChangeState(_ dataChannel: RTCDataChannel) {
        Task { @MainActor in
            print("Data channel '\(dataChannel.label)' state: \(dataChannel.readyState.rawValue)")
        }
    }

    nonisolated func dataChannel(_ dataChannel: RTCDataChannel,
                                 didReceiveMessageWith buffer: RTCDataBuffer) {
        guard let text = String(data: buffer.data, encoding: .utf8) else { return }
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            print("Invalid JSON from data channel")
            return
        }
        Task { @MainActor in
            if json["type"] as? String == "settings",
               let settings = json["settings"] as? [String: Any] {
                self.remoteSettings = settings
                print("Received settings via data channel: \(settings)")
            }
        }
    }
}

class SignalingClient: NSObject, URLSessionWebSocketDelegate {
    private let url: String
    private let roomID: String
    private var webSocketTask: URLSessionWebSocketTask?
    private var connectContinuation: CheckedContinuation<Void, Error>?
    var onMessage: (([String: Any]) -> Void)?

    init(url: String, roomID: String) {
        self.url = url
        self.roomID = roomID
        super.init()
    }

    func connect() async throws {
        guard let wsURL = URL(string: "\(url)/ws/\(roomID)") else {
            throw SignalingError.invalidURL
        }
        let session = URLSession(configuration: .default, delegate: self, delegateQueue: OperationQueue())
        webSocketTask = session.webSocketTask(with: wsURL)
        webSocketTask?.resume()
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            self.connectContinuation = continuation
        }
        receiveMessage()
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        DispatchQueue.main.async {
            self.connectContinuation?.resume()
            self.connectContinuation = nil
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error = error {
            DispatchQueue.main.async {
                self.connectContinuation?.resume(throwing: error)
                self.connectContinuation = nil
            }
        }
    }

    private func receiveMessage() {
        webSocketTask?.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let message):
                if case .string(let text) = message,
                   let data = text.data(using: .utf8),
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    DispatchQueue.main.async {
                        self.onMessage?(json)
                    }
                }
                self.receiveMessage()
            case .failure(let error):
                print("WebSocket error: \(error)")
            }
        }
    }

    func send(message: [String: Any]) async {
        guard let data = try? JSONSerialization.data(withJSONObject: message),
              let text = String(data: data, encoding: .utf8) else { return }
        try? await webSocketTask?.send(.string(text))
    }

    func disconnect() {
        connectContinuation?.resume(throwing: SignalingError.disconnected)
        connectContinuation = nil
        webSocketTask?.cancel(with: .goingAway, reason: nil)
    }
}

enum SignalingError: Error {
    case invalidURL
    case disconnected
}
