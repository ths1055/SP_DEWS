#!/usr/bin/env python3
"""
스테레오 캘리브레이션 → stereo_calib.npz  (Pi나 맥 어디서 돌려도 됨)

  python3 calibrate.py --imgs calib_imgs --square 25     # 체커보드 한 칸 25mm

결과 확인 기준
  - 개별/스테레오 RMS 오차 0.5px 이하면 좋음, 1px 넘으면 사진 다시 찍기
  - 출력되는 baseline이 실제 두 카메라 렌즈 간격(자로 잰 값)이랑 비슷해야 함
  - rectify_check.jpg 에서 초록 가로선이 양쪽 같은 지점을 지나가야 함
"""
import argparse
import glob
import os

import cv2
import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--imgs", default="calib_imgs")
    ap.add_argument("--cols", type=int, default=9)
    ap.add_argument("--rows", type=int, default=6)
    ap.add_argument("--square", type=float, required=True, help="체커보드 한 칸 크기 (mm)")
    ap.add_argument("--out", default="stereo_calib.npz")
    args = ap.parse_args()

    pattern = (args.cols, args.rows)
    objp = np.zeros((args.cols * args.rows, 3), np.float32)
    objp[:, :2] = np.mgrid[0:args.cols, 0:args.rows].T.reshape(-1, 2) * args.square

    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-4)
    obj, ptl, ptr = [], [], []
    size = None
    lefts = sorted(glob.glob(os.path.join(args.imgs, "left_*.png")))
    for lp in lefts:
        rp = lp.replace("left_", "right_")
        if not os.path.exists(rp):
            continue
        gl = cv2.imread(lp, cv2.IMREAD_GRAYSCALE)
        gr = cv2.imread(rp, cv2.IMREAD_GRAYSCALE)
        size = gl.shape[::-1]
        okl, cl = cv2.findChessboardCorners(gl, pattern)
        okr, cr = cv2.findChessboardCorners(gr, pattern)
        if not (okl and okr):
            print(f"skip {os.path.basename(lp)}")
            continue
        cl = cv2.cornerSubPix(gl, cl, (11, 11), (-1, -1), crit)
        cr = cv2.cornerSubPix(gr, cr, (11, 11), (-1, -1), crit)
        obj.append(objp); ptl.append(cl); ptr.append(cr)

    print(f"사용한 쌍: {len(obj)} / {len(lefts)}")
    if len(obj) < 10:
        raise SystemExit("10쌍 이상 필요")

    # 1) 카메라별 내부 파라미터
    e1, K1, D1, _, _ = cv2.calibrateCamera(obj, ptl, size, None, None)
    e2, K2, D2, _, _ = cv2.calibrateCamera(obj, ptr, size, None, None)
    print(f"RMS left {e1:.3f}px / right {e2:.3f}px")

    # 2) 두 카메라 사이 관계 (R, T)
    es, K1, D1, K2, D2, R, T, _, _ = cv2.stereoCalibrate(
        obj, ptl, ptr, K1, D1, K2, D2, size,
        criteria=crit, flags=cv2.CALIB_FIX_INTRINSIC)
    print(f"stereo RMS {es:.3f}px")

    # 3) 평행화(rectify): 같은 점이 양쪽 이미지에서 같은 y에 오도록
    R1, R2, P1, P2, Q, roi1, roi2 = cv2.stereoRectify(K1, D1, K2, D2, size, R, T, alpha=0)

    baseline_mm = -P2[0, 3] / P2[0, 0]
    print(f"baseline {baseline_mm:.1f} mm  |T| {np.linalg.norm(T):.1f} mm  focal {P1[0, 0]:.1f}px")
    if baseline_mm < 0:
        print("⚠ baseline이 음수 → 좌우가 바뀐 것. capture/detect 때 --swap 쓰고 다시 찍기")

    np.savez(args.out, K1=K1, D1=D1, K2=K2, D2=D2, R=R, T=T,
             R1=R1, R2=R2, P1=P1, P2=P2, Q=Q, size=np.array(size))
    print(f"saved {args.out}")

    # 확인용 이미지
    gl = cv2.imread(lefts[0]); gr = cv2.imread(lefts[0].replace("left_", "right_"))
    m1 = cv2.initUndistortRectifyMap(K1, D1, R1, P1, size, cv2.CV_32FC1)
    m2 = cv2.initUndistortRectifyMap(K2, D2, R2, P2, size, cv2.CV_32FC1)
    vis = np.hstack([cv2.remap(gl, *m1, cv2.INTER_LINEAR), cv2.remap(gr, *m2, cv2.INTER_LINEAR)])
    for y in range(0, vis.shape[0], 40):
        cv2.line(vis, (0, y), (vis.shape[1], y), (0, 255, 0), 1)
    cv2.imwrite("rectify_check.jpg", vis)
    print("saved rectify_check.jpg")


if __name__ == "__main__":
    main()
