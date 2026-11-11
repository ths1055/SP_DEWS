#!/usr/bin/env python3
"""
스테레오 드론 탐지 + 거리 기반 경고 (Pi 5 + AI HAT + Camera Module 3 x2)

  # Pi 실시간
  python3 detect_stereo.py --backend hailo --hef drone.hef --calib stereo_calib.npz --show
  # 녹화본으로 맥/PC 테스트
  python3 detect_stereo.py --backend ultralytics --weights best.pt --calib stereo_calib.npz \
      --video left.mp4 right.mp4 --show

흐름: 좌/우 동시 캡처 → 양쪽 YOLO 검출 → 중심점만 평행화 → 같은 높이(y) 짝짓기
      → 시차(disparity)로 거리 Z = f·B/d → 짝 못 찾으면 드론 크기로 단안 추정
      → 왼쪽 기준 추적 → 거리/접근속도로 경고

경고 레벨
  1 DETECTED : 추적 확정
  2 WARNING  : warn_m 이내, 또는 watch_m 이내에서 approach_mps 이상으로 접근, 또는 도달예상 tta_s 이하
"""
import argparse
import time
from collections import defaultdict, deque

import cv2
import numpy as np
import supervision as sv
from trackers import ByteTrackTracker

from detect_pi import HailoDetector, UltralyticsDetector, Alerter, LEVEL_NAME, LEVEL_COLOR


# ---------------------------------------------------------------- 스테레오 기하
class StereoGeometry:
    def __init__(self, path):
        c = np.load(path)
        self.K1, self.D1, self.R1, self.P1 = c["K1"], c["D1"], c["R1"], c["P1"]
        self.K2, self.D2, self.R2, self.P2 = c["K2"], c["D2"], c["R2"], c["P2"]
        self.size = tuple(int(v) for v in c["size"])
        self.f = float(self.P1[0, 0])
        self.cx, self.cy = float(self.P1[0, 2]), float(self.P1[1, 2])
        self.B = float(-self.P2[0, 3] / self.P2[0, 0]) / 1000.0  # mm → m
        print(f"calib: f={self.f:.1f}px  B={self.B * 100:.1f}cm  size={self.size}")

    def rectify(self, pts, side):
        """원본 픽셀 좌표 → 평행화된 좌표 (이미지 전체 remap 안 하고 점만 변환해서 빠름)"""
        if len(pts) == 0:
            return np.zeros((0, 2), np.float32)
        K, D, R, P = ((self.K1, self.D1, self.R1, self.P1) if side == "L"
                      else (self.K2, self.D2, self.R2, self.P2))
        p = cv2.undistortPoints(np.asarray(pts, np.float32).reshape(-1, 1, 2), K, D, R=R, P=P)
        return p.reshape(-1, 2)

    def depth(self, disparity):
        return self.f * self.B / disparity

    def angles(self, x, y):
        """평행화 좌표 → 방위각/고각(도). 오른쪽 +, 위쪽 +"""
        az = np.degrees(np.arctan2(x - self.cx, self.f))
        el = np.degrees(np.arctan2(self.cy - y, self.f))
        return az, el


def centers_and_sizes(boxes):
    c = np.stack([(boxes[:, 0] + boxes[:, 2]) / 2, (boxes[:, 1] + boxes[:, 3]) / 2], 1)
    s = np.maximum(boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1])
    return c, s


def stereo_match(geo, bl, br, y_tol, max_range_m, min_range_m):
    """
    왼쪽 박스마다 오른쪽 짝 찾기. 평행화되면 같은 물체는 y가 같고 x는 왼쪽이 더 큼(시차>0)
    returns: 왼쪽 박스별 (거리 m 또는 nan), 평행화된 왼쪽 중심
    """
    nL = len(bl)
    dist = np.full(nL, np.nan)
    if nL == 0:
        return dist, np.zeros((0, 2))
    cl, sl = centers_and_sizes(bl)
    rl = geo.rectify(cl, "L")
    if len(br) == 0:
        return dist, rl
    cr, sr = centers_and_sizes(br)
    rr = geo.rectify(cr, "R")

    d_min = geo.f * geo.B / max_range_m  # 너무 먼 것(시차 너무 작음) 제외
    d_max = geo.f * geo.B / min_range_m
    cand = []
    for i in range(nL):
        for j in range(len(br)):
            dy = abs(rl[i, 1] - rr[j, 1])
            d = rl[i, 0] - rr[j, 0]
            ratio = sl[i] / max(sr[j], 1e-6)
            if dy > y_tol + 0.1 * sl[i] or not (d_min <= d <= d_max) or not (0.6 < ratio < 1.66):
                continue
            cand.append((dy + 5 * abs(np.log(ratio)), i, j, d))
    used_l, used_r = set(), set()
    for _, i, j, d in sorted(cand):  # 비용 낮은 순서로 1:1 매칭
        if i in used_l or j in used_r:
            continue
        used_l.add(i); used_r.add(j)
        dist[i] = geo.depth(d)
    return dist, rl


