"""
hud/media.py — 관측소 영상(Wi-Fi), 음성 안내, 경보음

VideoClient: 관측소 Pi video_link.py에 붙음. /state 1초마다 → 살아 있으면 /live.mjpg 받아서 프레임 시그널
Voice:       Windows 내장 한국어 음성(QTextToSpeech). 한국어 음성이 없으면 자막만
Cues:        경보음 (실행할 때 짧은 WAV를 만들어서 씀, 파일 따로 없음. 윈도우는 winsound)
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

from PySide6.QtCore import QLocale, QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


# ---------------------------------------------------------------- MJPEG
class MjpegParser:
    """multipart MJPEG 스트림 조각 → JPEG 한 장씩 (JPEG 시작 FFD8 ~ 끝 FFD9로 자름)"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(b"\xff\xd8")
            if i < 0:
                del self.buf[:-1]
                break
            j = self.buf.find(b"\xff\xd9", i + 2)
            if j < 0:
                del self.buf[:i]
                if len(self.buf) > 4_000_000:           # 끝이 안 오는 이상한 스트림
                    self.buf.clear()
                break
            out.append(bytes(self.buf[i:j + 2]))
            del self.buf[:j + 2]
        return out


class VideoClient(QObject):
    frame = Signal(QImage)
    online_changed = Signal(bool)
    clips_changed = Signal(list, list)       # 전체 목록, 새로 생긴 것
    thumb = Signal(str, QImage)
    clip_finished = Signal()

    def __init__(self, base: str = "", parent=None):
        super().__init__(parent)
        self.nam = QNetworkAccessManager(self)
        self.base, self.online, self.state = "", False, {}
        self.rev, self.clips, self.known, self.loaded = None, [], set(), False
        self.mode, self.clip = "live", None
        self._stream = self._parser = self._poll_reply = None
        self._last_ok = 0.0
        self._thumbs: dict = {}
        self._timer = QTimer(self, interval=1000, timeout=self.poll)
        self._timer.start()
        self.set_base(base)

    # ---- 바깥에서 부르는 것
    def set_base(self, base: str) -> None:
        base = (base or "").strip().rstrip("/")
        if base and "://" not in base:
            base = "http://" + base
        if base == self.base:
            return
        self._stop_stream()
        was = self.online
        self.base, self.online, self.state = base, False, {}
        self.rev, self.clips, self.known, self.loaded = None, [], set(), False
        self.mode, self.clip = "live", None
        if was:
            self.online_changed.emit(False)
        self.clips_changed.emit([], [])
        QTimer.singleShot(50, self.poll)

    def play_clip(self, clip: dict) -> None:
        if clip and self.base:
            self.mode, self.clip = "clip", clip
            self._start_stream(f"/clips/{clip['id']}.mjpg")

    def go_live(self) -> None:
        self.mode, self.clip = "live", None
        self._stop_stream()
        if self.online:
            self._start_stream("/live.mjpg")

    def thumb_for(self, cid: str):
        return self._thumbs.get(cid)

    # ---- 상태 확인
    def poll(self) -> None:
        if not self.base or self._poll_reply is not None:
            return
        req = QNetworkRequest(QUrl(self.base + "/state"))
        req.setTransferTimeout(1500)
        r = self._poll_reply = self.nam.get(req)
        r.finished.connect(lambda r=r: self._on_state(r))

    def _on_state(self, r: QNetworkReply) -> None:
        self._poll_reply = None
        st = None
        if r.error() == QNetworkReply.NetworkError.NoError:
            try:
                st = json.loads(bytes(r.readAll()))
            except ValueError:
                st = None
        r.deleteLater()
        now = time.monotonic()
        if isinstance(st, dict):
            self.state, self._last_ok = st, now
            if not self.online:
                self.online = True
                self.online_changed.emit(True)
            if self.mode == "live" and self._stream is None:
                self._start_stream("/live.mjpg")
            if st.get("clips_rev") != self.rev:
                self.rev = st.get("clips_rev")
                self._get("/clips", self._on_clips)
        elif self.online and now - self._last_ok > 3.5:
            self.online = False
            self._stop_stream()
            self.online_changed.emit(False)

    def _get(self, path: str, cb) -> None:
        req = QNetworkRequest(QUrl(self.base + path))
        req.setTransferTimeout(4000)
        r = self.nam.get(req)
        r.finished.connect(lambda r=r: cb(r))

    def _on_clips(self, r: QNetworkReply) -> None:
        ok = r.error() == QNetworkReply.NetworkError.NoError
        data = bytes(r.readAll()) if ok else b""
        r.deleteLater()
        try:
            clips = json.loads(data) if ok else None
        except ValueError:
            clips = None
        if not isinstance(clips, list):
            return
        fresh = [c for c in clips if c.get("id") not in self.known]
        self.known.update(c.get("id") for c in clips)
        self.clips = clips
        self.clips_changed.emit(clips, fresh if self.loaded else [])
        self.loaded = True
        for c in clips[:6]:
            if c["id"] not in self._thumbs:
                self._get(f"/clips/{c['id']}/thumb.jpg", lambda r, cid=c["id"]: self._on_thumb(cid, r))

    def _on_thumb(self, cid: str, r: QNetworkReply) -> None:
        img = QImage.fromData(bytes(r.readAll())) if r.error() == QNetworkReply.NetworkError.NoError else QImage()
        r.deleteLater()
        if not img.isNull():
            self._thumbs[cid] = img
            self.thumb.emit(cid, img)

    # ---- 스트림
    def _start_stream(self, path: str) -> None:
        self._stop_stream()
        req = QNetworkRequest(QUrl(self.base + path))
        req.setTransferTimeout(5000)                   # 5초 동안 아무것도 안 오면 끊고 다시
        r = self._stream = self.nam.get(req)
        self._parser = MjpegParser()
        r.readyRead.connect(lambda r=r: self._on_data(r))
        r.finished.connect(lambda r=r: self._on_end(r))

    def _stop_stream(self) -> None:
        r, self._stream = self._stream, None
        if r is not None:
            r.abort()

    def _on_data(self, r: QNetworkReply) -> None:
        if r is not self._stream:
            return
        jpgs = self._parser.feed(bytes(r.readAll()))
        if jpgs:                                       # 밀려 있으면 마지막 장면만 그림
            img = QImage.fromData(jpgs[-1], "JPG")
            if not img.isNull():
                self.frame.emit(img)

    def _on_end(self, r: QNetworkReply) -> None:
        r.deleteLater()
        if r is not self._stream:
            return
        self._stream = None
        if self.mode == "clip":
            self.clip_finished.emit()
        # 라이브가 끊긴 거면 다음 poll 때 다시 붙음


