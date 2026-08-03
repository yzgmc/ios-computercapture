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

    @State private var isSharing = false
    // 传输模式：lan / usb
    @State private var transportMode = "lan"
    // 原画质传输（无压缩 TCP）
    @State private var rawStreamEnabled = true
    @State private var rawStreamHost = "192.168.1.100"  // USB 模式时此值忽略
    @State private var rawStreamPort = "5000"
    @State private var isDiscovering = false
    private let rawStreamServer = RawStreamServer()
    private let discoveryClient = DiscoveryClient()

    // UDP 音频传输
    @State private var audioStreamEnabled = true
    @State private var audioStreamPort = "5001"
    private let audioStreamServer = AudioStreamServer()

    @State private var statusMessage = "未连接"

    var body: some View {
        NavigationView {
            GeometryReader { geometry in
                ZStack {
                    Color.appBackground
                        .ignoresSafeArea()

                    ScrollView(showsIndicators: false) {
                        VStack(spacing: 16) {
                            previewCard(in: geometry)
                            captureSettingsCard()
                            rawStreamCard()
                            audioStreamCard()
                            statusBar()
                        }
                        .padding(.horizontal)
                        .padding(.top, 8)
                        // 底部为固定连接按钮预留空间
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

            CameraPreviewView(captureManager: captureManager)
                .frame(maxWidth: .infinity)
                .frame(height: previewHeight(for: geometry.size.width - 32, in: geometry))
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                .shadow(color: .black.opacity(0.08), radius: 8, x: 0, y: 4)
        }
        .padding()
        .background(Color.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.04), radius: 12, x: 0, y: 6)
    }

    // MARK: - 采集参数卡片（分辨率/帧率，本机控制）
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
                .disabled(isSharing)
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
                .disabled(isSharing)
            }

            Divider()

            // 压缩模式：JPEG 85 (推荐) 或 BGRA 无压缩
            HStack {
                Text("压缩模式")
                    .font(.subheadline)
                    .foregroundStyle(Color.secondaryText)
                Spacer()
                Picker("压缩", selection: $captureManager.useJPEGCompression) {
                    Text("JPEG 85 (1080p60)").tag(true)
                    Text("BGRA 无压缩").tag(false)
                }
                .pickerStyle(.menu)
                .disabled(isSharing)
            }
        }
        .padding()
        .background(Color.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.04), radius: 12, x: 0, y: 6)
    }

    // MARK: - 原画质视频传输卡片（TCP）
    @ViewBuilder
    private func rawStreamCard() -> some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 8) {
                Image(systemName: "bolt.horizontal.fill")
                    .foregroundStyle(Color.accentColor)
                    .frame(width: 22)
                Text("原画质视频（无压缩 TCP）")
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
                // 传输模式选择：局域网 / USB 直连
                Picker("传输模式", selection: $transportMode) {
                    Text("局域网").tag("lan")
                    Text("USB 直连").tag("usb")
                }
                .pickerStyle(.segmented)
                .disabled(isSharing)

                if transportMode == "usb" {
                    HStack(spacing: 6) {
                        Image(systemName: "cable.connector")
                            .foregroundStyle(.secondary)
                        Text("USB 模式：iPhone 通过 USB 共享通道连接电脑，桌面端自动桥接。")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                HStack(spacing: 12) {
                    Image(systemName: "desktopcomputer")
                        .foregroundStyle(Color.secondaryText)
                        .frame(width: 24)
                    TextField(transportMode == "usb" ? "127.0.0.1 (USB)" : "桌面 IP",
                              text: $rawStreamHost)
                        .textFieldStyle(.roundedBorder)
                        .keyboardType(.decimalPad)
                        .autocapitalization(.none)
                        .disableAutocorrection(true)
                        .disabled(isSharing || transportMode == "usb")
                    if transportMode == "lan" {
                        Button {
                            autoDiscover()
                        } label: {
                            if isDiscovering {
                                ProgressView().controlSize(.small)
                            } else {
                                Image(systemName: "antenna.radiowaves.left.and.right")
                            }
                        }
                        .buttonStyle(.bordered)
                        .disabled(isSharing || isDiscovering)
                    }
                }
                HStack(spacing: 12) {
                    Image(systemName: "dot.radiowaves.left.and.right")
                        .foregroundStyle(Color.secondaryText)
                        .frame(width: 24)
                    TextField("TCP 端口", text: $rawStreamPort)
                        .textFieldStyle(.roundedBorder)
                        .keyboardType(.numberPad)
                        .disableAutocorrection(true)
                        .disabled(isSharing)
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

    // MARK: - UDP 音频传输卡片
    @ViewBuilder
    private func audioStreamCard() -> some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 8) {
                Image(systemName: "waveform")
                    .foregroundStyle(Color.accentColor)
                    .frame(width: 22)
                Text("麦克风音频（PCM UDP）")
                    .font(.headline)
                    .foregroundStyle(Color.primaryText)
                Spacer()
                Toggle("启用", isOn: $audioStreamEnabled)
                    .toggleStyle(.switch)
                    .tint(Color.accentBlue)
                    .labelsHidden()
                Text(audioStreamEnabled ? "已启用" : "关闭")
                    .font(.caption)
                    .foregroundStyle(Color.secondaryText)
            }

            if audioStreamEnabled {
                Divider()
                HStack(spacing: 12) {
                    Image(systemName: "dot.radiowaves.left.and.right")
                        .foregroundStyle(Color.secondaryText)
                        .frame(width: 24)
                    TextField("UDP 端口", text: $audioStreamPort)
                        .textFieldStyle(.roundedBorder)
                        .keyboardType(.numberPad)
                        .disableAutocorrection(true)
                        .disabled(isSharing)
                }
                Text("格式：48kHz / mono / 16-bit PCM。UDP 不可靠，丢包会产生短暂跳变。")
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

            Text(statusMessage)
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
            if isSharing {
                disconnect()
            } else {
                connect()
            }
        }) {
            HStack(spacing: 10) {
                Image(systemName: isSharing ? "stop.fill" : "play.fill")
                Text(isSharing ? "停止共享" : "开始共享")
                    .font(.headline)
            }
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 16)
            .background(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(isSharing ? Color.accentRed : Color.accentBlue)
            )
            .shadow(color: (isSharing ? Color.accentRed : Color.accentBlue).opacity(0.35),
                    radius: 12, x: 0, y: 6)
        }
        .buttonStyle(.plain)
    }

    // MARK: - 响应式尺寸计算
    private var previewAspectRatio: CGFloat {
        let size = captureManager.actualCaptureSize
        guard size.height > 0 else { return 16.0 / 9.0 }
        return size.width / size.height
    }

    private func previewHeight(for cardWidth: CGFloat, in geometry: GeometryProxy) -> CGFloat {
        let naturalHeight = cardWidth / previewAspectRatio
        return min(naturalHeight, geometry.size.height * 0.5)
    }

    private var statusColor: Color {
        isSharing ? .green : .gray
    }

    // MARK: - 业务逻辑
    private func connect() {
        Task {
            // USB 模式：iOS 端通过 usbmuxd 桥接到 desktop 的 127.0.0.1，
            // 数据流走 USB 物理通道。LAN 模式：iOS 主动连接 desktop 的局域网 IP。
            let effectiveHost = (transportMode == "usb") ? "127.0.0.1" : rawStreamHost

            // 启动 TCP 连接（不等 ready，先发起）
            var tcpReady = false
            var udpReady = false
            if rawStreamEnabled, let port = UInt16(rawStreamPort) {
                captureManager.rawStreamServer = rawStreamServer
                rawStreamServer.start(host: effectiveHost, port: port) { ready in
                    tcpReady = ready
                }
            }
            if audioStreamEnabled, let port = UInt16(audioStreamPort) {
                captureManager.audioStreamServer = audioStreamServer
                audioStreamServer.start(host: effectiveHost, port: port) { ready in
                    udpReady = ready
                }
            }
            // 等待 TCP 真正 ready（最多 5 秒），再启动 capture
            let deadline = Date().addingTimeInterval(5.0)
            while (!tcpReady || (rawStreamEnabled && !udpReady)) && Date() < deadline {
                try? await Task.sleep(nanoseconds: 50_000_000)
            }
            if !tcpReady {
                await MainActor.run {
                    if transportMode == "usb" {
                        statusMessage = "USB 直连失败，请确认桌面端已切换到 USB 模式且 iPhone 已连接 USB"
                    } else {
                        statusMessage = "连接桌面端失败，请检查 IP/端口"
                    }
                }
                rawStreamServer.stop()
                audioStreamServer.stop()
                return
            }
            await captureManager.startCapture()
            await MainActor.run {
                isSharing = true
                let mode = transportMode == "usb" ? "USB" : rawStreamHost
                statusMessage = "正在共享 → \(mode):\(rawStreamPort)/\(audioStreamPort)"
            }
        }
    }

    private func disconnect() {
        Task {
            rawStreamServer.stop()
            captureManager.rawStreamServer = nil
            audioStreamServer.stop()
            captureManager.audioStreamServer = nil
            await captureManager.stopCapture()
            await MainActor.run {
                isSharing = false
                statusMessage = "已停止"
            }
        }
    }

    /// 自动发现桌面端：UDP 广播 PHONECAM_DISCOVER 等待回包。
    private func autoDiscover() {
        isDiscovering = true
        statusMessage = "正在搜索桌面端..."
        discoveryClient.discover { [self] result in
            DispatchQueue.main.async {
                isDiscovering = false
                switch result {
                case .success(let endpoint):
                    self.rawStreamHost = endpoint.host
                    self.rawStreamPort = String(endpoint.tcpPort)
                    self.audioStreamPort = String(endpoint.udpPort)
                    self.statusMessage = "已发现桌面端: \(endpoint.host)"
                case .failure(let err):
                    self.statusMessage = "未发现桌面端: \(err.localizedDescription)"
                }
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
