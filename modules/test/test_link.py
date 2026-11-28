"""
test_link.py — 표시 지연·전달률 측정 (통신 목표: 지휘소 표시 2초 이내, 재전송 포함 전달률 95% 이상)

관측소 쪽에서 실행. 지휘소는 alert_rx.py(또는 gui.py --lora)를 먼저 켜 둔다 — 지휘소 쪽 코드는 그대로.
경보 1개를 보내고 ACK가 올 때까지(재전송 포함) 걸린 시간을 잰다.
  지연: 지휘소는 경보를 받은 뒤 ACK를 보내므로, 이 시간 ≥ 실제 표시 지연 (보수적인 값)
  전달률: ACK를 끝내 못 받으면 실패 (경보는 갔는데 ACK만 잃었어도 실패로 셈 → 역시 보수적)

  python test/test_link.py --count 100                            Pi (관측소)
  python test/test_link.py --no-gpio --port COM17                 노트북을 관측소로 쓸 때
  python test/test_link.py --count 50 --label 20m --csv link.csv  결과 요약을 CSV에 한 줄 추가 (거리별 기록)
  python test/test_link.py --selftest                             하드웨어 없이 (가짜 E22)
"""
import argparse
import csv
import math
import statistics
import threading
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # e22·protocol은 상위 modules/ 에 있음
from alert_tx import ACK_TIMEOUT_S, RETRIES, AlertTx
from protocol import LEVEL_DANGER, Alert

GOAL_S = 2.0      # 지휘소 표시 2초 이내
GOAL_RATE = 0.95  # 재전송 포함 전달률 95% 이상


def measure(tx: AlertTx, count: int, interval: float, quiet: bool = False) -> list[tuple[bool, float, int]]:
    """경보를 하나씩 보내고 끝날 때까지 기다림. [(ACK 받음, 걸린 초, 재전송 횟수)]"""
    results = []
    try:
        for i in range(count):
            before = dict(tx.stats)
            t0 = time.monotonic()
            tx.submit(Alert(node_id=0, seq=0, track_id=1 + i % 255, level=LEVEL_DANGER, stereo=True,
                            dist_m=20.0, speed_mps=5.0, tta_s=4.0, az_deg=0.0, el_deg=0.0))
            tx.flush()                               # 송신 + ACK 대기 + 재전송까지 끝날 때까지
            dt = time.monotonic() - t0
            ok = tx.stats["acked"] > before["acked"]
            retries = tx.stats["retries"] - before["retries"]
            results.append((ok, dt, retries))
            if not quiet:
                print(f"[{i + 1}/{count}] {'ACK ' if ok else '실패'} {dt * 1000:5.0f} ms | 재전송 {retries}")
            time.sleep(max(0.0, interval - dt))
    except KeyboardInterrupt:                        # 중간에 멈춰도 그때까지 결과는 남김
        print(f"\n중단됨 — {len(results)}개까지 집계")
    return results


def summarize(results: list[tuple[bool, float, int]]) -> dict:
    n = len(results)
    lat = sorted(dt for ok, dt, _ in results if ok)
    p95 = lat[math.ceil(0.95 * len(lat)) - 1] if lat else math.inf   # 순위 기준 95번째 백분위
    rate = len(lat) / n if n else 0.0
    return {
        "n": n,
        "rate": rate,
        "p50_s": statistics.median(lat) if lat else math.inf,
        "p95_s": p95,
        "max_s": lat[-1] if lat else math.inf,
        "within_goal": sum(dt <= GOAL_S for dt in lat) / n if n else 0.0,
        "retries": sum(r for *_, r in results),
        "pass": rate >= GOAL_RATE and p95 <= GOAL_S,
    }


