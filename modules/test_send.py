"""
test_send.py — 송신 시험 (관측소 역할)

  python test_send.py --raw                  1단계: 글자 그대로 보내기
  python test_send.py --count 100            2단계: 경보 패킷 100개 보내기
  python test_send.py --no-gpio --port COM3  노트북에서 실행할 때
"""
import argparse
import time

from e22 import E22
from protocol import Alert, SeqCounter, encode_alert, now_ms16

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="/dev/serial0")
ap.add_argument("--no-gpio", action="store_true")
ap.add_argument("--raw", action="store_true", help="패킷 대신 글자 전송")
ap.add_argument("--count", type=int, default=100, help="256 이하 (seq가 0~255 순환)")
ap.add_argument("--interval", type=float, default=1.0, help="전송 간격(초)")
ap.add_argument("--node", type=int, default=1)
args = ap.parse_args()

with E22(args.port, use_gpio=not args.no_gpio) as lora:
    seq = SeqCounter()
    for i in range(args.count):
        if args.raw:
            frame = f"hello {i}\n".encode()
        else:
            dist = 40.0 - (i % 40)                     # 40 m → 1 m 로 다가오는 가짜 드론
            level = 3 if dist <= 10 else 2 if dist <= 20 else 1
            a = Alert(node_id=args.node, seq=seq.next(), track_id=7, level=level,
                      stereo=True, dist_m=dist, speed_mps=5.0, tta_s=dist / 5.0,
                      az_deg=-12.5, el_deg=8.0, ts_ms=now_ms16())
            frame = encode_alert(a)

        t0 = time.monotonic()
        lora.send(frame)
        lora.wait_aux(2.0)                             # 무선 송신이 끝날 때까지
        dt = (time.monotonic() - t0) * 1000
        print(f"[{i + 1}/{args.count}] {len(frame)} B, 송신 {dt:.0f} ms | {frame.hex(' ')}")
        time.sleep(args.interval)
