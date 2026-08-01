import Foundation
import WebRTC

@MainActor
class WebRTCManager: ObservableObject {
    @Published var statusMessage = "未连接"

    private var peerConnection: RTCPeerConnection?
    private let factory: RTCPeerConnectionFactory
    private var signalingClient: SignalingClient?

    private let iceServers = [
        RTCIceServer(urlStrings: ["stun:stun.l.google.com:19302"])
    ]

    init() {
        let encoderFactory = RTCDefaultVideoEncoderFactory()
        let decoderFactory = RTCDefaultVideoDecoderFactory()
        factory = RTCPeerConnectionFactory(encoderFactory: encoderFactory,
                                           decoderFactory: decoderFactory)
    }

    func connect(signalingURL: String, roomID: String) async {
        let client = SignalingClient(url: signalingURL, roomID: roomID)
        client.onMessage = { [weak self] message in
            Task { @MainActor [weak self] in
                await self?.handleSignalingMessage(message)
            }
        }
        await client.connect()
        self.signalingClient = client
        statusMessage = "已连接信令服务器"
    }

    func publish(videoTrack: RTCVideoTrack?, audioTrack: RTCAudioTrack?) async {
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
            statusMessage = "WebRTC 连接已建立"
        } else if type == "ice",
                  let candidate = message["candidate"] as? String,
                  let sdpMid = message["sdpMid"] as? String,
                  let sdpMLineIndex = message["sdpMLineIndex"] as? Int32 {
            let iceCandidate = RTCIceCandidate(sdp: candidate,
                                               sdpMLineIndex: sdpMLineIndex,
                                               sdpMid: sdpMid)
            peerConnection?.add(iceCandidate)
        }
    }

    func disconnect() async {
        peerConnection?.close()
        peerConnection = nil
        await signalingClient?.disconnect()
        signalingClient = nil
        statusMessage = "已断开"
    }
}

extension WebRTCManager: RTCPeerConnectionDelegate {
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {}
    nonisolated func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {}

    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection,
                                    didChange newState: RTCIceConnectionState) {
        Task { @MainActor in
            switch newState {
            case .connected, .completed:
                self.statusMessage = "WebRTC 已连接"
            case .disconnected, .failed, .closed:
                self.statusMessage = "WebRTC 连接断开"
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
                                    didRemove candidates: [RTCIceCandidate]) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection,
                                    didOpen dataChannel: RTCDataChannel) {}
}

actor SignalingClient {
    private let url: String
    private let roomID: String
    private var webSocketTask: URLSessionWebSocketTask?
    var onMessage: (([String: Any]) -> Void)?

    init(url: String, roomID: String) {
        self.url = url
        self.roomID = roomID
    }

    func connect() {
        guard let wsURL = URL(string: "\(url)/ws/\(roomID)") else { return }
        let session = URLSession(configuration: .default)
        webSocketTask = session.webSocketTask(with: wsURL)
        webSocketTask?.resume()
        receiveMessage()
    }

    private func receiveMessage() {
        webSocketTask?.receive { [weak self] result in
            switch result {
            case .success(let message):
                if case .string(let text) = message,
                   let data = text.data(using: .utf8),
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    Task { @MainActor [weak self] in
                        self?.onMessage?(json)
                    }
                }
                self?.receiveMessage()
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
        webSocketTask?.cancel(with: .goingAway, reason: nil)
    }
}
