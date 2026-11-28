"""
alert_tx.py — 경보 송신 (관측소, ⑥ → ⑦)

구조: E22는 LoRa 스레드 하나만 만진다. 다른 스레드(alert_gen 등)는 submit()으로
PriorityQueue에 넣기만 하고, LoRa 스레드가 높은 등급부터 꺼내 송신 → ACK 대기 → 재전송.

  위험(3) > 경계(2) > 관심(1) > 없음(0), 같은 등급은 넣은 순서대로
  같은 track_id는 최신 경보만: 기다리는 동안 더 새 경보가 들어오면 옛 것은 건너뜀 (stats["superseded"])
    → 등급 순서 때문에 옛 경보가 나중에 도착해 지휘소에 옛 상태가 남는 일이 없음
  재전송은 같은 seq·같은 프레임 그대로 (지휘소가 (node_id, seq)로 중복 제거)
  큐가 가득 차면 새 경보를 버리고 stats["dropped"] 증가

사용 예
  tx = AlertTx(E22(), node_id=1); tx.start()
  tx.submit(Alert(node_id=0, seq=0, ...))   # node_id·seq·ts_ms는 송신 직전에 채움
  tx.close()

실기 시험
  python alert_tx.py --count 20                Pi에서 (지휘소에서 alert_rx.py 먼저 실행)
  python alert_tx.py --no-gpio --port COM3     노트북에서
"""
from __future__ import annotations

import itertools
import os
import queue
import random
import threading
import time
from dataclasses import replace

from protocol import Ack, Alert, PacketParser, SeqCounter, encode_alert, now_ms16

# shortcut: 실측 전 추정값 (경보+ACK 약 0.4~0.5 s 추정, 2.4 kbps). test/test_link.py 실측 후 조정
#   ack_timeout: 경보+ACK 실측보다 넉넉히 (짧으면 ACK 오는 중에 또 보내서 채널을 두 배로 씀)
#   마지막 재전송의 표시 시각 ≈ retries × ack_timeout + 송신 시간 → 2 s 안 (0.8 × 2 + 0.3 = 1.9 s)
#   코드 안 고치고 바꾸기: SP_ACK_TIMEOUT=1.0 SP_RETRIES=1 python3 detect_stereo.py ...
ACK_TIMEOUT_S = float(os.environ.get("SP_ACK_TIMEOUT", "0.8"))
RETRIES = int(os.environ.get("SP_RETRIES", "2"))


class AlertTx:
    def __init__(self, lora, node_id: int = 1, ack_timeout: float = ACK_TIMEOUT_S,
                 retries: int = RETRIES, maxsize: int = 16):
        self.lora = lora
        self.node_id = node_id
        self.ack_timeout = ack_timeout
        self.retries = retries
        self._q: queue.PriorityQueue = queue.PriorityQueue(maxsize)
        self._order = itertools.count()       # 같은 등급끼리 넣은 순서 유지 (Alert끼리 비교 안 하게)
        self._seq = SeqCounter(random.randrange(256))   # 재시작 직후 이전 seq와 겹쳐 중복 처리되지 않게
        self._parser = PacketParser()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lora-tx", daemon=True)
        self._latest: dict[int, int] = {}     # track_id → 가장 최근에 넣은 경보의 순번
        self.stats = {"acked": 0, "failed": 0, "retries": 0, "dropped": 0, "superseded": 0}

    # -- 다른 스레드에서 호출
    def start(self) -> None:
        self._thread.start()

    def submit(self, alert: Alert) -> bool:
        """송신 대기열에 넣음. 가득 차 있으면 False."""
        order = next(self._order)
        try:
            self._q.put_nowait((-alert.level, order, alert))
        except queue.Full:                    # 버린 경보는 최신으로 치지 않음 (기다리던 옛 경보는 그대로 나감)
            self.stats["dropped"] += 1
            return False
        self._latest[alert.track_id] = order
        return True

    def flush(self) -> None:
        """대기열이 빌 때까지 대기 (송신·ACK 대기 포함)."""
        self._q.join()

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    # -- LoRa 스레드
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                _, order, a = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if order < self._latest.get(a.track_id, order):   # 같은 표적의 더 새 경보가 기다리는 중
                    self.stats["superseded"] += 1
                    continue
                self._deliver(replace(a, node_id=self.node_id, seq=self._seq.next(), ts_ms=now_ms16()))
            except Exception as e:            # 한 건 실패로 송신 스레드가 죽지 않게
                self.stats["failed"] += 1
                print(f"송신 오류: {e}")
            finally:
                self._q.task_done()

    def _deliver(self, a: Alert) -> None:
        frame = encode_alert(a)
        for attempt in range(1 + self.retries):
            if attempt:
                self.stats["retries"] += 1
            self.lora.send(frame)
            if self._wait_ack(a.seq):
                self.stats["acked"] += 1
                return
        self.stats["failed"] += 1
        print(f"ACK 없음: seq {a.seq} ({1 + self.retries}회 송신)")

    def _wait_ack(self, seq: int) -> bool:
        t_end = time.monotonic() + self.ack_timeout
        while time.monotonic() < t_end:
            for p in self._parser.feed(self.lora.recv()):
                if isinstance(p, Ack) and p.node_id == self.node_id and p.seq == seq:
                    return True
        return False


# ---------------------------------------------------------------- 실기 시험
if __name__ == "__main__":
    import argparse

    from e22 import E22

    ap = argparse.ArgumentParser(description="경보 송신 시험 (ACK·재전송 포함)")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--no-gpio", action="store_true")
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--node", type=int, default=1)
    ap.add_argument("--ack-timeout", type=float, default=ACK_TIMEOUT_S)
    ap.add_argument("--retries", type=int, default=RETRIES)
    args = ap.parse_args()

    with E22(args.port, use_gpio=not args.no_gpio) as lora:
        tx = AlertTx(lora, args.node, args.ack_timeout, args.retries)
        tx.start()
        try:
            for i in range(args.count):
                dist = 40.0 - (i % 40)                 # 40 m → 1 m 로 다가오는 가짜 드론
                level = 3 if dist <= 10 else 2 if dist <= 20 else 1
                tx.submit(Alert(node_id=0, seq=0, track_id=7, level=level, stereo=True,
                                dist_m=dist, speed_mps=5.0, tta_s=dist / 5.0,
                                az_deg=-12.5, el_deg=8.0))
                time.sleep(args.interval)
            tx.flush()
        except KeyboardInterrupt:
            pass
        tx.close()
    s = tx.stats
    print(f"\n===== 결과 =====\nACK {s['acked']}/{args.count} | 실패 {s['failed']} "
          f"| 재전송 {s['retries']} | 큐 초과 {s['dropped']}")