# ---------------------------------------------------------------- 음성
class Voice(QObject):
    speaking = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled, self.ok, self.note = True, False, ""
        self._prio, self._pending, self.tts = -1, None, None
        try:
            from PySide6.QtTextToSpeech import QTextToSpeech
            self._State = QTextToSpeech.State
            self.tts = QTextToSpeech(self)
        except Exception:                              # 음성 엔진 없음
            self.note = "음성 엔진이 없어 자막만 표시"
            return
        ko = QLocale.Language.Korean
        voices = [v for v in self.tts.availableVoices() if v.locale().language() == ko]
        if not voices:
            for loc in self.tts.availableLocales():
                if loc.language() == ko:
                    self.tts.setLocale(loc)
                    voices = list(self.tts.availableVoices())
                    break
        voices = [v for v in voices if v.locale().language() == ko]
        if not voices:
            self.note = "한국어 음성이 없어 자막만 표시 (Windows 설정 > 시간 및 언어 > 음성에서 한국어 추가)"
            return
        best = next((v for v in voices if any(n in v.name() for n in ("SunHi", "Heami", "InJoon"))), voices[0])
        self.tts.setLocale(best.locale())
        self.tts.setVoice(best)
        self.tts.setRate(0.1)
        self.tts.stateChanged.connect(self._on_state)
        self.ok = True

    def speak(self, text: str, prio: int) -> None:
        if not (self.enabled and self.ok):
            return
        busy = tuple(getattr(self._State, n) for n in ("Speaking", "Synthesizing") if hasattr(self._State, n))
        if self.tts.state() in busy:
            if prio > self._prio:                      # 더 급한 말이면 끊고
                self._pending = (text, prio)
                self.tts.stop()
            elif self._pending is None or prio >= self._pending[1]:
                self._pending = (text, prio)           # 아니면 끝나고 (제일 최근 것 하나만)
            return
        self._prio = prio
        self.tts.say(text)

    def stop(self) -> None:
        self._pending = None
        if self.tts is not None:
            self.tts.stop()

    def _on_state(self, st) -> None:
        if st == self._State.Speaking:
            self.speaking.emit(True)
        elif st in (self._State.Ready, self._State.Error):
            self.speaking.emit(False)
            self._prio = -1
            if self._pending:
                text, prio = self._pending
                self._pending, self._prio = None, prio
                self.tts.say(text)


# ---------------------------------------------------------------- 경보음
_SPECS = {   # (주파수 Hz, 길이 s, 파형) — 주파수 0 = 쉼
    0: [(660, .10, "sin"), (990, .16, "sin")],
    1: [(1046, .14, "sin")],
    2: [(784, .16, "tri"), (0, .06, ""), (784, .16, "tri")],
    3: [(1175, .09, "sq"), (0, .06, ""), (1175, .09, "sq"), (0, .06, ""), (1175, .09, "sq")],
}


def _write_wav(path: Path, spec, rate: int = 22050) -> None:
    frames = bytearray()
    for f, dur, kind in spec:
        n = int(rate * dur)
        for i in range(n):
            env = min(1.0, i / (rate * .008)) * min(1.0, (n - i) / (rate * .03))   # 딸깍 소리 안 나게
            ph = (f * i / rate) % 1.0
            v = 0.0 if not f else (math.sin(2 * math.pi * ph) if kind == "sin" else
                                   (4 * abs(ph - .5) - 1) if kind == "tri" else (0.55 if ph < .5 else -0.55))
            frames += struct.pack("<h", int(v * env * 0.6 * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


class Cues(QObject):
    """윈도우는 내장 winsound로 바로 재생 (Qt 멀티미디어 안 씀 → exe도 가벼움). 다른 OS는 QSoundEffect"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled, self.files, self.fx = True, {}, {}
        d = Path(tempfile.gettempdir()) / "sp_dews_hud"
        try:
            d.mkdir(exist_ok=True)
            for k, spec in _SPECS.items():
                p = d / f"cue{k}.wav"
                if not p.exists():
                    _write_wav(p, spec)
                self.files[k] = p
        except OSError:
            return
        if sys.platform == "win32" or os.environ.get("QT_QPA_PLATFORM") == "offscreen" \
                or os.environ.get("SP_DEWS_NO_AUDIO"):
            return                                     # 화면 없는 시험 환경은 소리 장치가 없어서 건드리지 않음
        try:
            from PySide6.QtMultimedia import QSoundEffect
        except Exception:
            return
        for k, p in self.files.items():
            fx = QSoundEffect(self)
            fx.setSource(QUrl.fromLocalFile(str(p)))
            fx.setVolume(0.45)
            self.fx[k] = fx

    def play(self, kind: int) -> None:
        if not self.enabled or kind not in self.files:
            return
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(str(self.files[kind]), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        elif kind in self.fx:
            self.fx[kind].play()
