#!/usr/bin/env python3
"""
여러 모델을 여러 영상에 돌려서 경고 결과를 표로 비교 (화면 없이 빠르게)

  python tools/compare_models.py --models runs/detect/drone_v2-3/weights/best.pt runs/detect/drone_v3/weights/best.pt runs/detect/drone_v3/weights/last.pt ^
      --videos heli3.mp4 plane3.mp4 bird_test.mp4 drone_test1.mp4 drone_test4.mp4 drone_test5.mp4

영상 이름에 'drone'이 들어가면 드론 영상(잡아야 함), 아니면 오탐 영상(잡으면 안 됨)으로 판단
"""
import argparse
import os
import sys

import cv2
import numpy as np
import supervision as sv
from trackers import ByteTrackTracker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pi"))
from detect_pi import UltralyticsDetector, ThreatJudge  # noqa: E402


def run(weights, video, conf):
    det = UltralyticsDetector(weights, 640, conf)
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        print(f"  ⚠ 영상을 열 수 없음: {video} (파일 이름/위치 확인) → 건너뜀")
        return None
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps < 1 or fps > 240:  # 메타데이터 이상한 영상 대비
        fps = 30.0
    tracker = ByteTrackTracker(frame_rate=int(fps), track_activation_threshold=0.35,
                               high_conf_det_threshold=0.4, lost_track_buffer=int(fps))
    judge = ThreatJudge()
    n = n_box = n_det = n_warn = changes = 0
    last = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        boxes, scores, classes = det.detect(frame)
        if len(boxes):
            d = sv.Detections(xyxy=boxes.astype(np.float32), confidence=scores.astype(np.float32),
                              class_id=classes.astype(int))
            n_box += 1
        else:
            d = sv.Detections.empty()
        level, _ = judge.update(tracker.update(d), frame.shape[:2], n / fps)
        n_det += level >= 1
        n_warn += level == 2
        changes += level != last
        last = level
        n += 1
    cap.release()
    return n, n_box, n_det, n_warn, changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--videos", nargs="+", required=True)
    ap.add_argument("--conf", type=float, default=0.35)
    args = ap.parse_args()

    def short(p):
        parts = os.path.normpath(p).split(os.sep)
        return f"{parts[-3]}/{parts[-1]}" if len(parts) >= 3 else p

    rows = []
    for m in args.models:
        for v in args.videos:
            r = run(m, v, args.conf)
            if r is None:
                continue
            n, nb, nd, nw, ch = r
            rows.append((short(m), os.path.basename(v), n, nb, nd, nw, ch))
            print(f"done {short(m)} {os.path.basename(v)}")

    print("\n박스% = 한 번이라도 박스 친 프레임 비율 / 경고% = DETECTED 이상 유지된 프레임 비율")
    print("드론 영상은 높을수록, 오탐 영상은 낮을수록 좋음. 전환 = 경고 단계 바뀐 횟수(적을수록 안정)\n")
    print(f"{'model':28s} {'video':18s} {'박스%':>6s} {'경고%':>6s} {'WARN%':>6s} {'전환':>4s}  판정")
    for m, v, n, nb, nd, nw, ch in rows:
        drone = "drone" in v.lower()
        pb, pd, pw = 100 * nb / max(n, 1), 100 * nd / max(n, 1), 100 * nw / max(n, 1)
        if drone:
            verdict = "좋음" if pd >= 70 else ("보통" if pd >= 40 else "놓침 많음")
        else:
            verdict = "좋음" if pd <= 3 else ("보통" if pd <= 15 else "오탐 많음")
        print(f"{m:28s} {v:18s} {pb:6.1f} {pd:6.1f} {pw:6.1f} {ch:4d}  {verdict}")
    print()


if __name__ == "__main__":
    main()
