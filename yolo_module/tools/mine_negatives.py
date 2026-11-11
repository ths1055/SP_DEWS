#!/usr/bin/env python3
"""
하드 네거티브 수집: 드론이 '없는' 영상(새, 비행기, 벌레 등)에서 모델이 잘못 잡은 프레임만 뽑아서
빈 라벨(배경)로 저장 → 재학습하면 그 오탐을 집중적으로 고침

  python tools/mine_negatives.py --weights runs/detect/drone_v1/weights/best.pt ^
      --videos bird1.mp4 bird2.mp4 --out D:/datasets/negatives

주의: 영상에 진짜 드론이 나오면 안 됨 (드론까지 '배경'으로 배워버림)
"""
import argparse
import os

import cv2
from ultralytics import YOLO


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True)
    ap.add_argument("--videos", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--conf", type=float, default=0.25, help="이 이상으로 잡힌 프레임을 오탐으로 수집")
    ap.add_argument("--gap", type=int, default=5, help="연속 프레임은 거의 같으니 최소 N프레임 간격으로 저장")
    ap.add_argument("--also-clean", type=int, default=30, help="오탐 없는 프레임도 N프레임마다 1장 저장(0이면 안 함)")
    args = ap.parse_args()

    img_dir, lbl_dir = os.path.join(args.out, "images"), os.path.join(args.out, "labels")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)
    model = YOLO(args.weights)
    start = len(os.listdir(img_dir))
    n_fp = n_clean = 0

    for v in args.videos:
        cap = cv2.VideoCapture(v)
        stem = os.path.splitext(os.path.basename(v))[0]
        i, last_saved = 0, -10**9
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            r = model.predict(frame, conf=args.conf, verbose=False)[0]
            fp = len(r.boxes) > 0
            save = (fp and i - last_saved >= args.gap) or \
                   (not fp and args.also_clean and i % args.also_clean == 0)
            if save:
                name = f"hn_{stem}_{i:05d}"
                cv2.imwrite(os.path.join(img_dir, name + ".jpg"), frame)
                open(os.path.join(lbl_dir, name + ".txt"), "w").close()
                if fp:
                    n_fp += 1
                    last_saved = i
                else:
                    n_clean += 1
            i += 1
        cap.release()
        print(f"{v}: {i} frames")
    print(f"saved {n_fp} false-positive frames + {n_clean} clean frames (folder now {start + n_fp + n_clean})")


if __name__ == "__main__":
    main()
