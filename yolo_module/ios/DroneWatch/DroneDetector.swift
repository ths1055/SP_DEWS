import CoreML
import QuartzCore
import Vision

enum ThreatLevel: Int, Comparable {
    case clear = 0, detected = 1, warning = 2
    static func < (a: Self, b: Self) -> Bool { a.rawValue < b.rawValue }
    var name: String { ["CLEAR", "DETECTED", "WARNING"][rawValue] }
}

struct TrackedDrone: Identifiable {
    let id: Int
    var box: CGRect                 // Vision 정규화 좌표 (0~1, 좌하단 원점)
    var confidence: Float
    var hits: Int
    var lastSeen: TimeInterval
    var areaHistory: [(t: TimeInterval, a: CGFloat)]
    var approaching = false
}

/// CoreML YOLO 검출 + 간단한 IoU 트래커 + 경고 판정 (Pi 쪽 ThreatJudge와 같은 규칙)
final class DroneDetector: ObservableObject {
    @Published private(set) var drones: [TrackedDrone] = []
    @Published private(set) var level: ThreatLevel = .clear
    @Published private(set) var fps: Double = 0
    @Published private(set) var imageSize = CGSize(width: 1080, height: 1920)

    var onLevelChange: ((ThreatLevel, [TrackedDrone]) -> Void)?

    // 파라미터
    let confThreshold: Float = 0.35
    let minHits = 5                  // 이 프레임 수 이상 연속 추적돼야 확정
    let iouMatch: CGFloat = 0.2
    let maxLost: TimeInterval = 1.0
    let growWindow: TimeInterval = 1.0
    let growRatio: CGFloat = 1.3     // 1초 전보다 면적 1.3배 → 접근
    let nearArea: CGFloat = 0.02     // 화면의 2% 이상 → 가까움

    private var request: VNCoreMLRequest!
    private var tracks: [TrackedDrone] = []
    private var nextID = 0
    private var lastTime = CACurrentMediaTime()
    private var currentLevel: ThreatLevel = .clear

    init() {
        // drone.mlpackage 를 Xcode 프로젝트에 넣으면 빌드 시 drone.mlmodelc 로 컴파일됨
        guard let url = Bundle.main.url(forResource: "drone", withExtension: "mlmodelc"),
              let ml = try? MLModel(contentsOf: url),
              let vn = try? VNCoreMLModel(for: ml) else {
            fatalError("drone.mlpackage 가 프로젝트(타깃)에 추가됐는지 확인")
        }
        request = VNCoreMLRequest(model: vn) { [weak self] req, _ in self?.handle(req) }
        request.imageCropAndScaleOption = .scaleFill
    }

    /// 카메라 큐에서 호출됨 (동기 실행)
    func process(_ pb: CVPixelBuffer) {
        let size = CGSize(width: CVPixelBufferGetWidth(pb), height: CVPixelBufferGetHeight(pb))
        if size != imageSize { DispatchQueue.main.async { self.imageSize = size } }
        try? VNImageRequestHandler(cvPixelBuffer: pb, orientation: .up).perform([request])
    }

    private func handle(_ req: VNRequest) {
        let obs = (req.results as? [VNRecognizedObjectObservation] ?? [])
            .filter { $0.confidence >= confThreshold }
        let now = CACurrentMediaTime()

        updateTracks(obs, now: now)
        let lvl = evaluate(now: now)
        let visible = tracks.filter { $0.hits >= minHits && now - $0.lastSeen < 0.2 }

        let dt = max(now - lastTime, 1e-6)
        lastTime = now
        let changed = lvl != currentLevel
        currentLevel = lvl

        DispatchQueue.main.async {
            self.drones = visible
            self.fps = 0.9 * self.fps + 0.1 / dt
            self.level = lvl
            if changed { self.onLevelChange?(lvl, visible) }
        }
    }

    // 탐욕적 IoU 매칭 (드론 1~2대 수준이면 충분, 많아지면 ByteTrack 이식 고려)
    private func updateTracks(_ obs: [VNRecognizedObjectObservation], now: TimeInterval) {
        var unmatched = Array(obs.indices)
        for i in tracks.indices {
            var best = -1
            var bestIoU = iouMatch
            for j in unmatched {
                let v = iou(tracks[i].box, obs[j].boundingBox)
                if v > bestIoU { bestIoU = v; best = j }
            }
            guard best >= 0 else { continue }
            let o = obs[best]
            tracks[i].box = o.boundingBox
            tracks[i].confidence = o.confidence
            tracks[i].hits += 1
            tracks[i].lastSeen = now
            tracks[i].areaHistory.append((now, area(o.boundingBox)))
            tracks[i].areaHistory.removeAll { now - $0.t > 3 }
            unmatched.removeAll { $0 == best }
        }
        for j in unmatched {
            let o = obs[j]
            tracks.append(TrackedDrone(id: nextID, box: o.boundingBox, confidence: o.confidence,
                                       hits: 1, lastSeen: now,
                                       areaHistory: [(now, area(o.boundingBox))]))
            nextID += 1
        }
        tracks.removeAll { now - $0.lastSeen > maxLost }
    }

    private func evaluate(now: TimeInterval) -> ThreatLevel {
        var lvl = ThreatLevel.clear
        for i in tracks.indices where tracks[i].hits >= minHits && now - tracks[i].lastSeen < 0.5 {
            let recent = tracks[i].areaHistory.suffix(3).map { $0.a }.sorted()
            let cur = recent[recent.count / 2]                       // 최근 3개 중앙값
            let past = tracks[i].areaHistory.last { now - $0.t >= growWindow }?.a
            let approaching = past.map { cur / max($0, 1e-9) >= growRatio } ?? false
            tracks[i].approaching = approaching
            lvl = max(lvl, (approaching || cur >= nearArea) ? .warning : .detected)
        }
        return lvl
    }

    private func area(_ r: CGRect) -> CGFloat { r.width * r.height }

    private func iou(_ a: CGRect, _ b: CGRect) -> CGFloat {
        let inter = a.intersection(b)
        guard !inter.isNull else { return 0 }
        let i = area(inter)
        return i / (area(a) + area(b) - i)
    }
}
