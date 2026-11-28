"""
test_alert.py — alert_tx ↔ alert_rx 하드웨어 없는 시험. `python test_alert.py`

E22 대신 서로 연결된 가짜 모듈 2개로 확인하는 것:
  우선순위 순서, 경보 유실 → 재전송, ACK 유실 → 중복 제거 + 재회신, GUI로 UDP JSON 전달
"""
import json
import socket
import threading
import time

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # e22·protocol은 상위 modules/ 에 있음
from alert_rx import AlertRx
from alert_tx import AlertTx
from fake_lora import pair
from protocol import Alert


def alert(level: int, track: int) -> Alert:
    return Alert(node_id=0, seq=0, track_id=track, level=level, stereo=True,
                 dist_m=10.0, speed_mps=1.0, tta_s=None, az_deg=0.0, el_deg=0.0)


tx_lora, rx_lora = pair()

gui = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
gui.bind(("127.0.0.1", 0))
gui.settimeout(1.0)

tx = AlertTx(tx_lora, node_id=3, ack_timeout=0.05, retries=2)
rx = AlertRx(rx_lora, gui.getsockname())
got: list[Alert] = []
stop = threading.Event()


def wait_got(n: int) -> None:
    """rx는 ACK를 보낸 뒤 목록에 추가하므로 flush() 직후엔 마지막 건이 아직 없을 수 있음."""
    t_end = time.monotonic() + 1.0
    while len(got) < n and time.monotonic() < t_end:
        time.sleep(0.005)


def rx_loop():
    while not stop.is_set():
        got.extend(rx.poll())


threading.Thread(target=rx_loop, daemon=True).start()

# 1) 시작 전에 넣어 두면 등급 높은 것부터 나가야 함 (같은 등급은 넣은 순서)
for lv, tr in [(1, 10), (1, 11), (3, 12), (2, 13)]:
    assert tx.submit(alert(lv, tr))
tx_lora.drop = 1      # 첫 경보의 첫 송신 유실 → 재전송
tx.start()
tx.flush()
wait_got(4)
assert [a.track_id for a in got] == [12, 13, 10, 11], [a.track_id for a in got]

# 2) ACK 유실 → 같은 seq 재전송 → 지휘소는 중복으로 걸러내고 다시 ACK
rx_lora.drop = 1
tx.submit(alert(2, 14))
tx.flush()
wait_got(5)
stop.set()
tx.close()

assert tx.stats == {"acked": 5, "failed": 0, "retries": 2, "dropped": 0, "superseded": 0}, tx.stats
assert rx.stats == {"alerts": 5, "dups": 1}, rx.stats
assert len({a.seq for a in got}) == 5 and all(a.node_id == 3 for a in got)

msgs = [json.loads(gui.recv(1024)) for _ in range(5)]
assert [m["track_id"] for m in msgs] == [12, 13, 10, 11, 14], msgs

# 3) 큐가 가득 차면 버리고 False
full = AlertTx(tx_lora, maxsize=1)
assert full.submit(alert(1, 1)) and not full.submit(alert(1, 2)) and full.stats["dropped"] == 1

# 4) 같은 표적은 최신 경보만: 기다리는 동안 새 경보가 오면 옛 것은 건너뜀
#    (큐가 등급 순이라 옛 경보가 나중에 도착하면 지휘소에 옛 상태가 남음:
#     경계→위험인데 경계로 표시, 사라졌다 다시 잡혔는데 '없음'으로 지워짐)
a4, b4 = pair()
rx4, got4, stop4 = AlertRx(b4, ("127.0.0.1", 9)), [], threading.Event()


def rx4_loop():
    while not stop4.is_set():
        got4.extend(rx4.poll())


threading.Thread(target=rx4_loop, daemon=True).start()
tx4 = AlertTx(a4, node_id=7, ack_timeout=0.05, retries=1)
for lv, tr in [(1, 20), (2, 21), (3, 21), (0, 22), (1, 22)]:   # 21: 경계→위험, 22: 없음→다시 잡힘
    assert tx4.submit(alert(lv, tr))
tx4.start()
tx4.flush()
t_end = time.monotonic() + 1.0
while len(got4) < 3 and time.monotonic() < t_end:
    time.sleep(0.005)
stop4.set()
tx4.close()
final = {a.track_id: a.level for a in got4}                    # 지휘소에 마지막으로 남은 등급
assert final == {20: 1, 21: 3, 22: 1} and len(got4) == 3, [(a.track_id, a.level) for a in got4]
assert tx4.stats["superseded"] == 2 and tx4.stats["acked"] == 3, tx4.stats

print("test_alert.py 시험 모두 통과:", tx.stats, rx.stats)
