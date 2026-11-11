#!/usr/bin/env python3
"""
Anti-UAV (RGB) → YOLO 포맷
원본 구조: <src>/<영상폴더>/visible.mp4 + visible.json  {"exist":[...], "gt_rect":[[x,y,w,h],...]}

  python convert_antiuav.py --src Anti-UAV/train --out datasets/antiuav --split train --step 10
  python convert_antiuav.py --src Anti-UAV/val   --out datasets/antiuav --split val   --step 10

--step: N프레임마다 1장 (연속 프레임은 거의 똑같아서 다 쓰면 용량만 커지고 과적합 남)
드론 없는 프레임은 --neg-ratio 비율만큼 빈 라벨(배경)으로 저장
"""
import argparse
import json
import os
import random

import cv2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--split", default="train")
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--neg-ratio", type=float, default=0.3)
    args = ap.parse_args()

    img_dir = os.path.join(args.out, "images", args.split)
    lbl_dir = os.path.join(args.out, "labels", args.split)
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)
    random.seed(0)
    total = pos = 0

    for vid in sorted(os.listdir(args.src)):
        vdir = os.path.join(args.src, vid)
        mp4, js = os.path.join(vdir, "visible.mp4"), os.path.join(vdir, "visible.json")
        if not (os.path.exists(mp4) and os.path.exists(js)):
            continue
        ann = json.load(open(js))
        exist, rects = ann.get("exist", []), ann.get("gt_rect", [])
        cap = cv2.VideoCapture(mp4)
        i = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if i % args.step == 0 and i < len(exist):
                H, W = frame.shape[:2]
                has = exist[i] == 1 and i < len(rects) and len(rects[i]) == 4 and rects[i][2] > 0
                if has or random.random() < args.neg_ratio:
                    name = f"{vid}_{i:05d}"
                    cv2.imwrite(os.path.join(img_dir, name + ".jpg"), frame,
                                [cv2.IMWRITE_JPEG_QUALITY, 92])
                    with open(os.path.join(lbl_dir, name + ".txt"), "w") as f:
                        if has:
                            x, y, w, h = rects[i]
                            f.write(f"0 {(x + w / 2) / W:.6f} {(y + h / 2) / H:.6f} {w / W:.6f} {h / H:.6f}\n")
                            pos += 1
                    total += 1
            i += 1
        cap.release()
        print(f"{vid}: {i} frames")
    print(f"done: {total} images ({pos} with drone)")


if __name__ == "__main__":
    main()
