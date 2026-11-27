"""
alert_gen.py — 경보 생성 (⑥, 관측소): 탐지·판정 결과 → LoRa 경보 (AlertTx 큐)

  python3 detect_stereo.py ... --lora           탐지하면서 지휘소로 LoRa 경보 (E22 /dev/serial0, 관측소 번호 --node-id)
  python alert_gen.py                           자체 시험 (하드웨어 없음, 실제 AlertTx/AlertRx + 가짜 E22)

입력: detect_stereo.RangeJudge가 매 프레임 주는 tracks
  {id, dist_m, src("stereo"|"mono"), closing_mps, tta_s, az_deg, el_deg, level(1 감지, 2 WARNING)}
  탐지 루프에서 바로 부름. 큐에 넣기만 해서 금방 끝나므로 ⑤ → ⑥ 사이 Mailbox는 필요 없음
출력: AlertTx.submit() — 큐에 넣기만 함 (송신·ACK·재전송은 alert_tx의 LoRa 스레드)

등급 (protocol.py) — RangeJudge는 1/2만 줘서 여기서 3단계로 나눔
  1 관심 = 잡힘
  2 경계 = 탐지 WARNING (감시선 40 m 안에서 2 m/s 이상 접근 등)
  3 위험 = 경고선(20 m) 안이거나 5초 안에 도달

전송 정책 (README 3-2)
  - 매 프레임(30 fps) 보내지 않음 → LoRa 대역폭 안에서
  - 새 표적·등급 변화는 바로, 같은 등급이면 관심 2초·경계 1초·위험 0.5초마다 갱신
  - 송신이 밀리면(끝나지 않은 경보 3건 이상) 같은 등급 갱신은 미룸
    → 큐에 옛날 위치가 쌓이지 않고, 풀리면 주기 대비 오래 기다린 표적부터 그때 최신 값으로 나감
  - 표적이 사라지면 '없음(0)' 한 번 → 지휘소가 바로 지움
  - 표적이 하나도 없으면 5초마다 생존 신호 = 등급 0 + track_id 0
    → 지휘소가 "드론 없음"과 "LoRa 끊김"을 구분 (gui.py: 15초 넘게 아무것도 안 오면 '신호 끊김')
  - track_id는 ByteTrack ID를 1~255로 접음 (0은 생존 신호 자리)
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from protocol import Alert

PERIOD_S = {1: 2.0, 2: 1.0, 3: 0.5}       # 같은 등급 갱신 주기
HEARTBEAT_S = 5.0                          # 표적 없을 때 생존 신호 주기 (0이면 안 보냄)
HEARTBEAT_TRACK = 0                        # 생존 신호의 track_id (실제 표적은 1~255)
BACKLOG_MAX = 3                            # 끝나지 않은 경보가 이만큼이면 같은 등급 갱신은 미룸


def pkt_track_id(tid) -> int:
    """ByteTrack ID → 패킷 track_id 1~255 (1~255는 그대로, 256부터 다시 1)"""
    return (int(tid) - 1) % 255 + 1


class AlertGen:
    def __init__(self, tx, warn_m: float = 20.0, tta_s: float = 5.0, period: Optional[dict] = None,
                 heartbeat_s: float = HEARTBEAT_S, backlog_max: int = BACKLOG_MAX, lora=None):
        self.tx, self.warn_m, self.tta_s = tx, warn_m, tta_s
        self.period = dict(PERIOD_S if period is None else period)
        self.heartbeat_s, self.backlog_max = heartbeat_s, backlog_max
        self._lora = lora                          # close() 때 같이 닫을 E22 (make_alert_gen이 연 경우)
        self._sent: dict[int, tuple[int, float]] = {}   # track_id → (마지막으로 보낸 등급, 시각)
        self._last = -1e9                          # 마지막으로 큐에 넣은 시각
        self.stats = {"sent": 0, "lost": 0, "heartbeat": 0, "deferred": 0, "queue_full": 0}
        # deferred = 송신이 밀려서 같은 등급 갱신을 미룬 프레임 수

    def level_of(self, tr: dict) -> int:
        """탐지 등급(1 감지, 2 WARNING) + 거리·도달 예상 → protocol 등급(1 관심, 2 경계, 3 위험)"""
        if tr.get("level", 0) <= 0:
            return 0
        d, t = tr.get("dist_m"), tr.get("tta_s")
        if (d is not None and d <= self.warn_m) or (t is not None and t <= self.tta_s):
            return 3
        return 2 if tr["level"] >= 2 else 1

    def backlog(self) -> int:
        """큐에 넣었는데 아직 안 끝난(대기 + 송신 중) 경보 수. AlertTx stats(acked + failed)로 셈"""
        s = getattr(self.tx, "stats", None) or {}
        return max(0, self.stats["sent"] - s.get("acked", 0) - s.get("failed", 0))

    def update(self, tracks: list, now: float) -> list:
        """매 프레임 호출. 이번에 큐에 넣은 경보 목록을 돌려줌 (대부분 프레임은 빈 목록)"""
        out, seen, due = [], set(), []
        for tr in tracks:
            lvl = self.level_of(tr)
            if lvl == 0:
                continue
            tid = pkt_track_id(tr["id"])
            seen.add(tid)
            if tr.get("dist_m") is None:           # 거리를 아직 모르면 기다림 (소실로 치지 않음)
                continue
            last = self._sent.get(tid)
            if last is not None and last[0] == lvl:            # 같은 등급 → 주기 갱신만 (아래에서 따로)
                late = (now - last[1]) / self.period[lvl]
                if late >= 1.0:
                    due.append((late, tid, lvl, tr))
                continue
            a = self._alert(tid, lvl, tr)                      # 새 표적·등급 변화 → 바로
            if self._submit(a, now):
                self._sent[tid] = (lvl, now)
                out.append(a)
        due.sort(key=lambda x: -x[0])                          # 송신이 밀리면 주기 대비 오래 기다린 표적부터
        for _, tid, lvl, tr in due:
            if self.backlog() >= self.backlog_max:
                self.stats["deferred"] += 1
                continue
            a = self._alert(tid, lvl, tr)
            if self._submit(a, now):
                self._sent[tid] = (lvl, now)
                out.append(a)
        for tid in [k for k in self._sent if k not in seen]:   # 사라진 표적 → 없음 한 번
            del self._sent[tid]
            self.stats["lost"] += 1
            a = self._zero(tid)
            if self._submit(a, now):
                out.append(a)
        if not self._sent and self.heartbeat_s and now - self._last >= self.heartbeat_s:
            a = self._zero(HEARTBEAT_TRACK)                    # 생존 신호
            if self._submit(a, now):
                self.stats["heartbeat"] += 1
                out.append(a)
        return out

    def close(self, timeout: float = 2.0) -> None:
        """남은 표적은 '없음'으로 정리하고, 큐를 최대 timeout초 비운 뒤 송신 스레드·E22 닫기"""
        now = time.monotonic()
        for tid in list(self._sent):
            self._submit(self._zero(tid), now)
        self._sent.clear()
        th = threading.Thread(target=self.tx.flush, daemon=True)
        th.start()
        th.join(timeout)
        self.tx.close()
        if self._lora is not None:
            self._lora.close()

    # ---- 안쪽
    @staticmethod
    def _alert(tid: int, lvl: int, tr: dict) -> Alert:
        c = tr.get("closing_mps")
        return Alert(node_id=0, seq=0, track_id=tid, level=lvl, stereo=tr.get("src") == "stereo",
                     dist_m=float(tr["dist_m"]), speed_mps=0.0 if c is None else float(c),
                     tta_s=tr.get("tta_s"), az_deg=float(tr.get("az_deg") or 0.0),
                     el_deg=float(tr.get("el_deg") or 0.0))        # node_id·seq·ts_ms는 AlertTx가 송신 직전에 채움

    @staticmethod
    def _zero(tid: int) -> Alert:
        return Alert(node_id=0, seq=0, track_id=tid, level=0, stereo=False, dist_m=0.0, speed_mps=0.0,
                     tta_s=None, az_deg=0.0, el_deg=0.0)

    def _submit(self, a: Alert, now: float) -> bool:
        if not self.tx.submit(a):                  # 큐가 가득 참 (AlertTx stats["dropped"]에도 셈) → 다음 프레임에 다시
            self.stats["queue_full"] += 1
            return False
        self.stats["sent"] += 1
        self._last = now
        return True


class NullGen:
    """--lora 안 줬거나 E22를 못 열었을 때. 탐지 코드에서 if 없이 쓰려고"""

    def update(self, *args, **kwargs) -> list:
        return []

    def close(self) -> None:
        pass


def make_alert_gen(port, node_id: int = 1, **kwargs):
    """detect_stereo --lora 값으로 만들기: None → 안 씀 / '/dev/serial0' 같은 포트 → E22 열고 송신 시작"""
    if not port:
        return NullGen()
    use_gpio = str(port).startswith(("/dev/serial", "/dev/ttyAMA"))   # Pi 내장 UART = GPIO 제어, USB-UART = 점퍼
    try:
        from alert_tx import AlertTx
        from e22 import E22
        lora = E22(port, use_gpio=use_gpio)
    except Exception as e:                         # 시리얼 꺼짐·GPIO 사용 중 등 → 탐지는 계속
        print(f"[lora] E22를 못 엶 ({type(e).__name__}: {e}) → LoRa 경보 없이 탐지만 계속 "
              f"(지휘소에는 '신호 끊김'으로 보임)")
        return NullGen()
    tx = AlertTx(lora, node_id=node_id)
    tx.start()
    print(f"[lora] 관측소 {node_id} → 지휘소 경보 송신 ({port}). 등급 바뀌면 바로, 같은 등급은 "
          f"{PERIOD_S[1]:g}/{PERIOD_S[2]:g}/{PERIOD_S[3]:g}초마다, 표적 없으면 {HEARTBEAT_S:g}초마다 생존 신호")
    return AlertGen(tx, lora=lora, **kwargs)


# ---------------------------------------------------------------- 자체 시험
if __name__ == "__main__":
    import json
    import queue
    import socket

    from alert_rx import AlertRx
    from alert_tx import AlertTx

    class FakeLora:
        """send()한 바이트가 상대의 recv()로 나옴 (test/test_alert.py와 같은 방식). airtime = 한 번 보내는 시간"""

        def __init__(self, airtime: float = 0.0):
            self.inbox: queue.Queue = queue.Queue()
            self.peer, self.airtime = None, airtime

        def send(self, data: bytes) -> None:
            time.sleep(self.airtime)
            self.peer.inbox.put(data)

        def recv(self) -> bytes:
            try:
                return self.inbox.get(timeout=0.005)
            except queue.Empty:
                return b""

    def station(node_id: int, airtime: float = 0.0, **tx_kw):
        """관측소 AlertTx ─가짜 무선─ 지휘소 AlertRx → UDP 소켓"""
        a, b = FakeLora(airtime), FakeLora(airtime)
        a.peer, b.peer = b, a
        gui = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        gui.bind(("127.0.0.1", 0))
        gui.settimeout(0.5)
        rx, stop = AlertRx(b, gui.getsockname()), threading.Event()
        threading.Thread(target=lambda: [rx.poll() for _ in iter(stop.is_set, True)], daemon=True).start()
        tx = AlertTx(a, node_id=node_id, **tx_kw)
        tx.start()
        return tx, gui, stop

    def received(gui, stop) -> list:
        msgs = []
        try:
            while True:
                msgs.append(json.loads(gui.recv(1024)))
        except socket.timeout:
            pass
        stop.set()
        return msgs

    # 1) 정책: 30 fps로 16초 (가상 시각)
    #    0~2초 관심(50 m) → 2~4초 경계(35 m, 3 m/s 접근) → 4~5초 위험(15 m) → 사라짐 → 표적 없음
    tx, gui, stop = station(4, ack_timeout=0.05, retries=2)
    gen = AlertGen(tx)
    frames = 0
    for k in range(16 * 30):
        t = k / 30
        if t < 5:
            d, lvl, c = (50.0, 1, 0.5) if t < 2 else (35.0, 2, 3.0) if t < 4 else (15.0, 2, 3.0)
            tracks = [dict(id=263, level=lvl, dist_m=d, src="stereo", closing_mps=c,
                           tta_s=d / c if c > 0.3 else None, az_deg=-12.5, el_deg=8.0)]
        else:
            tracks = []
        if gen.update(tracks, t):
            tx.flush()                             # 시험에서 순서 고정용 (실제 탐지 루프는 안 기다림)
        frames += 1
    gen.close()
    msgs = received(gui, stop)
    levels = [m["level"] for m in msgs]
    print(f"[정책] 프레임 {frames}개 → 경보 {len(msgs)}개: 등급 {levels}, 생성 {gen.stats}, 송신 {tx.stats}")
    assert levels == [1, 2, 2, 3, 3, 0, 0, 0], levels
    assert all(m["track_id"] == pkt_track_id(263) == 8 and m["node_id"] == 4 for m in msgs[:6])
    assert msgs[3]["dist_m"] == 15.0 and msgs[3]["tta_s"] == 5.0 and msgs[3]["stereo"] is True
    assert msgs[6]["track_id"] == msgs[7]["track_id"] == HEARTBEAT_TRACK and gen.stats["heartbeat"] == 2
    assert tx.stats["failed"] == 0 and [pkt_track_id(i) for i in (1, 255, 256, 510, 0)] == [1, 255, 1, 255, 255]

    # 2) 느린 무선 (한 번 보내는 데 0.15 s → 경보 + ACK 0.3 s): 위험 표적 3개가 0.5초마다 갱신하려 하면 넘침
    #    → 같은 등급 갱신을 미뤄서 큐가 안 쌓이고, 표적마다 고르게 최신 값이 나가야 함
    tx, gui, stop = station(5, airtime=0.15, ack_timeout=0.5, retries=1)
    gen = AlertGen(tx)
    t0, peak = time.monotonic(), 0
    while (now := time.monotonic()) - t0 < 3.0:
        tracks = [dict(id=i, level=2, dist_m=10.0 + i + (now - t0), src="stereo", closing_mps=4.0, tta_s=3.0,
                       az_deg=0.0, el_deg=0.0) for i in (1, 2, 3)]
        gen.update(tracks, now)
        peak = max(peak, gen.backlog())
        time.sleep(1 / 30)
    gen.close(timeout=4.0)
    msgs = received(gui, stop)
    per = {i: sum(m["track_id"] == i and m["level"] == 3 for m in msgs) for i in (1, 2, 3)}
    print(f"[느린 무선] 표적별 위험 경보 {per}, 최대 대기 {peak}, 생성 {gen.stats}, 송신 {tx.stats}")
    assert peak <= BACKLOG_MAX and gen.stats["deferred"] > 0 and gen.stats["queue_full"] == 0
    assert tx.stats["dropped"] == 0 and tx.stats["failed"] == 0 and max(per.values()) - min(per.values()) <= 1, per
    print("alert_gen.py 자체 시험 통과: 매 프레임 안 보냄, 등급 바뀌면 바로, 주기 갱신, 소실 '없음', 생존 신호, "
          "느린 무선에서 갱신 미룸")
