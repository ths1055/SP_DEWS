import Foundation
import Network

/// 경고 레벨이 바뀔 때 Pi(udp_receiver.py)로 JSON 전송
final class AlertSender {
    private let conn: NWConnection

    init(host: String, port: UInt16) {
        conn = NWConnection(host: NWEndpoint.Host(host),
                            port: NWEndpoint.Port(rawValue: port)!,
                            using: .udp)
        conn.start(queue: .global(qos: .userInitiated))
    }

    func send(level: ThreatLevel, drones: [TrackedDrone]) {
        let payload: [String: Any] = [
            "t": Date().timeIntervalSince1970,
            "level": level.rawValue,
            "drones": drones.map { d -> [String: Any] in
                [
                    "id": d.id,
                    "conf": Double(d.confidence),
                    "cx": Double(d.box.midX),          // 0~1, 좌상단 원점으로 변환
                    "cy": Double(1 - d.box.midY),
                    "w": Double(d.box.width),
                    "h": Double(d.box.height),
                    "approaching": d.approaching,
                ]
            },
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
        conn.send(content: data, completion: .contentProcessed { _ in })
    }
}