# ---------------------------------------------------------------- 판정
class RangeJudge:
    def __init__(self, min_hits=5, warn_m=20.0, watch_m=40.0, approach_mps=2.0, tta_s=5.0,
                 hold_s=1.5, forget=2.0):
        self.min_hits, self.warn_m, self.watch_m = min_hits, warn_m, watch_m
        self.approach_mps, self.tta_s, self.forget = approach_mps, tta_s, forget
        self.hold_s = hold_s                # WARNING 한 번 뜨면 최소 유지 시간 (깜빡임 방지)
        self.warn_until = {}
        self.keep_s = 1.0                   # 검출이 잠깐 끊겨도 표적을 유지하는 시간
        self.state = {}
        self.hist = defaultdict(deque)  # tid -> (t, 거리, stereo여부)
        self.n = defaultdict(int)
        self.last_seen = {}

    def update(self, dets, now):
        level, out = 0, []
        n = 0 if dets.tracker_id is None else len(dets)
        for k in range(n):
            tid = int(dets.tracker_id[k])
            if tid < 0:
                continue
            z, src = float(dets.data["dist"][k]), str(dets.data["src"][k])
            self.n[tid] += 1
            self.last_seen[tid] = now
            h = self.hist[tid]
            if np.isfinite(z):
                h.append((now, z, src == "stereo"))
            while h and now - h[0][0] > 3.0:
                h.popleft()
            if self.n[tid] < self.min_hits:
                continue

            dist = closing = tta = None
            if h:
                recent = [d for _, d, _ in list(h)[-7:]]
                dist = float(np.median(recent))
                win = [(t, d) for t, d, _ in h if now - t <= 1.5]
                if len(win) >= 8 and win[-1][0] - win[0][0] > 1.0:
                    ts, ds = np.array(win).T
                    slope = np.polyfit(ts - ts[0], ds, 1)[0]  # m/s, 음수면 다가오는 중
                    closing = float(-slope)
                    if closing > 0.3:
                        tta = dist / closing

            lvl = 1
            if dist is not None:
                if (dist <= self.warn_m
                        or (closing is not None and closing >= self.approach_mps and dist <= self.watch_m)
                        or (tta is not None and tta <= self.tta_s)):
                    self.warn_until[tid] = now + self.hold_s
            if now < self.warn_until.get(tid, 0):
                lvl = 2
            self.state[tid] = (now, dict(
                id=tid, conf=round(float(dets.confidence[k]), 3),
                box=[round(float(v), 1) for v in dets.xyxy[k]],
                dist_m=None if dist is None else round(dist, 1),
                src=src,
                closing_mps=None if closing is None else round(closing, 1),
                tta_s=None if tta is None else round(tta, 1),
                az_deg=round(float(dets.data["az"][k]), 1),
                el_deg=round(float(dets.data["el"][k]), 1),
                level=lvl))
        for tid, (t, tr) in list(self.state.items()):
            if now - t > self.keep_s:
                self.state.pop(tid)
                continue
            if tr["level"] == 2 and now >= self.warn_until.get(tid, 0):
                tr["level"] = 1
            level = max(level, tr["level"])
            out.append(tr)
        self._gc(now)
        return level, out

    def _gc(self, now):
        for tid in [k for k, t in self.last_seen.items() if now - t > self.forget]:
            for d in (self.hist, self.n, self.last_seen, self.warn_until):
                d.pop(tid, None)


