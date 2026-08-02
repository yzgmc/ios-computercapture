import SwiftUI

// MARK: - 主题配色
private extension Color {
    static let appBackground = Color(uiColor: UIColor.systemGroupedBackground)
    static let cardBackground = Color(uiColor: UIColor.secondarySystemGroupedBackground)
    static let accentBlue = Color.blue
    static let accentRed = Color.red
    static let primaryText = Color(uiColor: UIColor.label)
    static let secondaryText = Color(uiColor: UIColor.secondaryLabel)
}

struct ContentView: View {
    @StateObject private var captureManager = CaptureManager()
    @StateObject private var webRTCManager = WebRTCManager()

    @State private var serverURL = "ws://192.168.1.100:8080"
    @State private var roomID = "room1"
    @State private var isConnected = false

    var body: some View {
        NavigationView {
            GeometryReader { geometry in
                ZStack {
                    Color.appBackground
                        .ignoresSafeArea()

                    ScrollView(showsIndicators: false) {
                        VStack(spacing: 16) {
                            previewCard(in: geometry)
                            connectionCard()
                            remoteSettingsCard()
                            statusBar()
                        }
                        .padding(.horizontal)
                        .padding(.top, 8)
                        .padding(.bottom, 92) // 为底部固定按钮留出空间
                    }

                    connectButton()
                        .frame(maxHeight: .infinity, alignment: .bottom)
                        .padding(.horizontal)
                        .padding(.bottom, 16)
                }
            }
            .navigationTitle("PhoneCam")
            .navigationBarTitleDisplayMode(.large)
        }
    }

