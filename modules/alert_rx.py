"""
alert_rx.py — 경보 수신 (지휘소, ⑧ → ⑨ → ⑩)

받은 경보마다 즉시 ACK 회신 → (node_id, seq)로 중복 제거 → 새 경보만 로컬 UDP JSON으로 GUI에 전달.
장치가 LoRa 하나뿐이라 메인 루프가 E22를 독점한다 (스레드 불필요). GUI는 별도 프로세스(UDP).

  python alert_rx.py                              Pi에서
  python alert_rx.py --no-gpio --port COM4        노트북에서
  GUI 없이 확인: 출력만 보면 됨 (UDP는 받는 쪽이 없어도 그냥 버려짐)
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import asdict

from protocol import LEVEL_NAMES, Alert, PacketParser, encode_ack

GUI_ADDR = ("127.0.0.1", 5005)
DUP_WINDOW_S = 10.0   # 재전송은 2 s 안에 끝남. seq는 256개마다 돌아오므로 이보다 오래된 같은 seq는 새 경보


class AlertRx:
    def __init__(self, lora, gui_addr=GUI_ADDR):
        self.lora = lora
        self.gui_addr = gui_addr
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._parser = PacketParser()
        self._seen: dict[tuple[int, int], float] = {}   # 키는 노드당 최대 256개라 커지지 않음
        self.stats = {"alerts": 0, "dups": 0}

    def poll(self) -> list[Alert]:
        """한 번 읽고 처리. 새로 받은 경보 목록 반환."""
        new = []
        for p in self._parser.feed(self.lora.recv()):
            if not isinstance(p, Alert):
                continue
            try:
                self.lora.send(encode_ack(p.node_id, p.seq))   # 중복이어도 회신: 앞선 ACK가 유실됐을 수 있음
            except (TimeoutError, OSError) as e:
                print(f"ACK 송신 실패: {e}")
            now = time.monotonic()
            key = (p.node_id, p.seq)
            last = self._seen.get(key)
            self._seen[key] = now
            if last is not None and now - last < DUP_WINDOW_S:
                self.stats["dups"] += 1
                continue
            self.stats["alerts"] += 1
            self._sock.sendto(json.dumps(asdict(p)).encode(), self.gui_addr)
            new.append(p)
        return new

    def close(self) -> None:
        self._sock.close()


if __name__ == "__main__":
    import argparse

    from e22 import E22

    ap = argparse.ArgumentParser(description="경보 수신 (지휘소)")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--no-gpio", action="store_true")
    ap.add_argument("--gui-port", type=int, default=GUI_ADDR[1])
    args = ap.parse_args()

    with E22(args.port, use_gpio=not args.no_gpio) as lora:
        rx = AlertRx(lora, (GUI_ADDR[0], args.gui_port))
        print("수신 대기 중... (Ctrl+C로 종료)")
        try:
            while True:
                for a in rx.poll():
                    if a.level == 0 and a.track_id == 0:     # 관측소 생존 신호 (표적 없을 때 5초마다, alert_gen)
                        print(f"노드 {a.node_id} seq {a.seq:3d} | 생존 신호")
                        continue
                    tta = "-" if a.tta_s is None else f"{a.tta_s:.1f}s"
                    print(f"노드 {a.node_id} seq {a.seq:3d} | ID {a.track_id} | {LEVEL_NAMES[a.level]} "
                          f"| {a.dist_m:.2f} m | {a.speed_mps:+.2f} m/s | 도달 {tta}")
        except KeyboardInterrupt:
            pass
        rx.close()
    print(f"\n경보 {rx.stats['alerts']}건, 중복(재전송) {rx.stats['dups']}건")
