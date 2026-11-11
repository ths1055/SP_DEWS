#!/usr/bin/env python3
"""
datasets 폴더를 훑어서 실제로 있는 데이터셋만 넣은 train/data.yaml 자동 생성
  python tools/make_yaml.py --root E:/datasets
"""
import argparse
import os

# 이름: (train 이미지 경로, val 이미지 경로)  — root 기준 상대경로
KNOWN = {
    "roboflow_drone": ("roboflow_drone/train/images", "roboflow_drone/valid/images"),
    "dut":            ("dut/images/train", "dut/images/val"),
    "antiuav":        ("antiuav/images/train", "antiuav/images/val"),
    "mavvid":         ("mavvid/train/images", "mavvid/val/images"),
    "negatives":      ("negatives/images", None),
    "negatives_val":  (None, "negatives_val/images"),   # 검증용 네거티브 (오탐을 점수에 반영)
}
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp")


def count(root, rel):
    if rel is None:
        return 0, False
    d = os.path.join(root, rel)
    if not os.path.isdir(d):
        return 0, False
    n = sum(1 for f in os.listdir(d) if f.lower().endswith(IMG_EXT))
    # Ultralytics는 경로의 마지막 'images'를 'labels'로 바꿔서 라벨을 찾음
    lbl = os.path.join(root, rel.replace("images", "labels")) if "images" in rel else None
    return n, bool(lbl and os.path.isdir(lbl))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "train", "data.yaml"))
    args = ap.parse_args()
    root = os.path.abspath(args.root).replace("\\", "/")

    train, val = [], []
    print(f"root: {root}\n")
    sets = dict(KNOWN)
    # roboflow로 시작하는 폴더는 전부 자동 포함 (roboflow_drone, roboflow_sky 등)
    for d in sorted(os.listdir(root)):
        if d.lower().startswith("roboflow") and d not in sets and os.path.isdir(os.path.join(root, d)):
            sets[d] = (f"{d}/train/images", f"{d}/valid/images")
    for name, (tr, va) in sets.items():
        nt, lt = count(root, tr)
        nv, lv = count(root, va)
        if tr is None:  # 검증 전용 폴더
            if nv and lv:
                val.append(va)
                print(f"  [O] {name:15s} val {nv}장 (검증 전용)")
            else:
                print(f"  [ ] {name:15s} 없음")
            continue
        if nt == 0:
            print(f"  [ ] {name:15s} 없음")
            continue
        if not lt:
            print(f"  [!] {name:15s} 이미지 {nt}장은 있는데 labels 폴더가 없음 → 제외")
            continue
        train.append(tr)
        msg = f"  [O] {name:15s} train {nt}장"
        if nv and lv:
            val.append(va)
            msg += f" / val {nv}장"
        print(msg)

    if not train:
        raise SystemExit("\n쓸 수 있는 데이터셋이 하나도 없음. START_HERE.md 폴더 구조 확인")
    if not val:
        raise SystemExit("\nval 데이터가 없음. roboflow_drone/valid 등 하나는 있어야 함")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# tools/make_yaml.py 가 자동 생성\n")
        f.write(f"path: {root}\n\ntrain:\n")
        f.writelines(f"  - {p}\n" for p in train)
        f.write("val:\n")
        f.writelines(f"  - {p}\n" for p in val)
        f.write("\nnames:\n  0: drone\n")
    print(f"\nwrote {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