def draw(frame, tracks, level, fps, skew, n_stereo):
    for t in tracks:
        x1, y1, x2, y2 = map(int, t["box"])
        c = LEVEL_COLOR[t["level"]]
        cv2.rectangle(frame, (x1, y1), (x2, y2), c, 2)
        d = "--" if t["dist_m"] is None else f"{t['dist_m']:.1f}m"
        tag = f"#{t['id']} {d}{'' if t['src'] == 'stereo' else '*'}"
        if t["closing_mps"] is not None and t["closing_mps"] > 0.3:
            tag += f" {t['closing_mps']:+.1f}m/s"
        cv2.putText(frame, tag, (x1, max(15, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, c, 2)
    cv2.putText(frame, f"{LEVEL_NAME[level]}  {fps:.1f}fps  skew {skew:.1f}ms  stereo {n_stereo}",
                (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, LEVEL_COLOR[level], 2)
    cv2.putText(frame, "* = mono estimate", (10, frame.shape[0] - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["hailo", "ultralytics"], default="hailo")
    ap.add_argument("--hef", default="drone.hef")
    ap.add_argument("--weights", default="best.pt")
    ap.add_argument("--calib", default="stereo_calib.npz")
    ap.add_argument("--video", nargs=2, metavar=("LEFT", "RIGHT"), help="녹화본 테스트")
    ap.add_argument("--swap", action="store_true", help="CAM0/CAM1 좌우 반대로 꽂았을 때")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--drone-size", type=float, default=0.35, help="단안 추정용 드론 대각 크기(m)")
    ap.add_argument("--max-skew", type=float, default=10.0, help="좌우 시간차가 이 이상(ms)이면 스테레오 안 씀")
    ap.add_argument("--y-tol", type=float, default=6.0, help="좌우 짝 찾을 때 허용 y 오차(px)")
    ap.add_argument("--max-range", type=float, default=150.0)
    ap.add_argument("--warn-m", type=float, default=20.0)
    ap.add_argument("--watch-m", type=float, default=40.0)
    ap.add_argument("--approach-mps", type=float, default=2.0)
    ap.add_argument("--min-hits", type=int, default=5)
    ap.add_argument("--udp", default=None)
    ap.add_argument("--buzzer-pin", type=int, default=None)
    ap.add_argument("--record", default=None, help="좌/우 원본 영상 저장 폴더")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    geo = StereoGeometry(args.calib)
    if args.video:
        from stereo_cam import StereoVideo
        cam = StereoVideo(*args.video)
    else:
        from stereo_cam import StereoCamera
        cam = StereoCamera(geo.size, fps=args.fps, swap=args.swap)

    det = (HailoDetector(args.hef, args.conf) if args.backend == "hailo"
           else UltralyticsDetector(args.weights, args.imgsz, args.conf))
    tracker = ByteTrackTracker(frame_rate=args.fps, track_activation_threshold=0.35,
                               high_conf_det_threshold=0.4, lost_track_buffer=args.fps)
    judge = RangeJudge(min_hits=args.min_hits, warn_m=args.warn_m, watch_m=args.watch_m,
                       approach_mps=args.approach_mps)
    alerter = Alerter(args.udp, args.buzzer_pin)

    writers = None
    if args.record:
        import os
        os.makedirs(args.record, exist_ok=True)
        four = cv2.VideoWriter_fourcc(*"mp4v")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        writers = [cv2.VideoWriter(f"{args.record}/{stamp}_{s}.mp4", four, args.fps, geo.size)
                   for s in ("left", "right")]

    fps, t_prev = 0.0, time.time()
    try:
        while True:
            l, r, skew, t_cap = cam.read()
            if l is None:
                break
            if writers:
                writers[0].write(l); writers[1].write(r)

            bl, sl, kl = det.detect(l)
            if len(bl) and skew <= args.max_skew:
                br, _, _ = det.detect(r)
            else:
                br = np.zeros((0, 4))

            dist, rect_c = stereo_match(geo, bl, br, args.y_tol, args.max_range, 1.0)
            src = np.where(np.isfinite(dist), "stereo", "mono")
            if len(bl):
                _, size_px = centers_and_sizes(bl)
                mono = geo.f * args.drone_size / np.maximum(size_px, 1.0)
                dist = np.where(np.isfinite(dist), dist, mono)
                az, el = geo.angles(rect_c[:, 0], rect_c[:, 1])
                dets = sv.Detections(xyxy=bl.astype(np.float32), confidence=sl.astype(np.float32),
                                     class_id=kl.astype(int),
                                     data={"dist": dist, "src": src, "az": az, "el": el})
            else:
                dets = sv.Detections.empty()
            dets = tracker.update(dets)

            now = time.time() if not args.video else t_cap
            level, tracks = judge.update(dets, now)
            alerter.update(level, tracks, now)

            t = time.time()
            fps = 0.9 * fps + 0.1 / max(t - t_prev, 1e-6)
            t_prev = t
            if args.show:
                vis = l.copy()
                draw(vis, tracks, level, fps, skew, int((src == "stereo").sum()) if len(bl) else 0)
                cv2.imshow("stereo drone", vis)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        if writers:
            for w in writers:
                w.release()
        alerter.close()
        cam.close()
        det.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
