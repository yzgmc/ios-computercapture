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

    // 跟踪当前设备尺寸类别，用于响应式布局
    @Environment(\.verticalSizeClass) private var verticalSizeClass

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
                            parametersCard()
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

            GeometryReader { cardGeometry in
                CameraPreviewView(captureManager: captureManager)
                    .frame(
                        width: cardGeometry.size.width,
                        height: previewHeight(width: cardGeometry.size.width,
                                              screenHeight: geometry.size.height)
                    )
                    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            }
            .aspectRatio(previewAspectRatio, contentMode: .fit)
            .shadow(color: .black.opacity(0.08), radius: 8, x: 0, y: 4)
        }
        .padding()
        .background(Color.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.04), radius: 12, x: 0, y: 6)
    }

    // MARK: - 连接设置卡片
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
            }
        }
        .padding()
        .background(Color.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.04), radius: 12, x: 0, y: 6)
    }

    // MARK: - 参数控制卡片
    @ViewBuilder
    private func parametersCard() -> some View {
        VStack(alignment: .leading, spacing: 16) {
            sectionHeader(icon: "slider.horizontal.3", title: "采集参数")

            VStack(alignment: .leading, spacing: 10) {
                Text("分辨率")
                    .font(.subheadline)
                    .foregroundStyle(Color.secondaryText)

                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 10) {
                        ForEach(CaptureResolution.allCases) { resolution in
                            Button(action: {
                                captureManager.selectedResolution = resolution
                            }) {
                                Text(resolution.label)
                                    .font(.subheadline)
                                    .fontWeight(.semibold)
                                    .lineLimit(1)
                                    .minimumScaleFactor(0.8)
                                    .foregroundStyle(
                                        captureManager.selectedResolution == resolution
                                            ? Color.white
                                            : Color.primaryText
                                    )
                                    .padding(.horizontal, 14)
                                    .padding(.vertical, 8)
                                    .background(
                                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                                            .fill(
                                                captureManager.selectedResolution == resolution
                                                    ? Color.accentColor
                                                    : Color(uiColor: UIColor.tertiarySystemFill)
                                            )
                                    )
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }

            Divider()

            sliderRow(
                icon: "film",
                label: "帧率",
                value: .init(
                    get: { Double(captureManager.fps) },
                    set: { captureManager.fps = Int($0) }
                ),
                range: 15...60,
                step: 1,
                display: "\(captureManager.fps) FPS"
            )

            Divider()

            sliderRow(
                icon: "speaker.wave.2.fill",
                label: "音量",
                value: $captureManager.volume,
                range: 0...1,
                step: 0.01,
                display: "\(Int(captureManager.volume * 100))%"
            )
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

    private func sliderRow(icon: String,
                           label: String,
                           value: Binding<Double>,
                           range: ClosedRange<Double>,
                           step: Double,
                           display: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: icon)
                    .foregroundStyle(Color.secondaryText)
                    .frame(width: 24)
                Text(label)
                    .font(.subheadline)
                    .foregroundStyle(Color.secondaryText)
                Spacer()
                Text(display)
                    .font(.subheadline)
                    .fontWeight(.semibold)
                    .monospacedDigit()
                    .foregroundStyle(Color.accentColor)
            }

            Slider(value: value, in: range, step: step)
                .tint(Color.accentColor)
        }
    }

    // MARK: - 响应式尺寸计算
    private var previewAspectRatio: CGFloat {
        let size = captureManager.selectedResolution.size
        return size.width / size.height
    }

    private func previewHeight(width: CGFloat, screenHeight: CGFloat) -> CGFloat {
        let calculatedHeight = width / previewAspectRatio

        // 在小屏/横屏设备上限制最大高度，避免内容被挤出
        let maxHeightRatio: CGFloat = verticalSizeClass == .compact ? 0.45 : 0.38
        let maxHeight = screenHeight * maxHeightRatio

        return min(calculatedHeight, maxHeight)
    }

    private var statusColor: Color {
        switch webRTCManager.statusMessage {
        case "WebRTC 已连接":
            return .green
        case "已连接信令服务器", "等待电脑端响应...":
            return .orange
        case "未连接", "已断开":
            return .gray
        default:
            return .secondary
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
