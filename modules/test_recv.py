"""
test_recv.py — 수신 시험 (지휘소 역할). Ctrl+C로 끝내면 전달률 출력.

  python test_recv.py --raw                  1단계: 받은 바이트 그대로 출력
  python test_recv.py --count 100            2단계: 패킷 풀어서 출력 + 전달률 계산
  python test_recv.py --no-gpio --port COM4  노트북에서 실행할 때
"""
import argparse

from e22 import E22
from protocol import LEVEL_NAMES, Ack, Alert, PacketParser

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="/dev/serial0")
ap.add_argument("--no-gpio", action="store_true")
ap.add_argument("--raw", action="store_true")
ap.add_argument("--count", type=int, default=100, help="송신 측에서 보낸 개수")
args = ap.parse_args()

parser = PacketParser()
seen: set[int] = set()
dups = 0

with E22(args.port, use_gpio=not args.no_gpio) as lora:
    print("수신 대기 중... (Ctrl+C로 종료)")
    try:
        while True:
            data = lora.recv()
            if not data:
                continue
            if args.raw:
                print(repr(data))
                continue
            for pkt in parser.feed(data):
                if isinstance(pkt, Alert):
                    if pkt.seq in seen:
                        dups += 1
                    seen.add(pkt.seq)
                    tta = "-" if pkt.tta_s is None else f"{pkt.tta_s:.1f}s"
                    print(f"seq {pkt.seq:3d} | 노드 {pkt.node_id} ID {pkt.track_id} "
                          f"| {LEVEL_NAMES[pkt.level]} | {pkt.dist_m:.2f} m "
                          f"({'stereo' if pkt.stereo else 'mono'}) | {pkt.speed_mps:+.2f} m/s "
                          f"| 도달 {tta} | 방위 {pkt.az_deg:+.1f}° 고각 {pkt.el_deg:+.1f}°")
                elif isinstance(pkt, Ack):
                    print(f"ACK 노드 {pkt.node_id} seq {pkt.seq}")
    except KeyboardInterrupt:
        pass

if not args.raw:
    n = len(seen)
    missing = sorted(set(range(args.count)) - seen)
    print("\n===== 결과 =====")
    print(f"수신 {n}/{args.count} → 전달률 {n / args.count * 100:.1f}%")
    print(f"CRC 오류 {parser.crc_errors}회, 중복 {dups}회")
    if missing:
        print(f"못 받은 seq: {missing[:30]}{' ...' if len(missing) > 30 else ''}")
