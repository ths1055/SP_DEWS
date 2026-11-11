#!/usr/bin/env python3
"""
Pascal VOC(xml) → YOLO 포맷  (DUT Anti-UAV detection 등)

  python convert_voc.py --imgs DUT/train/img --xmls DUT/train/xml --out datasets/dut --split train
  python convert_voc.py --imgs DUT/val/img   --xmls DUT/val/xml   --out datasets/dut --split val

xml 안의 객체 이름은 전부 drone(0)으로 통일. 받은 폴더 구조가 다르면 --imgs/--xmls 경로만 맞춰주면 됨
"""
import argparse
import glob
import os
import shutil
import xml.etree.ElementTree as ET


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--imgs", required=True)
    ap.add_argument("--xmls", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--split", default="train")
    args = ap.parse_args()

    img_dir = os.path.join(args.out, "images", args.split)
    lbl_dir = os.path.join(args.out, "labels", args.split)
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)

    imgs = {os.path.splitext(os.path.basename(p))[0]: p
            for p in glob.glob(os.path.join(args.imgs, "*")) if p.lower().endswith((".jpg", ".jpeg", ".png"))}
    n = 0
    for xp in sorted(glob.glob(os.path.join(args.xmls, "*.xml"))):
        stem = os.path.splitext(os.path.basename(xp))[0]
        if stem not in imgs:
            continue
        root = ET.parse(xp).getroot()
        W = float(root.find("size/width").text)
        H = float(root.find("size/height").text)
        lines = []
        for o in root.iter("object"):
            b = o.find("bndbox")
            x1, y1, x2, y2 = (float(b.find(k).text) for k in ("xmin", "ymin", "xmax", "ymax"))
            if x2 <= x1 or y2 <= y1:
                continue
            lines.append(f"0 {(x1 + x2) / 2 / W:.6f} {(y1 + y2) / 2 / H:.6f} {(x2 - x1) / W:.6f} {(y2 - y1) / H:.6f}")
        ext = os.path.splitext(imgs[stem])[1]
        shutil.copy(imgs[stem], os.path.join(img_dir, stem + ext))
        with open(os.path.join(lbl_dir, stem + ".txt"), "w") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        n += 1
    print(f"done: {n} images")


if __name__ == "__main__":
    main()
