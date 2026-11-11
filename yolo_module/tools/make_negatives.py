#!/usr/bin/env python3
"""
새·비행기·하늘 사진 등 '드론 아닌' 이미지를 배경(빈 라벨)으로 추가
  python tools/make_negatives.py --src 다운받은_새사진_폴더 --out E:/datasets/negatives --max 3000
하위 폴더까지 다 훑어서 이미지만 복사하고, 같은 이름의 빈 .txt 라벨을 만듦
"""
import argparse
import os
import random
import shutil

IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max", type=int, default=3000, help="드론 이미지의 10~20% 정도가 적당")
    args = ap.parse_args()

    files = [os.path.join(d, f) for d, _, fs in os.walk(args.src) for f in fs if f.lower().endswith(IMG_EXT)]
    random.seed(0)
    random.shuffle(files)
    files = files[:args.max]
    img_dir, lbl_dir = os.path.join(args.out, "images"), os.path.join(args.out, "labels")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)
    for i, p in enumerate(files):
        name = f"neg_{i:06d}"
        shutil.copy(p, os.path.join(img_dir, name + os.path.splitext(p)[1].lower()))
        open(os.path.join(lbl_dir, name + ".txt"), "w").close()
    print(f"done: {len(files)} negatives")


if __name__ == "__main__":
    main()
