#!/usr/bin/env python3
"""
스테레오 캘리브레이션용 체커보드 사진 촬영 (Pi에서 실행)

  python3 capture_calib.py --out calib_imgs            # 모니터 연결: SPACE로 저장, q 종료
  python3 capture_calib.py --out calib_imgs --auto     # SSH 등 화면 없을 때: 양쪽 다 보이면 2초마다 자동 저장

체커보드는 '안쪽 꼭짓점' 개수 기준 (기본 9x6 = 칸 10x7)
20~40쌍 정도, 거리(1~4m)·각도·화면 위치(가장자리 포함)를 골고루 바꿔가며 찍기
"""
import argparse
import os
import time

import cv2
import numpy as np

from stereo_cam import StereoCamera


def find(gray, pattern):
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_FAST_CHECK
    ok, _ = cv2.findChessboardCorners(gray, pattern, flags)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="calib_imgs")
    ap.add_argument("--cols", type=int, default=9)
    ap.add_argument("--rows", type=int, default=6)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--swap", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    pattern = (args.cols, args.rows)
    cam = StereoCamera((args.width, args.height), fps=15, swap=args.swap)
    n = len([f for f in os.listdir(args.out) if f.startswith("left_")])
    last = 0.0
    print(f"저장 위치: {args.out} (기존 {n}쌍)")

    try:
        while True:
            l, r, skew, _ = cam.read()
            # 빠른 확인용으로 축소해서 검사
            sl = cv2.resize(cv2.cvtColor(l, cv2.COLOR_BGR2GRAY), None, fx=0.5, fy=0.5)
            sr = cv2.resize(cv2.cvtColor(r, cv2.COLOR_BGR2GRAY), None, fx=0.5, fy=0.5)
            okl, okr = find(sl, pattern), find(sr, pattern)
            both = okl and okr

            save = False
            if args.auto:
                save = both and time.time() - last > args.interval
            else:
                view = np.hstack([cv2.resize(l, None, fx=0.5, fy=0.5), cv2.resize(r, None, fx=0.5, fy=0.5)])
                txt = f"L:{'O' if okl else 'X'} R:{'O' if okr else 'X'} skew {skew:.1f}ms saved {n}"
                cv2.putText(view, txt, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                            (0, 255, 0) if both else (0, 0, 255), 2)
                cv2.imshow("calib (SPACE save / q quit)", view)
                k = cv2.waitKey(1) & 0xFF
                if k == ord("q"):
                    break
                save = k == ord(" ") and both

            if save:
                cv2.imwrite(f"{args.out}/left_{n:03d}.png", l)
                cv2.imwrite(f"{args.out}/right_{n:03d}.png", r)
                print(f"saved pair {n} (skew {skew:.1f} ms)")
                n += 1
                last = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        cam.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
