import Foundation
import CoreMedia

/// 视频流传输抽象。RawStreamServer（TCP）与 SRTStreamServer（SRT）均遵从此协议，
/// CaptureManager 通过协议持有传输实现，运行时可切换。
///
/// 实现需保证：
/// - 所有 processXxx 方法在 isRunning=false 时为 no-op；
/// - onBackpressure/onClientConnected 在传输内部串行队列上回调；
/// - stop() 幂等，可重复调用。
protocol VideoStreamTransport: AnyObject {
    /// 是否正在传输（已连接 / 已监听）。
    var isRunning: Bool { get }

    /// 背压等级变化回调。CaptureManager 据此动态调整码率。
    var onBackpressure: ((BackpressureLevel) -> Void)? { get set }

    /// 客户端（重）连接回调。CaptureManager 应强制下一帧 IDR。
    var onClientConnected: (() -> Void)? { get set }

    /// caller 模式：主动连接远端 host:port。onReady 在连接就绪时回调。
    func start(host: String, port: UInt16, onReady: ((Bool) -> Void)?)

    /// listener 模式：监听 port 等待远端连接（USB/SRT-passive 场景）。
    /// SRTStreamServer 不支持此模式，会调用 onReady?(false)。
    func startServer(port: UInt16, onReady: ((Bool) -> Void)?)

    /// 停止传输并释放资源。幂等。
    func stop()

    /// 发送 BGRA 原始帧。format=0。
    func processSampleBuffer(_ sampleBuffer: CMSampleBuffer,
                             requiresBGRAConversion: Bool)

    /// 发送 JPEG 帧。format=10。
    func processJPEGFrame(_ jpegData: Data, width: Int, height: Int)

    /// 发送 H.264 帧。format=20。
    func processH264Frame(_ h264Data: Data, width: Int, height: Int,
                          isKeyframe: Bool)
}
