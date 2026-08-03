import Foundation
import Network

/// 局域网自动发现客户端：UDP broadcast 发送 PHONECAM_DISCOVER 等待回包。
/// 桌面端 IP 自动填入 TextField，省去用户手动配置。
final class DiscoveryClient {
    static let magic = "PHONECAM_DISCOVER".data(using: .utf8)!
    static let port: UInt16 = 50000
    static let broadcastTimeout: TimeInterval = 3.0

    struct Endpoint {
        let host: String
        let tcpPort: UInt16
        let udpPort: UInt16
    }

    private var connection: NWConnection?

    /// 发送一次广播并等待第一个回包，返回 (ip, tcp, udp) 端点。
    /// 使用 NWConnection 的 UDP 模式，timeout 由调用方控制。
    func discover(completion: @escaping (Result<Endpoint, Error>) -> Void) {
        let host = NWEndpoint.Host("255.255.255.255")
        guard let port = NWEndpoint.Port(rawValue: Self.port) else {
            completion(.failure(NSError(domain: "Discovery", code: -1,
                userInfo: [NSLocalizedDescriptionKey: "Invalid port"])))
            return
        }
        let conn = NWConnection(host: host, port: port, using: .udp)
        let params = NWParameters.udp
        params.allowLocalEndpointReuse = true
        let finalConn = NWConnection(host: host, port: port, using: params)

        finalConn.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                self?.sendMagicAndReceive(on: finalConn, completion: completion)
            case .failed(let err):
                completion(.failure(err))
            case .cancelled:
                break
            default:
                break
            }
        }
        finalConn.start(queue: .global(qos: .userInitiated))
        connection = finalConn

        // 3 秒超时
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + Self.broadcastTimeout) {
            if finalConn.state == .ready {
                finalConn.cancel()
                completion(.failure(NSError(domain: "Discovery", code: -2,
                    userInfo: [NSLocalizedDescriptionKey: "未发现桌面端"])))
            }
        }
    }

    private func sendMagicAndReceive(on conn: NWConnection,
                                     completion: @escaping (Result<Endpoint, Error>) -> Void) {
        conn.send(content: Self.magic, completion: .contentProcessed { error in
            if let error = error {
                completion(.failure(error))
                return
            }
            conn.receiveMessage { data, _, _, recvError in
                if let recvError = recvError {
                    completion(.failure(recvError))
                    return
                }
                guard let data = data,
                      let str = String(data: data, encoding: .utf8) else {
                    completion(.failure(NSError(domain: "Discovery", code: -3,
                        userInfo: [NSLocalizedDescriptionKey: "回包格式错误"])))
                    return
                }
                let parts = str.split(separator: "|").map(String.init)
                guard parts.count == 3, let tcp = UInt16(parts[1]), let udp = UInt16(parts[2]) else {
                    completion(.failure(NSError(domain: "Discovery", code: -4,
                        userInfo: [NSLocalizedDescriptionKey: "回包字段错误: \(str)"])))
                    return
                }
                conn.cancel()
                completion(.success(Endpoint(host: parts[0], tcpPort: tcp, udpPort: udp)))
            }
        })
    }
}