    // MARK: - 摄像头预览卡片
    // WebRTC 连接成功后（connectionState == .connected），
    // 不再显示任何视频画面，仅保留连接状态文字指示。
    @ViewBuilder
    private func previewCard(in geometry: GeometryProxy) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "video.fill")
                    .foregroundStyle(Color.accentColor)
                Text("实时预览")
                    .font(.headline)
                    .foregroundStyle(Color.primaryText)
                Spacer()
                Text(captureManager.selectedResolution.label)
                    .font(.caption)
                    .fontWeight(.semibold)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(Color.accentColor.opacity(0.12))
                    .foregroundStyle(Color.accentColor)
                    .clipShape(Capsule())
            }

            if webRTCManager.connectionState == .connected {
                // 连接已建立：仅显示连接状态文字指示，不显示视频画面
                ZStack {
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(Color.black.opacity(0.9))
                    VStack(spacing: 12) {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 48))
                            .foregroundStyle(Color.green)
                        Text("Connected")
                            .font(.title2.bold())
                            .foregroundStyle(Color.white)
                        Text("视频画面已隐藏")
                            .font(.footnote)
                            .foregroundStyle(Color.white.opacity(0.6))
                    }
                }
                .frame(maxWidth: .infinity)
                .frame(height: previewHeight(for: geometry.size.width - 32))
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                .shadow(color: .black.opacity(0.08), radius: 8, x: 0, y: 4)
            } else {
                // 未连接时仍展示本地预览，方便用户确认摄像头工作正常
                CameraPreviewView(captureManager: captureManager)
                    .frame(maxWidth: .infinity)
                    .frame(height: previewHeight(for: geometry.size.width - 32))
                    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .shadow(color: .black.opacity(0.08), radius: 8, x: 0, y: 4)
            }
        }
        .padding()
        .background(Color.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.04), radius: 12, x: 0, y: 6)
    }

    // MARK: - 连接设置卡片
    // 服务器地址与房间 ID 仍允许编辑，因为这是建立连接必需的输入。
    // 建立连接后这些字段会被禁用，避免误改。
    @ViewBuilder
    private func connectionCard() -> some View {
        VStack(alignment: .leading, spacing: 16) {
            sectionHeader(icon: "network", title: "信令服务器")

            HStack(spacing: 12) {
                Image(systemName: "link")
                    .foregroundStyle(Color.secondaryText)
                    .frame(width: 24)
                TextField("ws://...", text: $serverURL)
                    .textFieldStyle(.roundedBorder)
                    .autocapitalization(.none)
                    .disableAutocorrection(true)
                    .keyboardType(.URL)
                    .disabled(isConnected)
            }

            Divider()

            sectionHeader(icon: "person.2.fill", title: "房间 ID")

            HStack(spacing: 12) {
                Image(systemName: "number")
                    .foregroundStyle(Color.secondaryText)
                    .frame(width: 24)
                TextField("room1", text: $roomID)
                    .textFieldStyle(.roundedBorder)
                    .autocapitalization(.none)
                    .disableAutocorrection(true)
                    .disabled(isConnected)
            }
        }
        .padding()
        .background(Color.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.04), radius: 12, x: 0, y: 6)
    }

    // MARK: - 远端配置卡片（只读）
    // 所有音视频/翻转参数均由桌面端推送，iPhone 端只显示，不可编辑。
    @ViewBuilder
    private func remoteSettingsCard() -> some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 8) {
                Image(systemName: "lock.fill")
                    .foregroundStyle(Color.secondaryText)
                    .frame(width: 22)
                Text("采集参数（由桌面端控制 · 只读）")
                    .font(.headline)
                    .foregroundStyle(Color.primaryText)
                Spacer()
            }

            Divider()

            readOnlyRow(label: "分辨率", value: resolutionDisplayText)
            Divider()
            readOnlyRow(label: "帧率", value: fpsDisplayText)
            Divider()
            readOnlyRow(label: "音量", value: volumeDisplayText)
            Divider()
            readOnlyRow(label: "水平翻转", value: flipDisplayText(key: "flip_horizontal"))
            Divider()
            readOnlyRow(label: "垂直翻转", value: flipDisplayText(key: "flip_vertical"))
        }
        .padding()
        .background(Color.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.04), radius: 12, x: 0, y: 6)
    }

    private func readOnlyRow(label: String, value: String) -> some View {
        HStack {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(Color.secondaryText)
            Spacer()
            Text(value)
                .font(.subheadline.bold())
                .monospacedDigit()
                .foregroundStyle(Color.primaryText)
        }
    }

    private var resolutionDisplayText: String {
        if let w = webRTCManager.remoteSettings["width"] as? Int,
           let h = webRTCManager.remoteSettings["height"] as? Int {
            return "\(w)x\(h)"
        }
        return "\(Int(captureManager.selectedResolution.size.width))x\(Int(captureManager.selectedResolution.size.height))"
    }

    private var fpsDisplayText: String {
        if let fps = webRTCManager.remoteSettings["fps"] as? Int {
            return "\(fps) FPS"
        }
        return "\(captureManager.fps) FPS"
    }

    private var volumeDisplayText: String {
        if let volume = webRTCManager.remoteSettings["volume"] as? Double {
            return "\(Int(volume * 100))%"
        }
        return "\(Int(captureManager.volume * 100))%"
    }

    private func flipDisplayText(key: String) -> String {
        if let flag = webRTCManager.remoteSettings[key] as? Bool {
            return flag ? "开启" : "关闭"
        }
        return "关闭"
    }

    // MARK: - 状态栏
    @ViewBuilder
    private func statusBar() -> some View {
        HStack(spacing: 8) {
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
                .shadow(color: statusColor.opacity(0.5), radius: 4)

            Text(webRTCManager.statusMessage)
                .font(.footnote)
                .foregroundStyle(Color.secondaryText)

            Spacer()
        }
        .padding(.horizontal, 8)
    }

    // MARK: - 连接按钮
    @ViewBuilder
    private func connectButton() -> some View {
        Button(action: {
            if isConnected {
                disconnect()
            } else {
                connect()
            }
        }) {
            HStack(spacing: 10) {
                Image(systemName: isConnected ? "stop.fill" : "play.fill")
                Text(isConnected ? "断开连接" : "开始共享")
                    .font(.headline)
            }
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 16)
            .background(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(isConnected ? Color.accentRed : Color.accentBlue)
            )
            .shadow(color: (isConnected ? Color.accentRed : Color.accentBlue).opacity(0.35),
                    radius: 12, x: 0, y: 6)
        }
        .buttonStyle(.plain)
    }

    // MARK: - 辅助视图
    private func sectionHeader(icon: String, title: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .foregroundStyle(Color.accentColor)
                .frame(width: 22)
            Text(title)
                .font(.headline)
                .foregroundStyle(Color.primaryText)
            Spacer()
        }
    }

    // MARK: - 响应式尺寸计算
    private var previewAspectRatio: CGFloat {
        let size = captureManager.actualCaptureSize
        guard size.height > 0 else { return 16.0 / 9.0 }
        return size.width / size.height
    }

    private func previewHeight(for cardWidth: CGFloat) -> CGFloat {
        let naturalHeight = cardWidth / previewAspectRatio
        return min(naturalHeight, UIScreen.main.bounds.height * 0.55)
    }

    private var statusColor: Color {
        switch webRTCManager.connectionState {
        case .connected:
            return .green
        case .connecting:
            return .orange
        case .disconnected, .failed, .idle:
            return .gray
        }
    }

    // MARK: - 业务逻辑
    private func connect() {
        Task {
            await webRTCManager.connect(signalingURL: serverURL, roomID: roomID)
            await captureManager.startCapture()
            await webRTCManager.publish(videoTrack: captureManager.videoTrack,
                                     audioTrack: captureManager.audioTrack,
                                     targetResolution: captureManager.selectedResolution)
            await MainActor.run {
                isConnected = true
            }
        }
    }

    private func disconnect() {
        Task {
            await captureManager.stopCapture()
            await webRTCManager.disconnect()
            await MainActor.run {
                isConnected = false
            }
        }
    }
}

// MARK: - 摄像头预览
struct CameraPreviewView: UIViewRepresentable {
    let captureManager: CaptureManager

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.backgroundColor = .black
        captureManager.setupPreview(in: view)
        return view
    }

    func updateUIView(_ uiView: UIView, context: Context) {
        captureManager.updatePreviewFrame(to: uiView.bounds)
    }
}
