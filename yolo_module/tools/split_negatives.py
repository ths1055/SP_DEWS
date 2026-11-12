#!/usr/bin/env python3
"""
negatives 일부(기본 15%)를 negatives_val 로 옮겨서 검증에도 오탐 사진을 넣음
→ 모델이 새·헬기를 드론으로 잡으면 검증 점수(precision)가 떨어져서, 조기종료·best 선택이 오탐까지 반영함

  python tools/split_negatives.py --root D:/datasets --ratio 0.15
한 번만 실행 (다시 실행하면 남은 negatives에서 또 15%를 옮김)
"""
import argparse
import os
import random
import shutil


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--ratio", type=float, default=0.15)
    args = ap.parse_args()

    src_i, src_l = os.path.join(args.root, "negatives", "images"), os.path.join(args.root, "negatives", "labels")
    dst_i, dst_l = os.path.join(args.root, "negatives_val", "images"), os.path.join(args.root, "negatives_val", "labels")
    os.makedirs(dst_i, exist_ok=True)
    os.makedirs(dst_l, exist_ok=True)

    imgs = sorted(f for f in os.listdir(src_i) if f.lower().endswith((".jpg", ".jpeg", ".png")))
    # 같은 영상의 연속 프레임이 train/val에 갈라지지 않게 '영상 단위'로 나눔
    groups = {}
    for f in imgs:
        key = f.rsplit("_", 1)[0]  # hn_heli4_00123 -> hn_heli4
        groups.setdefault(key, []).append(f)
    keys = sorted(groups)
    random.seed(0)
    random.shuffle(keys)
    target = int(len(imgs) * args.ratio)
    moved = 0
    for k in keys:
        if moved >= target:
            break
        for f in groups[k]:
            stem = os.path.splitext(f)[0]
            shutil.move(os.path.join(src_i, f), os.path.join(dst_i, f))
            lbl = os.path.join(src_l, stem + ".txt")
            if os.path.exists(lbl):
                shutil.move(lbl, os.path.join(dst_l, stem + ".txt"))
            moved += 1
        print(f"  → val: {k} ({len(groups[k])}장)")
    print(f"moved {moved} / {len(imgs)} to negatives_val")
    for c in ("labels.cache",):
        p = os.path.join(args.root, "negatives", c)
        if os.path.exists(p):
            os.remove(p)


if __name__ == "__main__":
    main()
