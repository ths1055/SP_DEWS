"""
Pi 5 + Camera Module 3 두 대 스테레오 캡처
  - CAM0 = 왼쪽, CAM1 = 오른쪽 (카메라가 바라보는 방향 기준). 반대로 꽂았으면 swap=True
  - 초점 무한대 고정 (오토포커스는 하늘에서 헤매고, 초점 바뀌면 캘리브레이션이 틀어짐)
  - libcamera 소프트웨어 동기화(SyncMode) 지원되면 사용, 안 되면 타임스탬프로 짝 맞춤
"""
import time

import cv2


class StereoCamera:
    def __init__(self, size=(1280, 720), fps=30, sync=True, lens_pos=0.0, swap=False):
        from picamera2 import Picamera2
        from libcamera import controls

        self.period_ms = 1000.0 / fps
        idx = (1, 0) if swap else (0, 1)
        self.cams = [Picamera2(idx[0]), Picamera2(idx[1])]
        self.synced = False

        for i, cam in enumerate(self.cams):
            base = {"FrameRate": float(fps),
                    "AfMode": controls.AfModeEnum.Manual,
                    "LensPosition": float(lens_pos)}
            ctrl = dict(base)
            if sync and hasattr(controls, "rpi") and hasattr(controls.rpi, "SyncModeEnum"):
                ctrl["SyncMode"] = (controls.rpi.SyncModeEnum.Server if i == 0
                                    else controls.rpi.SyncModeEnum.Client)
            # 2304x1296 = 센서 전체 화각(2x2 binning). 이걸 지정 안 하면 잘린 모드가 잡힐 수 있음
            kw = dict(main={"size": size, "format": "RGB888"},
                      sensor={"output_size": (2304, 1296)}, buffer_count=4)
            try:
                cam.configure(cam.create_video_configuration(controls=ctrl, **kw))
                self.synced = self.synced or ("SyncMode" in ctrl)
            except Exception as e:  # 구버전 libcamera면 SyncMode 없이
                print(f"[cam{i}] sync 설정 실패 → 일반 모드 ({e})")
                cam.configure(cam.create_video_configuration(controls=base, **kw))

        # client(오른쪽) 먼저 켜고 server(왼쪽)를 켬
        self.cams[1].start()
        self.cams[0].start()
        time.sleep(1.0)
        print(f"stereo camera ready (sync={'on' if self.synced else 'off'})")

    def _grab(self, i):
        req = self.cams[i].capture_request()
        try:
            return req.make_array("main"), req.get_metadata()["SensorTimestamp"]
        finally:
            req.release()

    def read(self):
        """returns left, right(BGR), skew_ms, t(sec)"""
        (l, tl), (r, tr) = self._grab(0), self._grab(1)
        # 한쪽이 반 프레임 이상 늦으면 그쪽을 한 장 더 받아서 짝을 맞춤
        for _ in range(2):
            d = (tl - tr) / 1e6
            if d > self.period_ms / 2:
                r, tr = self._grab(1)
            elif d < -self.period_ms / 2:
                l, tl = self._grab(0)
            else:
                break
        return l, r, abs(tl - tr) / 1e6, tl / 1e9

    def close(self):
        for c in self.cams:
            c.stop()
            c.close()


class StereoVideo:
    """녹화해둔 left/right 영상으로 맥·PC에서 테스트할 때"""

    def __init__(self, left_path, right_path):
        self.l, self.r = cv2.VideoCapture(left_path), cv2.VideoCapture(right_path)
        self.fps = self.l.get(cv2.CAP_PROP_FPS) or 30
        self.n = 0

    def read(self):
        okl, l = self.l.read()
        okr, r = self.r.read()
        if not (okl and okr):
            return None, None, 0.0, 0.0
        self.n += 1
        return l, r, 0.0, self.n / self.fps

    def close(self):
        self.l.release()
        self.r.release()
