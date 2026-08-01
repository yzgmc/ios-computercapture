import SwiftUI

struct ContentView: View {
    @StateObject private var captureManager = CaptureManager()
    @StateObject private var webRTCManager = WebRTCManager()

    @State private var serverURL = "ws://192.168.1.100:8080"
    @State private var roomID = "room1"
    @State private var isConnected = false

    var body: some View {
        NavigationView {
            VStack(spacing: 20) {
                // 摄像头预览
                CameraPreviewView(captureManager: captureManager)
                    .frame(height: 300)
                    .cornerRadius(12)
                    .padding(.horizontal)

                // 连接设置
                VStack(alignment: .leading, spacing: 12) {
                    Text("信令服务器")
                        .font(.headline)
                    TextField("ws://...", text: $serverURL)
                        .textFieldStyle(RoundedBorderTextFieldStyle())
                        .autocapitalization(.none)

                    Text("房间 ID")
                        .font(.headline)
                    TextField("room1", text: $roomID)
                        .textFieldStyle(RoundedBorderTextFieldStyle())
                }
                .padding(.horizontal)

                // 参数控制
                VStack(alignment: .leading, spacing: 12) {
                    Text("分辨率")
                        .font(.headline)
                    Picker("分辨率", selection: $captureManager.selectedResolution) {
                        ForEach(CaptureResolution.allCases) { resolution in
                            Text(resolution.label).tag(resolution)
                        }
                    }
                    .pickerStyle(SegmentedPickerStyle())

                    HStack {
                        Text("帧率: \(captureManager.fps)")
                        Slider(value: .init(
                            get: { Double(captureManager.fps) },
                            set: { captureManager.fps = Int($0) }
                        ), in: 15...60, step: 1)
                    }

                    HStack {
                        Text("音量: \(Int(captureManager.volume * 100))%")
                        Slider(value: $captureManager.volume, in: 0...1)
                    }
                }
                .padding(.horizontal)

                Spacer()

                // 连接按钮
                Button(action: {
                    if isConnected {
                        disconnect()
                    } else {
                        connect()
                    }
                }) {
                    Text(isConnected ? "断开连接" : "开始共享")
                        .font(.headline)
                        .foregroundColor(.white)
                        .frame(maxWidth: .infinity)
                        .padding()
                        .background(isConnected ? Color.red : Color.blue)
                        .cornerRadius(12)
                }
                .padding(.horizontal)

                Text(webRTCManager.statusMessage)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            .navigationTitle("PhoneCam")
        }
    }

    private func connect() {
        Task {
            await webRTCManager.connect(signalingURL: serverURL, roomID: roomID)
            await captureManager.startCapture()
            await webRTCManager.publish(videoTrack: captureManager.videoTrack,
                                         audioTrack: captureManager.audioTrack)
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

struct CameraPreviewView: UIViewRepresentable {
    let captureManager: CaptureManager

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        Task {
            await captureManager.setupPreview(in: view)
        }
        return view
    }

    func updateUIView(_ uiView: UIView, context: Context) {}
}