def report(s: dict) -> None:
    print("\n===== 링크 측정 결과 =====")
    print(f"전달률 {s['rate'] * 100:.1f}% ({round(s['rate'] * s['n'])}/{s['n']})  목표 {GOAL_RATE * 100:.0f}% 이상")
    print(f"지연(송신→ACK) 중앙값 {s['p50_s'] * 1000:.0f} ms | 95% {s['p95_s'] * 1000:.0f} ms "
          f"| 최대 {s['max_s'] * 1000:.0f} ms  목표 {GOAL_S:.0f} s 이내")
    print(f"{GOAL_S:.0f}초 안에 도착한 비율 {s['within_goal'] * 100:.1f}% | 재전송 합계 {s['retries']}회")
    print("판정:", "통과" if s["pass"] else "미달")


def append_csv(path: str, label: str, s: dict, ack_timeout: float, retries: int) -> None:
    new = not Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["label", "count", "rate", "p50_ms", "p95_ms", "max_ms", "within_2s", "retries",
                        "ack_timeout_s", "retries_setting", "pass"])
        w.writerow([label, s["n"], f"{s['rate']:.3f}", round(s["p50_s"] * 1000), round(s["p95_s"] * 1000),
                    round(s["max_s"] * 1000), f"{s['within_goal']:.3f}", s["retries"],
                    ack_timeout, retries, s["pass"]])


def selftest() -> None:
    # 계산: 20개 중 19개 성공, 모두 2초 안 → 95%로 통과 / 하나가 2.5초면 95번째 백분위가 넘어서 미달
    ok19 = [(True, 0.3, 0)] * 19
    s = summarize(ok19 + [(False, 1.8, 2)])
    assert s["rate"] == 0.95 and s["pass"] and s["retries"] == 2, s
    s = summarize(ok19[:18] + [(True, 2.5, 1), (True, 2.6, 1)])
    assert s["rate"] == 1.0 and not s["pass"] and s["max_s"] == 2.6, s
    assert summarize([(False, 1.0, 2)])["pass"] is False

    # 가짜 E22 2개로 실제 흐름: 경보 유실 → 재전송 성공, ACK 전부 유실 → 실패로 셈
    from alert_rx import AlertRx
    from fake_lora import pair

    tx_lora, rx_lora = pair()
    rx = AlertRx(rx_lora, ("127.0.0.1", 9))          # GUI 대신 버려지는 포트
    stop = threading.Event()

    def rx_loop():
        while not stop.is_set():
            rx.poll()

    threading.Thread(target=rx_loop, daemon=True).start()
    tx = AlertTx(tx_lora, node_id=1, ack_timeout=0.05, retries=2)
    tx.start()

    tx_lora.drop = 1                                 # 첫 경보 첫 송신 유실
    r = measure(tx, 3, 0.0, quiet=True)
    assert [ok for ok, *_ in r] == [True] * 3 and [n for *_, n in r] == [1, 0, 0], r

    rx_lora.drop = 3                                 # ACK 3번 다 유실 → 관측소는 실패로 봄
    r = measure(tx, 1, 0.0, quiet=True)
    assert r[0][0] is False and r[0][2] == 2, r
    stop.set()
    tx.close()
    rx.close()
    print("test_link.py 자체 시험 통과")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="표시 지연·전달률 측정 (관측소에서 실행, 지휘소는 alert_rx.py)")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--no-gpio", action="store_true")
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--interval", type=float, default=1.0, help="경보 간격(초)")
    ap.add_argument("--node", type=int, default=1)
    ap.add_argument("--ack-timeout", type=float, default=ACK_TIMEOUT_S)
    ap.add_argument("--retries", type=int, default=RETRIES)
    ap.add_argument("--label", default="", help="CSV에 남길 이름 (예: 20m, 벽1개)")
    ap.add_argument("--csv", help="결과 요약을 이 CSV 파일에 한 줄 추가")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        raise SystemExit

    from e22 import E22

    with E22(args.port, use_gpio=not args.no_gpio) as lora:
        tx = AlertTx(lora, args.node, args.ack_timeout, args.retries)
        tx.start()
        results = measure(tx, args.count, args.interval)
        tx.close()

    if results:
        s = summarize(results)
        report(s)
        if args.csv:
            append_csv(args.csv, args.label, s, args.ack_timeout, args.retries)
            print(f"CSV에 추가: {args.csv}")
