#!/usr/bin/env python3
"""
드론 탐지 + 경고 (라즈베리파이 5 + AI HAT / 맥·PC 테스트 겸용)

  # Pi + Hailo
  python3 detect_pi.py --backend hailo --hef drone.hef --source picam --show
  # 맥/PC 웹캠 테스트 (Hailo 없이)
  python3 detect_pi.py --backend ultralytics --weights best.pt --source 0 --show

경고 레벨: 0 CLEAR / 1 DETECTED(추적 확정) / 2 WARNING(접근 중 or 가까움)
"""
import argparse
import json
import socket
import time
from collections import defaultdict, deque

import cv2
import numpy as np
import supervision as sv
from trackers import ByteTrackTracker

from lcd_ui import make_lcd
from video_link import make_video_link

LEVEL_NAME = {0: "CLEAR", 1: "DETECTED", 2: "WARNING"}
LEVEL_COLOR = {0: (0, 200, 0), 1: (0, 200, 255), 2: (0, 0, 255)}  # BGR


# ---------------------------------------------------------------- 전처리
def letterbox(img, new_w, new_h, pad=114):
    """비율 유지 리사이즈 + 패딩 (YOLO 학습 때랑 같은 방식)"""
    h, w = img.shape[:2]
    r = min(new_w / w, new_h / h)
    nw, nh = int(round(w * r)), int(round(h * r))
    out = np.full((new_h, new_w, 3), pad, np.uint8)
    dx, dy = (new_w - nw) // 2, (new_h - nh) // 2
    out[dy:dy + nh, dx:dx + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    return out, r, dx, dy


# ---------------------------------------------------------------- 검출기
class HailoDetector:
    """HEF는 Hailo Model Zoo로 NMS 포함 컴파일했다고 가정
    (출력: 클래스별 [ymin, xmin, ymax, xmax, score], 0~1 정규화)"""

    def __init__(self, hef_path, conf):
        from hailo_platform import (HEF, VDevice, HailoStreamInterface, InferVStreams,
                                    ConfigureParams, InputVStreamParams, OutputVStreamParams, FormatType)
        self.conf = conf
        self.hef = HEF(hef_path)
        self.device = VDevice()
        cfg = ConfigureParams.create_from_hef(self.hef, interface=HailoStreamInterface.PCIe)
        self.ng = self.device.configure(self.hef, cfg)[0]
        in_info = self.hef.get_input_vstream_infos()[0]
        self.in_name = in_info.name
        self.in_h, self.in_w = in_info.shape[0], in_info.shape[1]
        self.out_name = self.hef.get_output_vstream_infos()[0].name
        in_p = InputVStreamParams.make(self.ng, format_type=FormatType.UINT8)
        out_p = OutputVStreamParams.make(self.ng, format_type=FormatType.FLOAT32)
        self.pipe = InferVStreams(self.ng, in_p, out_p)
        self.pipe.__enter__()
        self.act = self.ng.activate(self.ng.create_params())
        self.act.__enter__()

    def detect(self, frame_bgr):
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        img, r, dx, dy = letterbox(rgb, self.in_w, self.in_h)
        out = self.pipe.infer({self.in_name: img[None]})[self.out_name][0]  # 배치 0
        boxes, scores, classes = [], [], []
        for c, dets in enumerate(out):
            for y1, x1, y2, x2, s in np.asarray(dets).reshape(-1, 5):
                if s < self.conf:
                    continue
                # 정규화 → 입력(640) 픽셀 → 원본 프레임 픽셀
                bx = np.array([x1 * self.in_w - dx, y1 * self.in_h - dy,
                               x2 * self.in_w - dx, y2 * self.in_h - dy]) / r
                boxes.append(bx); scores.append(s); classes.append(c)
        return np.array(boxes).reshape(-1, 4), np.array(scores), np.array(classes, dtype=int)

    def close(self):
        self.act.__exit__(None, None, None)
        self.pipe.__exit__(None, None, None)
        self.device.release()


class UltralyticsDetector:
    """Hailo 없이 CPU/MPS로 테스트할 때"""

    def __init__(self, weights, imgsz, conf):
        from ultralytics import YOLO
        self.model, self.imgsz, self.conf = YOLO(weights), imgsz, conf

    def detect(self, frame_bgr):
        b = self.model.predict(frame_bgr, imgsz=self.imgsz, conf=self.conf, verbose=False)[0].boxes
        return b.xyxy.cpu().numpy(), b.conf.cpu().numpy(), b.cls.cpu().numpy().astype(int)

    def close(self):
        pass


# ---------------------------------------------------------------- 카메라
class Camera:
    def __init__(self, source, size):
        if source == "picam":
            from picamera2 import Picamera2
            self.cam = Picamera2()
            # picamera2의 RGB888은 메모리상 BGR 순서라 OpenCV에 바로 씀
            self.cam.configure(self.cam.create_video_configuration(main={"size": size, "format": "RGB888"}))
            self.cam.start()
            self.kind = "picam"
        else:
            self.cap = cv2.VideoCapture(int(source) if source.isdigit() else source)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
            self.kind = "cv"

    def read(self):
        if self.kind == "picam":
            return self.cam.capture_array()
        ok, f = self.cap.read()
        return f if ok else None

    def close(self):
        if self.kind == "picam":
            self.cam.stop()
        else:
            self.cap.release()


# ---------------------------------------------------------------- 판정
class ThreatJudge:
    """
    - min_hits 프레임 이상 추적돼야 '확정' (새/노이즈 1~2프레임 오탐 거르기)
    - grow_window초 전보다 bbox 면적이 grow_ratio배 이상 → 접근 중
    - 화면 대비 면적 near_area 이상 → 가까움
    """

    def __init__(self, min_hits=5, grow_window=1.0, grow_ratio=1.3, near_area=0.02, forget=2.0,
                 hold_s=1.0, warn_hold_s=1.5):
        self.min_hits, self.grow_window, self.grow_ratio = min_hits, grow_window, grow_ratio
        self.near_area, self.forget = near_area, forget
        # 검출이 한두 프레임 끊겨도 경고가 깜빡이지 않도록 마지막 상태를 잠깐 유지
        self.hold_s, self.warn_hold_s = hold_s, warn_hold_s
        self.state = {}       # tid -> (마지막 확인 시각, track dict)
        self.warn_until = {}
        self.hist = defaultdict(deque)  # track_id -> deque[(t, 면적비)]
        self.last_seen = {}

    def update(self, dets, frame_hw, now):
        H, W = frame_hw
        level, tracks = 0, []
        if dets.tracker_id is not None:
            for xyxy, conf, tid in zip(dets.xyxy, dets.confidence, dets.tracker_id):
                if tid < 0:  # 트래커가 아직 확정 안 한 검출
                    continue
                x1, y1, x2, y2 = xyxy
                area = max(0.0, (x2 - x1) * (y2 - y1)) / (W * H)
                h = self.hist[tid]
                h.append((now, area))
                self.last_seen[tid] = now
                while h and now - h[0][0] > 3.0:
                    h.popleft()
                if len(h) < self.min_hits:
                    continue
                cur = float(np.median([a for _, a in list(h)[-3:]]))  # 3프레임 중앙값으로 흔들림 완화
                past = [a for t, a in h if now - t >= self.grow_window]
                approaching = bool(past) and bool(cur / max(past[-1], 1e-9) >= self.grow_ratio)
                if approaching or cur >= self.near_area:
                    self.warn_until[tid] = now + self.warn_hold_s
                lvl = 2 if now < self.warn_until.get(tid, 0) else 1
                self.state[tid] = (now, dict(id=int(tid) + 1, conf=round(float(conf), 3),   # 보이는 번호는 1부터 (detect_stereo와 같게)
                                             box=[round(float(v), 1) for v in xyxy],
                                             area=round(cur, 5), approaching=approaching, level=lvl))
        # 이번 프레임에 안 잡혔어도 hold_s 이내에 확인된 표적은 유지
        for tid, (t, tr) in list(self.state.items()):
            if now - t > self.hold_s:
                self.state.pop(tid)
                continue
            if now - t > 0 and tr["level"] == 2 and now >= self.warn_until.get(tid, 0):
                tr["level"] = 1
            level = max(level, tr["level"])
            tracks.append(tr)
        for tid in [k for k, t in self.last_seen.items() if now - t > self.forget]:
            self.hist.pop(tid, None)
            self.last_seen.pop(tid)
            self.warn_until.pop(tid, None)
        return level, tracks


# ---------------------------------------------------------------- 경고 출력
class Alerter:
    def __init__(self, udp=None, buzzer_pin=None, rate_hz=5):
        self.sock = self.addr = None
        if udp:
            host, port = udp.split(":")
            self.addr = (host, int(port))
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.buzzer = None
        if buzzer_pin is not None:
            from gpiozero import Buzzer
            self.buzzer = Buzzer(buzzer_pin)
        self.level, self.last_send, self.period = 0, 0.0, 1.0 / rate_hz

    def update(self, level, tracks, now):
        changed = level != self.level
        if changed:
            print(f"[{time.strftime('%H:%M:%S')}] {LEVEL_NAME[self.level]} -> {LEVEL_NAME[level]} "
                  f"ids={[t['id'] for t in tracks]}")
            if self.buzzer:
                self.buzzer.off()
                if level == 2:
                    self.buzzer.beep(on_time=0.1, off_time=0.1)
                elif level == 1:
                    self.buzzer.beep(on_time=0.1, off_time=0.9)
            self.level = level
        if self.sock and (changed or (level > 0 and now - self.last_send >= self.period)):
            msg = {"t": now, "level": level, "tracks": tracks}
            self.sock.sendto(json.dumps(msg).encode(), self.addr)
            self.last_send = now

    def close(self):
        if self.buzzer:
            self.buzzer.off()


# 부저 핀이 다른 장치 핀과 겹치면 출력끼리 부딪혀 핀이 상할 수 있음 (BCM 번호, E22 배선은 루트 README 3-1)
E22_PINS = {14: "E22 RXD (UART TX)", 15: "E22 TXD (UART RX)", 17: "E22 AUX", 22: "E22 M0", 27: "E22 M1"}
LCD_PINS = {2: "LCD SDA (I2C)", 3: "LCD SCL (I2C)"}
HAT_PINS = {0: "HAT ID EEPROM", 1: "HAT ID EEPROM"}


def check_buzzer_pin(ap, pin, lcd=False, lora=False):
    """실행 전에 막기: --lcd면 I2C 핀, --lora면 E22 핀과 겹치면 에러. E22 핀은 --lora 없어도 경고"""
    if pin is None:
        return
    used = dict(HAT_PINS)
    if lcd:
        used.update(LCD_PINS)
    if lora:
        used.update(E22_PINS)
    if pin in used:
        ap.error(f"--buzzer-pin {pin}은 {used[pin]} 핀이라 못 씀 → 다른 핀으로 (예: --buzzer-pin 18)")
    if pin in E22_PINS:
        print(f"[주의] GPIO{pin}은 {E22_PINS[pin]} 자리. E22가 꽂혀 있으면 부저 출력과 부딪힘 → --buzzer-pin 18 권장")


def draw(frame, tracks, level, fps):
    for t in tracks:
        x1, y1, x2, y2 = map(int, t["box"])
        c = LEVEL_COLOR[t["level"]]
        cv2.rectangle(frame, (x1, y1), (x2, y2), c, 2)
        tag = f"#{t['id']} {t['conf']:.2f}" + (" APPROACH" if t["approaching"] else "")
        cv2.putText(frame, tag, (x1, max(15, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, c, 1)
    cv2.putText(frame, f"{LEVEL_NAME[level]}  {fps:.1f} fps", (10, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, LEVEL_COLOR[level], 2)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["hailo", "ultralytics"], default="hailo")
    ap.add_argument("--hef", default="drone.hef")
    ap.add_argument("--weights", default="best.pt")
    ap.add_argument("--source", default="picam", help="picam / 0(웹캠) / 영상 경로")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--fps", type=int, default=30, help="트래커에 알려줄 대략적 프레임레이트")
    ap.add_argument("--min-hits", type=int, default=5)
    ap.add_argument("--near-area", type=float, default=0.02)
    ap.add_argument("--udp", default=None, help="경고 JSON 보낼 곳 host:port")
    ap.add_argument("--buzzer-pin", type=int, default=None, help="GPIO 번호 (예: 18, 17은 E22 AUX라 안 됨)")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--lcd", nargs="?", const="0x27", default=None, metavar="ADDR",
                    help="1602 I2C LCD 상태 화면 (주소 기본 0x27, fake = 터미널 미리보기)")
    ap.add_argument("--lcd-no-blink", action="store_true", help="WARNING 때 LCD 백라이트 안 깜빡임")
    ap.add_argument("--video-link", nargs="?", const=8090, type=int, default=None, metavar="PORT",
                    help="Wi-Fi 영상 송출 (지휘소 gui.py가 받음, 기본 포트 8090)")
    args = ap.parse_args()
    check_buzzer_pin(ap, args.buzzer_pin, lcd=bool(args.lcd) and str(args.lcd).lower() != "fake")

    lcd = make_lcd(args.lcd, blink=not args.lcd_no_blink)
    lcd.message("SP_DEWS mono", "model loading")
    det = (HailoDetector(args.hef, args.conf) if args.backend == "hailo"
           else UltralyticsDetector(args.weights, args.imgsz, args.conf))
    lcd.message("SP_DEWS mono", "camera start")
    cam = Camera(args.source, (args.width, args.height))
    # 작은 드론은 신뢰도가 낮게 나오는 경우가 많아서 기본값(0.7)보다 낮춤
    tracker = ByteTrackTracker(frame_rate=args.fps, track_activation_threshold=0.35,
                               high_conf_det_threshold=0.4, lost_track_buffer=args.fps)
    judge = ThreatJudge(min_hits=args.min_hits, near_area=args.near_area)
    alerter = Alerter(args.udp, args.buzzer_pin)
    vlink = make_video_link(args.video_link)

    fps, t_prev = 0.0, time.time()
    try:
        while True:
            frame = cam.read()
            if frame is None:
                break
            boxes, scores, classes = det.detect(frame)
            if len(boxes):
                dets = sv.Detections(xyxy=boxes.astype(np.float32),
                                     confidence=scores.astype(np.float32),
                                     class_id=classes.astype(int))
            else:
                dets = sv.Detections.empty()
            dets = tracker.update(dets)

            now = time.time()
            level, tracks = judge.update(dets, frame.shape[:2], now)
            alerter.update(level, tracks, now)

            fps = 0.9 * fps + 0.1 / max(now - t_prev, 1e-6)
            t_prev = now
            lcd.update(level, tracks, fps)   # 저장만 하고 바로 리턴 (LCD 쓰기는 별도 스레드)
            vlink.push(frame.copy() if args.show else frame, level, tracks)   # --show는 frame에 바로 그려서 복사본
            if args.show:
                draw(frame, tracks, level, fps)
                cv2.imshow("drone", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        lcd.close()                      # LCD에 stopped / ERR 남김
        vlink.close()                    # 녹화 중이던 경고 클립은 저장하고 끝냄
        alerter.close()
        cam.close()
        det.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
