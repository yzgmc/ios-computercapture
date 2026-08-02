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
    // 原画质传输（无压缩 TCP，局域网适用）
    @State private var rawStreamEnabled = false
    @State private var rawStreamHost = "192.168.1.100"
    @State private var rawStreamPort = "5000"
    private let rawStreamServer = RawStreamServer()

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
                            captureSettingsCard()
                            rawStreamCard()
                            statusBar()
                        }
                        .padding(.horizontal)
                        .padding(.top, 8)
                        // 底部为固定连接按钮预留空间（按钮高度+上下边距），按屏幕高度自适应
                        .padding(.bottom, max(92, geometry.size.height * 0.12))
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
                .frame(height: previewHeight(for: geometry.size.width - 32, in: geometry))
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                .shadow(color: .black.opacity(0.08), radius: 8, x: 0, y: 4)
            } else {
                // 未连接时仍展示本地预览，方便用户确认摄像头工作正常
                CameraPreviewView(captureManager: captureManager)
                    .frame(maxWidth: .infinity)
                    .frame(height: previewHeight(for: geometry.size.width - 32, in: geometry))
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

    // MARK: - 采集参数卡片（可编辑）
    // 控制方向：iPhone 端可编辑分辨率/帧率/音量/翻转，实时推送到桌面端。
    @ViewBuilder
    private func captureSettingsCard() -> some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 8) {
                Image(systemName: "slider.horizontal.3")
                    .foregroundStyle(Color.accentColor)
                    .frame(width: 22)
                Text("采集参数（本机控制）")
                    .font(.headline)
                    .foregroundStyle(Color.primaryText)
                Spacer()
            }

            Divider()

            // 分辨率
            HStack {
                Text("分辨率")
                    .font(.subheadline)
                    .foregroundStyle(Color.secondaryText)
                Spacer()
                Picker("分辨率", selection: $captureManager.selectedResolution) {
                    ForEach(CaptureResolution.allCases) { res in
                        Text(res.label).tag(res)
                    }
                }
                .pickerStyle(.menu)
                .onChange(of: captureManager.selectedResolution) { _ in pushSettings() }
            }

            Divider()

            // 帧率
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("帧率")
                        .font(.subheadline)
                        .foregroundStyle(Color.secondaryText)
                    Spacer()
                    Text("\(captureManager.fps) FPS")
                        .font(.subheadline.bold())
                        .monospacedDigit()
                        .foregroundStyle(Color.primaryText)
                }
                Slider(value: Binding(
                    get: { Double(captureManager.fps) },
                    set: { captureManager.fps = Int($0) }
                ), in: 15...60, step: 1)
                .tint(Color.accentBlue)
                .onChange(of: captureManager.fps) { _ in pushSettings() }
            }

            Divider()

            // 音量
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("音量")
                        .font(.subheadline)
                        .foregroundStyle(Color.secondaryText)
                    Spacer()
                    Text("\(Int(captureManager.volume * 100))%")
                        .font(.subheadline.bold())
                        .monospacedDigit()
                        .foregroundStyle(Color.primaryText)
                }
                Slider(value: $captureManager.volume, in: 0...1, step: 0.05)
                    .tint(Color.accentBlue)
                    .onChange(of: captureManager.volume) { _ in pushSettings() }
            }

            Divider()

            // 翻转
            HStack {
                Toggle("水平翻转", isOn: $captureManager.flipHorizontal)
                    .toggleStyle(.switch)
                    .tint(Color.accentBlue)
                    .labelsHidden()
                Text("水平翻转")
                    .font(.subheadline)
                    .foregroundStyle(Color.primaryText)
                Spacer()
                Toggle("垂直翻转", isOn: $captureManager.flipVertical)
                    .toggleStyle(.switch)
                    .tint(Color.accentBlue)
                    .labelsHidden()
                Text("垂直翻转")
                    .font(.subheadline)
                    .foregroundStyle(Color.primaryText)
            }
            .onChange(of: captureManager.flipHorizontal) { _ in pushSettings() }
            .onChange(of: captureManager.flipVertical) { _ in pushSettings() }
        }
        .padding()
        .background(Color.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.04), radius: 12, x: 0, y: 6)
    }

    /// 收集当前采集参数并推送到桌面端。
    private func pushSettings() {
        let settings: [String: Any] = [
            "width": Int(captureManager.selectedResolution.size.width),
            "height": Int(captureManager.selectedResolution.size.height),
            "fps": captureManager.fps,
            "volume": captureManager.volume,
            "flip_horizontal": captureManager.flipHorizontal,
            "flip_vertical": captureManager.flipVertical,
        ]
        webRTCManager.sendSettings(settings)
    }

    // MARK: - 原画质传输卡片
    // 启用后通过 TCP 发送无压缩 BGRA 像素帧到桌面端，适用于局域网无损场景。
    @ViewBuilder
    private func rawStreamCard() -> some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 8) {
                Image(systemName: "bolt.horizontal.fill")
                    .foregroundStyle(Color.accentColor)
                    .frame(width: 22)
                Text("原画质传输（无压缩 TCP）")
                    .font(.headline)
                    .foregroundStyle(Color.primaryText)
                Spacer()
                Toggle("启用", isOn: $rawStreamEnabled)
                    .toggleStyle(.switch)
                    .tint(Color.accentBlue)
                    .labelsHidden()
                Text(rawStreamEnabled ? "已启用" : "关闭")
                    .font(.caption)
                    .foregroundStyle(Color.secondaryText)
            }

            if rawStreamEnabled {
                Divider()
                HStack(spacing: 12) {
                    Image(systemName: "desktopcomputer")
                        .foregroundStyle(Color.secondaryText)
                        .frame(width: 24)
                    TextField("桌面 IP", text: $rawStreamHost)
                        .textFieldStyle(.roundedBorder)
                        .keyboardType(.decimalPad)
                        .autocapitalization(.none)
                        .disableAutocorrection(true)
                        .disabled(isConnected)
                }
                HStack(spacing: 12) {
                    Image(systemName: "dot.radiowaves.left.and.right")
                        .foregroundStyle(Color.secondaryText)
                        .frame(width: 24)
                    TextField("TCP 端口", text: $rawStreamPort)
                        .textFieldStyle(.roundedBorder)
                        .keyboardType(.numberPad)
                        .disableAutocorrection(true)
                        .disabled(isConnected)
                }
                Text("提示：无压缩传输带宽需求高（720p≈265Mbps），仅建议千兆局域网使用。")
                    .font(.caption)
                    .foregroundStyle(Color.secondaryText)
            }
        }
        .padding()
        .background(Color.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.04), radius: 12, x: 0, y: 6)
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

    /// 预览高度按卡片宽度与真实宽高比计算，并限制不超过可用高度的一半，
    /// 确保 iPhone 12/15 及不同尺寸设备上预览与下方控件均可见。
    private func previewHeight(for cardWidth: CGFloat, in geometry: GeometryProxy) -> CGFloat {
        let naturalHeight = cardWidth / previewAspectRatio
        return min(naturalHeight, geometry.size.height * 0.5)
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
            // 启用原画质传输：连接桌面端 TCP 端口并挂载到采集管线
            if rawStreamEnabled, let port = UInt16(rawStreamPort) {
                captureManager.rawStreamServer = rawStreamServer
                rawStreamServer.start(host: rawStreamHost, port: port)
            }
            await webRTCManager.connect(signalingURL: serverURL, roomID: roomID)
            await captureManager.startCapture()
            await webRTCManager.publish(videoTrack: captureManager.videoTrack,
                                     audioTrack: captureManager.audioTrack,
                                     targetResolution: captureManager.selectedResolution)
            await MainActor.run {
                isConnected = true
                // 连接建立后立即推送一次当前采集参数，data channel 就绪后会再次推送
                pushSettings()
            }
        }
    }

    private func disconnect() {
        Task {
            rawStreamServer.stop()
            captureManager.rawStreamServer = nil
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
