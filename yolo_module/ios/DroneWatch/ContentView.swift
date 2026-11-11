import SwiftUI

struct ContentView: View {
    @StateObject private var camera = CameraManager()
    @StateObject private var detector = DroneDetector()

    // Pi IP로 바꿔서 사용 (같은 Wi-Fi)
    private let sender = AlertSender(host: "192.168.0.10", port: 5005)
    private let haptic = UINotificationFeedbackGenerator()

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .top) {
                CameraPreview(session: camera.session)

                ForEach(detector.drones) { d in
                    let r = toView(d.box, view: geo.size, image: detector.imageSize)
                    ZStack(alignment: .topLeading) {
                        Rectangle()
                            .stroke(d.approaching ? Color.red : Color.yellow, lineWidth: 2)
                        Text("#\(d.id) \(String(format: "%.2f", d.confidence))")
                            .font(.caption2.monospaced())
                            .foregroundStyle(.white)
                            .padding(2)
                            .background(.black.opacity(0.6))
                            .offset(y: -16)
                    }
                    .frame(width: r.width, height: r.height)
                    .position(x: r.midX, y: r.midY)
                }

                banner
                    .padding(.top, geo.safeAreaInsets.top + 8)
            }
        }
        .ignoresSafeArea()
        .onAppear {
            haptic.prepare()
            camera.onFrame = { [detector] pb in detector.process(pb) }
            detector.onLevelChange = { [sender, haptic] lvl, drones in
                if lvl == .warning { haptic.notificationOccurred(.warning) }
                sender.send(level: lvl, drones: drones)
            }
            camera.start()
        }
        .onDisappear { camera.stop() }
    }

    private var banner: some View {
        HStack {
            Text(detector.level.name).bold()
            Spacer()
            Text("\(detector.drones.count) tgt · \(Int(detector.fps)) fps")
                .font(.caption.monospaced())
        }
        .foregroundStyle(.white)
        .padding(10)
        .background(color(detector.level).opacity(0.85), in: RoundedRectangle(cornerRadius: 10))
        .padding(.horizontal, 12)
    }

    private func color(_ l: ThreatLevel) -> Color {
        switch l {
        case .clear: return .green
        case .detected: return .orange
        case .warning: return .red
        }
    }

    /// Vision 정규화 좌표(좌하단 원점) → 화면 좌표 (미리보기가 aspectFill 이라 같은 방식으로 변환)
    private func toView(_ bb: CGRect, view: CGSize, image: CGSize) -> CGRect {
        let scale = max(view.width / image.width, view.height / image.height)
        let ox = (view.width - image.width * scale) / 2
        let oy = (view.height - image.height * scale) / 2
        return CGRect(x: bb.minX * image.width * scale + ox,
                      y: (1 - bb.maxY) * image.height * scale + oy,
                      width: bb.width * image.width * scale,
                      height: bb.height * image.height * scale)
    }
}
