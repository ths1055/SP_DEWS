#!/usr/bin/env python3
"""
video_link.py — 관측소 영상 Wi-Fi 송출 (라이브 + 경고 클립) → 지휘소 HUD (modules/gui.py)

LoRa는 경보 17 B만 보내고, 영상은 Pi 5 내장 Wi-Fi로 따로 보낸다.
LoRa 2.4 kbps면 작은 JPEG 한 장(약 2 KB)에도 7초 넘게 걸리고 그동안 경보가 막혀서 영상은 불가.
Wi-Fi는 20 m 정도면 충분 (640px 10 fps MJPEG ≈ 2~3 Mbps).

  python3 detect_stereo.py ... --video-link     탐지하면서 송출 (기본 포트 8090, detect_pi.py도 같음)
  python3 video_link.py --demo                  카메라 없이 가짜 영상 + 가짜 경고 (노트북/맥에서 HUD 확인용)

Pi를 Wi-Fi 핫스팟으로 만들면 노트북이 여기에 바로 붙음 (공유기 필요 없음, Pi 주소 10.42.0.1)
  sudo nmcli device wifi hotspot ifname wlan0 ssid SP_DEWS password <8자 이상>

주소
  /live.mjpg              라이브 (가로 640, 10 fps, 표적 박스 표시. 브라우저로 열어도 보임)
  /snapshot.jpg           최신 한 장
  /state                  탐지 등급·표적·녹화 중 여부 (HUD가 1초마다 확인)
  /clips                  경고 클립 목록 (최신순)
  /clips/<id>.mjpg        클립 재생 (녹화된 속도 그대로)
  /clips/<id>/thumb.jpg   대표 장면 (제일 가까웠을 때)

경고 클립: 탐지 등급이 WARNING(2)이 되면 그 전 3초 + 경고가 끝난 뒤 4초를 JPEG로 저장 (최대 15초, 최근 30개)
메인 루프에선 push()만 부름 (참조만 저장하고 바로 리턴). 줄이기·JPEG 변환·전송·저장은 전부 별도 스레드.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
CLIP_ID = re.compile(r"^\d{8}_\d{6}(_\d+)?$")
COLOR = {0: (247, 216, 98), 1: (255, 243, 196), 2: (87, 64, 255)}   # BGR, 지휘소 HUD 색이랑 맞춤
FONT = cv2.FONT_HERSHEY_SIMPLEX


def annotate(img, level, tracks, scale):
    """표적에 모서리 괄호 + 번호·거리 (박스를 다 그리면 작은 드론이 가려져서 모서리만)"""
    for tr in tracks:
        box = tr.get("box")
        if not box:
            continue
        x1, y1, x2, y2 = (int(round(v * scale)) for v in box)
        c = COLOR.get(tr.get("level", 1), COLOR[1])
        L = max(5, min(x2 - x1, y2 - y1) // 4)
        for (px, py, dx, dy) in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x1, y2, 1, -1), (x2, y2, -1, -1)):
            cv2.line(img, (px, py), (px + dx * L, py), c, 1, cv2.LINE_AA)
            cv2.line(img, (px, py), (px, py + dy * L), c, 1, cv2.LINE_AA)
        d = tr.get("dist_m")
        label = f"#{tr.get('id')}" + ("" if d is None else f" {d:.1f}m") + ("*" if tr.get("src") == "mono" else "")
        cv2.putText(img, label, (x1, max(12, y1 - 5)), FONT, 0.42, c, 1, cv2.LINE_AA)
    cv2.putText(img, time.strftime("%H:%M:%S"), (8, img.shape[0] - 8), FONT, 0.42, (210, 210, 210), 1,
                cv2.LINE_AA)


class VideoLink:
    def __init__(self, port: int = 8090, fps: float = 10.0, width: int = 640, quality: int = 70,
                 pre_s: float = 3.0, post_s: float = 4.0, max_s: float = 15.0, keep: int = 30,
                 clip_dir=None, host: str = "0.0.0.0"):
        self.fps, self.width, self.quality = fps, width, quality
        self.post_s, self.max_s, self.keep = post_s, max_s, keep
        self.clip_dir = Path(clip_dir) if clip_dir else HERE / "clips"
        self.clip_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._latest, self._new = None, False          # 메인 루프가 넣은 최신 (frame, level, tracks, t)
        self._cond = threading.Condition()
        self._jpg, self._jseq = None, 0                 # 최신 JPEG (라이브용)
        self._ring: deque = deque(maxlen=max(1, int(pre_s * fps)))   # 경고 전 장면
        self._rec = None
        self._clients = 0
        self._state = {"level": 0, "tracks": [], "fps_out": 0.0, "recording": False,
                       "clips_rev": int(time.time())}
        self._stop = threading.Event()
        self._srv = ThreadingHTTPServer((host, port), _make_handler(self))
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        self._enc = threading.Thread(target=self._run, name="video-enc", daemon=True)
        threading.Thread(target=self._srv.serve_forever, name="video-http", daemon=True).start()
        self._enc.start()

    # ---- 메인 루프에서 부르는 것
    def push(self, frame, level, tracks) -> None:
        """매 프레임 호출. 참조만 저장 (frame은 넘긴 뒤에 고치지 말 것)"""
        with self._lock:
            self._latest, self._new = (frame, int(level), tracks, time.time()), True

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._enc.join(timeout=5)                       # 녹화 중이던 클립은 저장하고 끝냄
        self._srv.shutdown()
        self._srv.server_close()

    # ---- HTTP 쪽에서 쓰는 것
    def state(self) -> dict:
        return {**self._state, "clients": self._clients, "t": time.time()}

    def wait_jpg(self, seq: int, timeout: float):
        with self._cond:
            self._cond.wait_for(lambda: self._jseq != seq or self._stop.is_set(), timeout)
            return (self._jpg, self._jseq) if self._jseq != seq else (None, seq)

    def list_clips(self) -> list:
        out = []
        for d in sorted(self.clip_dir.iterdir(), reverse=True):
            if not (d.is_dir() and CLIP_ID.match(d.name)):
                continue
            try:
                meta = json.loads((d / "meta.json").read_text())
            except (OSError, ValueError):               # 저장 중이거나 깨진 클립
                continue
            meta.pop("times", None)
            out.append(meta)
        return out

    # ---- 인코딩 스레드
    def _run(self) -> None:
        period, n, t_fps = 1.0 / self.fps, 0, time.monotonic()
        nxt = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                item, new, self._new = self._latest, self._new, False
            if item is not None and new:
                try:
                    self._encode(*item)
                    n += 1
                except Exception as e:                  # 한 장 실패로 송출이 죽지 않게
                    print(f"[video] 인코딩 오류: {e}")
            now = time.monotonic()
            if now - t_fps >= 2.0:
                self._state["fps_out"] = round(n / (now - t_fps), 1)
                n, t_fps = 0, now
            nxt = max(nxt + period, now)
            self._stop.wait(nxt - time.monotonic())
        self._finish(wait=True)

    def _encode(self, frame, level, tracks, t) -> None:
        h, w = frame.shape[:2]
        s = min(1.0, self.width / w)
        small = cv2.resize(frame, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA) if s < 1 \
            else frame.copy()
        tracks = [dict(tr) for tr in tracks]
        annotate(small, level, tracks, s)
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if not ok:
            return
        jpg = buf.tobytes()
        with self._cond:
            self._jpg, self._jseq = jpg, self._jseq + 1
            self._cond.notify_all()
        dists = [tr["dist_m"] for tr in tracks if tr.get("dist_m") is not None]
        self._state.update(level=level, tracks=[{k: tr.get(k) for k in ("id", "level", "dist_m", "az_deg", "src")}
                                                for tr in tracks])
        fr = (t, jpg, level, min(dists) if dists else None)
        self._ring.append(fr)
        self._record(fr)

    def _record(self, fr) -> None:
        t, _, level, _ = fr
        rec = self._rec
        if rec is None:
            if level >= 2:                              # 경고 시작 → 직전 장면부터 녹화
                frames = list(self._ring)
                self._rec = {"frames": frames, "t0": frames[0][0], "hot": t, "level_max": level,
                             "id": time.strftime("%Y%m%d_%H%M%S", time.localtime(t))}
                self._state["recording"] = True
            return
        rec["frames"].append(fr)
        rec["level_max"] = max(rec["level_max"], level)
        if level >= 2:
            rec["hot"] = t
        if t - rec["hot"] >= self.post_s or t - rec["t0"] >= self.max_s:
            self._finish()

    def _finish(self, wait: bool = False) -> None:
        rec, self._rec = self._rec, None
        self._state["recording"] = False
        if rec is None:
            return
        th = threading.Thread(target=self._save, args=(rec,), name="video-save")
        th.start()                                      # SD카드 쓰기가 송출을 막지 않게
        if wait:
            th.join()

    def _save(self, rec) -> None:
        d, k = self.clip_dir / rec["id"], 1
        while d.exists():
            d, k = self.clip_dir / f"{rec['id']}_{k}", k + 1
        d.mkdir(parents=True)
        frames = rec["frames"]
        t0 = frames[0][0]
        for i, (_, jpg, _, _) in enumerate(frames):
            (d / f"{i:04d}.jpg").write_bytes(jpg)
        best = min(range(len(frames)), key=lambda i: (-frames[i][2], 1e9 if frames[i][3] is None else frames[i][3]))
        (d / "thumb.jpg").write_bytes(frames[best][1])
        dists = [f[3] for f in frames if f[3] is not None]
        meta = {"id": d.name, "t_start": t0, "duration": round(frames[-1][0] - t0, 2), "n": len(frames),
                "level_max": rec["level_max"], "min_dist": round(min(dists), 1) if dists else None,
                "times": [round(f[0] - t0, 3) for f in frames]}
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(meta))
        tmp.replace(d / "meta.json")                    # meta.json이 마지막 → 목록엔 다 쓴 클립만 나옴
        for old in [x for x in sorted(self.clip_dir.iterdir()) if x.is_dir() and CLIP_ID.match(x.name)][:-self.keep]:
            shutil.rmtree(old, ignore_errors=True)
        self._state["clips_rev"] += 1
        print(f"[video] 경고 클립 저장: {d.name} ({meta['duration']}초, {meta['n']}장)")


def _make_handler(vl: VideoLink):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SP_DEWS-video"

        def log_message(self, *args):
            pass

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")   # 브라우저·다른 프로그램에서도 바로 읽을 수 있게
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            try:
                if path == "/live.mjpg":
                    self._live()
                elif path == "/snapshot.jpg":
                    jpg = vl._jpg
                    self._send(200, "image/jpeg", jpg) if jpg else self.send_error(503, "no frame yet")
                elif path == "/state":
                    self._send(200, "application/json", json.dumps(vl.state()).encode())
                elif path == "/clips":
                    self._send(200, "application/json", json.dumps(vl.list_clips()).encode())
                elif (m := re.fullmatch(r"/clips/(\w+)\.mjpg", path)) and CLIP_ID.match(m.group(1)):
                    self._clip(vl.clip_dir / m.group(1))
                elif (m := re.fullmatch(r"/clips/(\w+)/(thumb|\d{4})\.jpg", path)) and CLIP_ID.match(m.group(1)):
                    p = vl.clip_dir / m.group(1) / f"{m.group(2)}.jpg"
                    self._send(200, "image/jpeg", p.read_bytes()) if p.exists() else self.send_error(404)
                else:
                    self.send_error(404)
            except OSError:                             # 브라우저가 끊음
                pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _mjpeg_start(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            self.wfile.write(b"--frame\r\n")

        def _part(self, jpg: bytes):                    # 뒤에 경계를 바로 붙여야 브라우저가 지금 장면을 바로 그림
            self.wfile.write(b"Content-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg)
                             + jpg + b"\r\n--frame\r\n")
            self.wfile.flush()

        def _live(self):
            self._mjpeg_start()
            vl._clients += 1
            try:
                seq = -1
                while not vl._stop.is_set():
                    jpg, seq = vl.wait_jpg(seq, 2.0)
                    if jpg is not None:
                        self._part(jpg)
            finally:
                vl._clients -= 1

        def _clip(self, d: Path):
            try:
                meta = json.loads((d / "meta.json").read_text())
            except (OSError, ValueError):
                self.send_error(404)
                return
            self._mjpeg_start()
            t0 = time.monotonic()
            for i, rel in enumerate(meta["times"]):
                delay = t0 + rel - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._part((d / f"{i:04d}.jpg").read_bytes())

    return Handler


class NullVideoLink:
    """--video-link 안 줬을 때. 탐지 코드에서 if 없이 쓰려고"""

    def push(self, *args, **kwargs):
        pass

    def close(self):
        pass


def _ips() -> list:
    try:
        return subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []


def make_video_link(port):
    """--video-link 값으로 만들기: None → 안 씀 / 포트 번호 → 송출 시작"""
    if port is None:
        return NullVideoLink()
    try:
        vl = VideoLink(port=port)
    except OSError as e:                                # 포트 사용 중 등 → 탐지는 계속
        print(f"[video] 영상 송출 못 켬 ({e}). 탐지는 계속")
        return NullVideoLink()
    ips = [ip for ip in _ips() if ":" not in ip] or ["<Pi 주소>"]
    print("[video] 영상 송출 중: " + "  ".join(f"http://{ip}:{vl.port}" for ip in ips)
          + f"  (지휘소 앱 '영상 주소'에 이 주소, Pi 핫스팟이면 http://10.42.0.1:{vl.port})")
    return vl


# ---------------------------------------------------------------- 데모 (카메라 없이)
def _demo_frame(t: float, sky):
    """하늘 배경에 쿼드콥터 하나가 왼쪽 위에서 다가오는 장면. 20초 주기"""
    img = sky.copy()
    ph = t % 20.0
    tracks, level = [], 0
    if 2.0 <= ph < 14.0:
        k = (ph - 2.0) / 12.0
        dist = 60.0 - 52.0 * k
        cx, cy = int(260 + 700 * k), int(200 + 160 * k)
        r = int(4 + 3000 / dist / 6)
        cv2.line(img, (cx - r, cy - r), (cx + r, cy + r), (40, 40, 45), max(2, r // 5), cv2.LINE_AA)
        cv2.line(img, (cx - r, cy + r), (cx + r, cy - r), (40, 40, 45), max(2, r // 5), cv2.LINE_AA)
        for dx, dy in ((-r, -r), (r, -r), (-r, r), (r, r)):
            cv2.circle(img, (cx + dx, cy + dy), max(2, r // 3), (60, 60, 70), -1, cv2.LINE_AA)
        level = 2 if dist < 20 else 1
        tracks.append(dict(id=7, conf=0.85, box=[cx - r * 1.4, cy - r * 1.4, cx + r * 1.4, cy + r * 1.4],
                           dist_m=round(dist, 1), src="stereo" if dist < 45 else "mono", level=level))
    return img, level, tracks


def main() -> None:
    ap = argparse.ArgumentParser(description="관측소 영상 Wi-Fi 송출 (지휘소 HUD용)")
    ap.add_argument("--demo", action="store_true", help="카메라 없이 가짜 영상 + 가짜 경고")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()
    if not args.demo:
        ap.print_help()
        return
    vl = make_video_link(args.port)
    yy = np.linspace(0, 1, 720)[:, None, None]
    sky = (np.array([235, 200, 150]) * (1 - yy) + np.array([250, 235, 215]) * yy).astype(np.uint8)
    sky = np.repeat(sky, 1280, axis=1)
    print("데모: 20초마다 드론이 다가오고, 20 m 안쪽이면 경고 → 클립 저장. Ctrl+C로 종료")
    t0 = time.monotonic()
    try:
        while True:
            img, level, tracks = _demo_frame(time.monotonic() - t0, sky)
            vl.push(img, level, tracks)
            time.sleep(1 / 30)
    except KeyboardInterrupt:
        pass
    vl.close()


if __name__ == "__main__":
    main()
