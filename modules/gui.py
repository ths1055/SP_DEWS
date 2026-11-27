#!/usr/bin/env python3
"""
gui.py — 지휘소 경보 표시 앱 (⑩). PySide6(Qt) 데스크톱 앱, 브라우저 필요 없음

alert_rx.py가 보내는 UDP JSON(127.0.0.1:5005)을 받아서 홀로그램 스타일 화면에 띄운다.
관측소 Pi가 Wi-Fi로 영상(yolo_module/pi/video_link.py)을 내보내면 라이브 영상·경고 클립도 같이 보여줌.

  pip install PySide6-Addons pyserial         처음 한 번 (PySide6-Addons 안에 음성·소리 모듈이 있음)
  python gui.py                               alert_rx.py를 따로 켤 때 (README 1장 구조 그대로)
  python gui.py --lora COM4                   alert_rx까지 한 번에 (앱 위쪽 'LoRa' 버튼으로 골라도 됨)
  python gui.py --demo                        LoRa 없이 가짜 경보로 화면 확인 ('데모' 버튼도 같음)
  python gui.py --video http://10.42.0.1:8090 관측소 Pi 영상 주소 (기본값. 앱 안 '영상 주소'로도 바꿈)
  python gui.py --selftest                    하드웨어·화면 없이 자체 시험
  python build_exe.py                         exe 만들기 → dist/SP_DEWS_HUD/SP_DEWS_HUD.exe

단축키: F11 전체 화면, V 음성, S 경보음, R 스코프 범위, L 라이브로
버튼으로 바꾼 설정(LoRa 포트, 영상 주소, 음성·경보음, 범위)은 저장돼서 다음에 그대로 켜짐
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import socket
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

try:
    from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal
    from PySide6.QtGui import QIcon, QKeySequence, QPainter, QPixmap, QShortcut
    from PySide6.QtNetwork import QHostAddress, QUdpSocket
    from PySide6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QMainWindow,
                                   QMessageBox, QPushButton, QVBoxLayout, QWidget)
except ImportError:
    print("PySide6가 없음 → pip install PySide6-Addons")
    sys.exit(1)

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))   # exe로 묶였을 때도 hud/fonts 찾게
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hud import widgets as W                     # noqa: E402
from hud.media import Cues, MjpegParser, VideoClient, Voice   # noqa: E402
from hud.model import ALERT_KEYS, Model, speed_text           # noqa: E402

UDP_PORT = 5005                                   # alert_rx.py GUI_ADDR 포트와 같아야 함
VIDEO_URL = "http://10.42.0.1:8090"               # Pi 핫스팟(nmcli) 기본 주소 + video_link.py 기본 포트


# ---------------------------------------------------------------- 입력: UDP JSON
class UdpInput(QObject):
    def __init__(self, port: int, model: Model):
        super().__init__()
        self.model, self.stats = model, {"udp": 0, "bad": 0}
        self.sock = QUdpSocket(self)
        self.ok = self.sock.bind(QHostAddress(QHostAddress.SpecialAddress.LocalHost), port)
        self.port = self.sock.localPort()
        self.sock.readyRead.connect(self._read)

    def _read(self) -> None:
        while self.sock.hasPendingDatagrams():
            data = bytes(self.sock.receiveDatagram().data())
            try:
                msg = json.loads(data)
                if not isinstance(msg, dict) or not ALERT_KEYS <= msg.keys():
                    raise ValueError("경보 필드 부족")
            except ValueError:                    # JSON 깨짐·다른 프로그램이 보낸 것
                self.stats["bad"] += 1
                continue
            self.stats["udp"] += 1
            self.model.on_alert(msg)

    def drain(self) -> None:
        """이미 도착해 있는 것까지 지금 바로 처리 (데모 끝낼 때)"""
        self._read()


class Bridge(QObject):
    """다른 스레드(LoRa·데모)에서 화면 쪽으로 글을 넘길 때"""
    sys_msg = Signal(str)


# ---------------------------------------------------------------- LoRa (alert_rx 같이 돌리기)
class LoraThread:
    """alert_rx.AlertRx를 그대로 돌림 → 자기 UDP 포트로 보냄 (따로 켤 때랑 같은 경로, ACK·중복 제거 그대로)"""

    def __init__(self, port: str, udp_port: int, gpio: bool, bridge: Bridge):
        self.port, self.udp_port, self.gpio, self.bridge = port, udp_port, gpio, bridge
        self.stop, self.status, self.stats = threading.Event(), "시작 중", {}
        threading.Thread(target=self._run, name="lora", daemon=True).start()

    def _say(self, msg: str) -> None:
        try:
            self.bridge.sys_msg.emit(msg)
        except RuntimeError:                      # 앱 창이 먼저 닫힘 (종료 중)
            pass

    def _run(self) -> None:
        try:
            from alert_rx import AlertRx
            from e22 import E22
        except ImportError as e:
            self.status = f"모듈 없음: {e}"
            self._say(f"LoRa 모듈을 못 불러옴 ({e}) → pip install pyserial")
            return
        while not self.stop.is_set():
            try:
                with E22(self.port, use_gpio=self.gpio) as lora:
                    rx = AlertRx(lora, ("127.0.0.1", self.udp_port))
                    self.status = "ok"
                    self._say(f"LoRa 수신 시작 ({self.port})")
                    try:
                        while not self.stop.is_set():
                            rx.poll()
                            self.stats = dict(rx.stats)
                    finally:
                        rx.close()
            except Exception as e:                # 포트 없음, USB 빠짐 → 3초 뒤 다시
                msg = f"{type(e).__name__}: {e}"
                if self.status != msg:
                    self._say(f"LoRa 오류 ({self.port}): {e}  (3초마다 다시 시도)")
                self.status = msg
                self.stop.wait(3.0)
        self._say(f"LoRa 수신 끔 ({self.port})")


# ---------------------------------------------------------------- 데모
def _demo_tracks(t: float, base: int):
    """t초 → 지금 보이는 표적 [(track_id, dist, speed, az, el, stereo)], 끝난 표적 id"""
    out, gone = [], []
    if 4 <= t < 29:                               # A: 72 m에서 좌측으로 휘어 들어오며 접근
        d = 72 - 2.0 * min(t - 4, 10) - 3.5 * max(0.0, t - 14)
        if d > 2.0:
            out.append((base + 7, d, 2.0 if t < 14 else 3.5, 28 - 1.4 * (t - 4), 18 - 0.5 * (t - 4), d < 45))
    if t >= 29:
        gone.append(base + 7)
    if 10 <= t < 34:                              # B: 46 m 부근을 가로지름 (접근 안 함)
        out.append((base + 12, 46 + 2 * math.sin(t / 3), 0.1 * ((t * 7) % 3 - 1), -42 + 2.5 * (t - 10), 30.0, False))
    if t >= 34:
        gone.append(base + 12)
    if 36 <= t < 40:                              # C: 잠깐 잡혔다 사라짐 (갱신 끊김 → 화면에서 정리)
        out.append((base + 3, 33.0, 0.4, 40.0, 25.0, True))
    return out, gone


def demo_loop(udp_port: int, stop: threading.Event) -> None:
    """alert_tx → LoRa → alert_rx 흉내: protocol로 포장·개봉(단위 반올림 그대로) 후 UDP JSON"""
    from protocol import Alert, SeqCounter, decode, encode_alert, now_ms16

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq, rng = SeqCounter(random.randrange(256)), random.Random(7)
    period = {1: 2.0, 2: 1.0, 3: 0.5}            # README 전송 정책: 같은 등급은 주기 갱신, 위험은 더 자주
    sent = [time.monotonic() - 3.5]              # 마지막 송신 (첫 생존 신호는 1.5초 뒤, 시작 인사 다음)

    def send(tid, level, d, sp, az, el, stereo):
        sent[0] = time.monotonic()
        tta = d / sp if sp > 0.3 else None
        a = Alert(node_id=1, seq=seq.next(), track_id=tid, level=level, stereo=stereo,
                  dist_m=d, speed_mps=sp, tta_s=tta, az_deg=az, el_deg=el, ts_ms=now_ms16())
        if rng.random() < 0.03:                   # 재전송까지 실패한 경보 (seq만 지나감)
            return
        sock.sendto(json.dumps(asdict(decode(encode_alert(a)))).encode(), ("127.0.0.1", udp_port))

    cycle = 0
    while not stop.is_set():
        base, last, t0 = (cycle * 20) % 200, {}, time.monotonic()
        while not stop.is_set():
            t = time.monotonic() - t0
            if t > 48:
                break
            live, gone = _demo_tracks(t, base)
            for tid, d, sp, az, el, stereo in live:
                tta = d / sp if sp > 0.3 else None
                level = 3 if d <= 20 or (tta is not None and tta <= 5) else 2 if (sp >= 2 and d <= 40) else 1
                lv, ts = last.get(tid, (None, -9.0))
                if level != lv or t - ts >= period[level]:
                    send(tid, level, d + rng.gauss(0, 0.15), sp + rng.gauss(0, 0.1), az + rng.gauss(0, 0.3), el, stereo)
                    last[tid] = (level, t)
            for tid in gone:
                if tid in last and tid != base + 3:      # C는 '없음' 없이 끊김
                    send(tid, 0, 2.0, 0.0, 0.0, 0.0, True)
                last.pop(tid, None)
            if not live and time.monotonic() - sent[0] >= 5.0:   # 표적 없으면 5초마다 생존 신호 (alert_gen과 같게)
                send(0, 0, 0.0, 0.0, 0.0, 0.0, False)
            stop.wait(0.1)
        cycle += 1


# ---------------------------------------------------------------- 창
def app_icon() -> QIcon:
    pm = QPixmap(128, 128)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    W.Brand.draw_logo(p, 64, 64, 46)
    p.end()
    return QIcon(pm)


def stylesheet(s: float) -> str:
    px = lambda v: f"{max(9, int(v * s))}px"
    return f"""
    QWidget {{ color: rgb(232,250,255); font-family: "{W.UI}"; font-size: {px(14)}; }}
    QMainWindow, QDialog {{ background: rgb(4,14,30); }}
    QLabel#dim {{ color: rgb(104,152,190); font-size: {px(13)}; }}
    QLabel#badge {{ color: rgb(2,8,20); background: rgb(255,181,71); border-radius: {px(9)}; padding: 1px 9px;
                    font-size: {px(12)}; }}
    QLabel#warn {{ color: rgb(255,77,94); font-size: {px(12.5)}; }}
    QLabel#clock {{ font-family: "{W.NUM}"; font-size: {px(19)}; }}
    QLabel#date {{ color: rgb(104,152,190); font-size: {px(11.5)}; }}
    QLabel#sub {{ font-size: {px(18)}; }}
    QLabel#note {{ color: rgb(104,152,190); font-size: {px(12)}; }}
    QPushButton {{ color: rgb(160,205,235); background: rgba(58,168,255,26); border: 1px solid rgba(143,227,255,75);
                   border-radius: {px(13)}; padding: {px(4)} {px(12)}; font-size: {px(12.5)}; }}
    QPushButton:hover {{ border-color: rgba(143,227,255,200); color: rgb(232,250,255); }}
    QPushButton:checked {{ background: rgba(58,168,255,75); border-color: rgba(143,227,255,200); color: rgb(232,250,255); }}
    QPushButton:focus {{ border-color: rgb(232,250,255); }}
    QLineEdit, QComboBox {{ background: rgba(58,168,255,30); border: 1px solid rgba(143,227,255,90);
                            border-radius: 6px; padding: 4px 8px; }}
    QComboBox QAbstractItemView {{ background: rgb(6,22,44); selection-background-color: rgba(58,168,255,110); }}
    """


class HudWindow(QMainWindow):
    def __init__(self, cfg: dict, prefs: QSettings, model: Model, udp: UdpInput, bridge: Bridge,
                 lora_gpio: bool = False):
        super().__init__()
        self.cfg, self.qs, self.model, self.udp, self.bridge = cfg, prefs, model, udp, bridge
        self.lora_gpio = lora_gpio
        self.prefs = {"range": int(prefs.value("range", 0))}
        self.lora = None
        self.demo_stop = None
        self.auto_back = False
        self.voice, self.cues = Voice(self), Cues(self)
        self.voice.enabled = prefs.value("voice", "true") == "true"
        self.cues.enabled = prefs.value("sound", "true") == "true"
        self.video = VideoClient(cfg["video"], self)
        self.setWindowTitle("SP_DEWS 지휘소")
        self.setWindowIcon(app_icon())

        root = W.Backdrop()
        self.setCentralWidget(root)
        m = int(14 * W.SCALE)
        grid = QGridLayout(root)
        grid.setContentsMargins(m, int(m * .6), m, m)
        grid.setSpacing(m)
        grid.addWidget(self._header(), 0, 0, 1, 3)
        self.p_left, self.p_right = self._left(), self._right()
        self.scope = W.ScopeWidget(model, cfg, self.prefs)
        self.p_voice, self.p_log, self.p_link = self._voice_panel(), self._log_panel(), self._link_panel()
        grid.addWidget(self.p_left, 1, 0)
        grid.addWidget(self.scope, 1, 1)
        grid.addWidget(self.p_right, 1, 2)
        grid.addWidget(self.p_voice, 2, 0)
        grid.addWidget(self.p_log, 2, 1)
        grid.addWidget(self.p_link, 2, 2)
        grid.setColumnStretch(0, 24)
        grid.setColumnStretch(1, 49)
        grid.setColumnStretch(2, 27)
        grid.setRowStretch(1, 80)
        grid.setRowStretch(2, 20)
        self.p_left.setMinimumWidth(int(300 * W.SCALE))
        self.p_right.setMinimumWidth(int(330 * W.SCALE))
        self.panels = (self.p_left, self.p_right, self.p_voice, self.p_log, self.p_link)
        root.overlay = self.edge = W.EdgeOverlay(root)
        self.edge.raise_()

        model.log.connect(self.logv.add)
        model.say.connect(self._say)
        model.cue.connect(self.cues.play)
        model.level_changed.connect(self._on_level)
        bridge.sys_msg.connect(self._sys)
        self.video.frame.connect(self.view.set_image)
        self.video.online_changed.connect(self._on_video_online)
        self.video.clips_changed.connect(self._on_clips)
        self.video.thumb.connect(lambda *_: self.strip.update())
        self.video.clip_finished.connect(self._on_clip_end)
        self.voice.speaking.connect(self._on_speaking)
        self.view.clicked.connect(lambda: self._play(self.video.clip) if self.view.ended else None)
        self.strip.clicked.connect(lambda c: (setattr(self, "auto_back", False), self._play(c)))
        self.b_live.clicked.connect(self._go_live)
        self.b_replay.clicked.connect(lambda: self._play(self.video.clips[0] if self.video.clips else None))
        self.b_voice.clicked.connect(self._toggle_voice)
        self.b_sound.clicked.connect(self._toggle_sound)
        self.b_range.clicked.connect(self._cycle_range)
        self.b_video.clicked.connect(self._ask_video)
        self.b_lora.clicked.connect(self._ask_lora)
        self.b_demo.clicked.connect(lambda on: self.set_demo(on))
        self.b_full.clicked.connect(self._toggle_full)
        for key, fn in (("F11", self._toggle_full), ("V", self._toggle_voice), ("S", self._toggle_sound),
                        ("R", self._cycle_range), ("L", self._go_live), ("Esc", self._exit_full)):
            QShortcut(QKeySequence(key), self, activated=fn)

        self._anim = QTimer(self, interval=16, timeout=self._animate)
        self._slow = QTimer(self, interval=100, timeout=self._refresh)
        self._clk = QTimer(self, interval=1000, timeout=self._clock)
        for t in (self._anim, self._slow, self._clk):
            t.start()
        self._clock()
        self._refresh()
        self._sync_buttons()
        QTimer.singleShot(900, self._greet)

    # ---- 화면 조각
    def _btn(self, text: str, checkable: bool = False, tip: str = "") -> QPushButton:
        b = QPushButton(text)
        b.setCheckable(checkable)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if tip:
            b.setToolTip(tip)
        return b

    def _header(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(int(10 * W.SCALE))
        h.addWidget(W.Brand())
        self.node_line = QLabel("관측소 신호 대기")
        self.node_line.setObjectName("dim")
        self.node_line.setTextFormat(Qt.TextFormat.RichText)
        h.addWidget(self.node_line)
        self.badge = QLabel("데모 데이터")
        self.badge.setObjectName("badge")
        self.badge.hide()
        h.addWidget(self.badge)
        h.addStretch(1)
        self.chip = W.StatusChip(self.model)
        h.addWidget(self.chip)
        h.addStretch(1)
        self.warn = QLabel("")
        self.warn.setObjectName("warn")
        h.addWidget(self.warn)
        self.b_lora = self._btn("LoRa 끔", tip="LoRa 수신기(USB-UART 포트) 고르기")
        self.b_demo = self._btn("데모", True, "가짜 경보로 화면 확인")
        self.b_voice = self._btn("음성", True, "음성 안내 (V)")
        self.b_sound = self._btn("경보음", True, "경보음 (S)")
        self.b_range = self._btn("범위 자동", tip="스코프 범위 (R)")
        self.b_video = self._btn("영상 주소", tip="관측소 Pi 영상 주소 (video_link.py)")
        self.b_full = self._btn("전체 화면", tip="F11")
        for b in (self.b_lora, self.b_demo, self.b_voice, self.b_sound, self.b_range, self.b_video, self.b_full):
            h.addWidget(b)
        clk = QVBoxLayout()
        clk.setSpacing(0)
        self.clock, self.date = QLabel(), QLabel()
        self.clock.setObjectName("clock")
        self.date.setObjectName("date")
        self.clock.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.date.setAlignment(Qt.AlignmentFlag.AlignRight)
        clk.addWidget(self.clock)
        clk.addWidget(self.date)
        h.addSpacing(int(6 * W.SCALE))
        h.addLayout(clk)
        return bar

    def _panel_layout(self, pnl, tilt: int):
        v = QVBoxLayout(pnl)
        pad = int(18 * W.SCALE)
        top = pad + (int(20 * W.SCALE) if tilt > 0 else 0)          # 기울어서 낮아진 모서리 피하기
        v.setContentsMargins(pad + (8 if tilt > 0 else 0), top, pad + (8 if tilt < 0 else 0), pad)
        v.setSpacing(int(8 * W.SCALE))
        return v

    def _left(self) -> QWidget:
        pnl = W.GlassPanel(tilt=-1)
        v = self._panel_layout(pnl, -1)
        v.addWidget(W.SectionTitle("주 표적"))
        self.dial = W.ThreatDial(self.model, self.cfg)
        v.addWidget(self.dial, 1)
        self.c_speed, self.c_tta, self.c_mode = W.Chip("접근 속도"), W.Chip("도달 예상"), W.Chip("거리 측정")
        for c in (self.c_speed, self.c_tta, self.c_mode):
            v.addWidget(c)
        self.dirg = W.DirGauge(self.model, self.cfg)
        v.addWidget(self.dirg)
        v.addWidget(W.SectionTitle("다른 표적"))
        self.tlist = W.TrackList(self.model)
        v.addWidget(self.tlist)
        return pnl

    def _right(self) -> QWidget:
        pnl = W.GlassPanel(tilt=1)
        v = self._panel_layout(pnl, 1)
        self.t_video = W.SectionTitle("관측소 영상")
        v.addWidget(self.t_video)
        self.view = W.VideoView()
        v.addWidget(self.view)
        row = QHBoxLayout()
        self.b_live = self._btn("라이브", True, "L")
        self.b_replay = self._btn("최근 클립 재생")
        row.addWidget(self.b_live)
        row.addWidget(self.b_replay)
        v.addLayout(row)
        v.addWidget(W.SectionTitle("경고 클립"))
        self.strip = W.ClipStrip(self.video)
        v.addWidget(self.strip)
        v.addStretch(1)
        return pnl

    def _link_panel(self) -> QWidget:
        pnl = W.GlassPanel(0)
        v = QVBoxLayout(pnl)
        pad = int(16 * W.SCALE)
        v.setContentsMargins(pad, int(pad * .8), pad, int(pad * .6))
        v.setSpacing(0)
        v.addWidget(W.SectionTitle("링크 상태"))
        rings = QHBoxLayout()
        self.r_lora, self.r_wifi = W.RingGauge("LoRa 전달률 (목표 95%)"), W.RingGauge("Wi-Fi 영상")
        rings.addWidget(self.r_lora)
        rings.addWidget(self.r_wifi)
        v.addLayout(rings, 1)
        return pnl

    def _voice_panel(self) -> QWidget:
        pnl = W.GlassPanel(0)
        h = QHBoxLayout(pnl)
        pad = int(16 * W.SCALE)
        h.setContentsMargins(pad, pad, pad, pad)
        self.orb = W.VoiceOrb()
        h.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        self.subtitle = QLabel("대기 중")
        self.subtitle.setObjectName("sub")
        self.subtitle.setWordWrap(True)
        self.vnote = QLabel(self.voice.note)
        self.vnote.setObjectName("note")
        self.vnote.setWordWrap(True)
        col.addStretch(1)
        col.addWidget(self.subtitle)
        col.addWidget(self.vnote)
        col.addStretch(1)
        h.addLayout(col, 1)
        return pnl

    def _log_panel(self) -> QWidget:
        pnl = W.GlassPanel(0)
        v = QVBoxLayout(pnl)
        pad = int(16 * W.SCALE)
        v.setContentsMargins(pad, int(pad * .8), pad, int(pad * .6))
        v.addWidget(W.SectionTitle("기록"))
        self.logv = W.LogView()
        v.addWidget(self.logv, 1)
        return pnl

    # ---- 이벤트
    def _greet(self) -> None:
        self.cues.play(0)
        self._say("지휘 시스템 가동. " + (f"지금까지 받은 경보 {self.model.total}건." if self.model.total
                                       else "관측소 경보를 기다립니다."), 0)

    def _say(self, text: str, prio: int) -> None:
        self.subtitle.setText(text)
        self.voice.speak(text, prio)

    def _sys(self, msg: str) -> None:
        self.model.log.emit(-1, msg, time.time(), "")
        self.cues.play(0)

    def _on_speaking(self, on: bool) -> None:
        self.orb.speaking = on

    def _on_level(self, new: int, old: int) -> None:
        acc = W.LEVEL_RGB[new] if new >= 2 else W.EDGE
        for pnl in self.panels:
            pnl.set_accent(acc)
        self.edge.set_level(new)
        if new >= 2 and self.video.mode == "clip" and self.auto_back:   # 자동 재생 중 위협이 오면 라이브로
            self._go_live()

    def _on_video_online(self, on: bool) -> None:
        host = self.video.base.split("://")[-1]
        if on:
            self._sys(f"영상 링크 연결 ({host})")
            self._say("영상 링크 연결.", 0)
        else:
            self._sys("영상 링크 끊김 (경보는 LoRa로 계속 수신)")
            self._say("영상 링크 끊김. 경보는 LoRa로 계속 받습니다.", 0)

    def _on_clips(self, clips: list, fresh: list) -> None:
        self.strip.set_clips(clips)
        if fresh:
            c = fresh[0]
            self.strip.fresh.add(c["id"])
            self._sys(f"경고 클립 저장 {time.strftime('%H:%M:%S', time.localtime(c['t_start']))}  {c['duration']:.1f}초")
            self._say("경고 영상 저장됨.", 0)
            if self.video.mode == "live" and self.model.level <= 1:   # 상황 끝났으면 바로 다시 보여줌
                self.auto_back = True
                self._play(c)

    def _play(self, clip) -> None:
        if not clip:
            return
        self.strip.current = clip["id"]
        self.strip.fresh.discard(clip["id"])
        self.view.ended = False
        self.view.tag = "클립 " + time.strftime("%H:%M:%S", time.localtime(clip["t_start"]))
        self.video.play_clip(clip)
        self._sync_buttons()

    def _on_clip_end(self) -> None:
        self.view.ended = True
        self.view.update()
        if self.auto_back:
            self.auto_back = False
            QTimer.singleShot(2500, lambda: self._go_live() if self.view.ended else None)

    def _go_live(self) -> None:
        self.view.ended, self.view.tag, self.strip.current = False, "라이브", None
        self.video.go_live()
        self.strip.update()
        self._sync_buttons()

    # ---- 버튼
    def _sync_buttons(self) -> None:
        self.b_voice.setChecked(self.voice.enabled)
        self.b_sound.setChecked(self.cues.enabled)
        self.b_range.setText(f"범위 {self.prefs['range']} m" if self.prefs["range"] else "범위 자동")
        self.b_live.setChecked(self.video.mode == "live")
        self.b_demo.setChecked(self.demo_stop is not None)
        self.badge.setVisible(self.demo_stop is not None)
        self.b_lora.setText(f"LoRa {self.lora.port}" if self.lora else "LoRa 끔")

    def _toggle_voice(self) -> None:
        self.voice.enabled = not self.voice.enabled
        if not self.voice.enabled:
            self.voice.stop()
        self.qs.setValue("voice", str(self.voice.enabled).lower())
        self._sync_buttons()

    def _toggle_sound(self) -> None:
        self.cues.enabled = not self.cues.enabled
        self.qs.setValue("sound", str(self.cues.enabled).lower())
        self._sync_buttons()

    def _cycle_range(self) -> None:
        opts = [0, 50, 100, 150]
        self.prefs["range"] = opts[(opts.index(self.prefs["range"]) + 1) % len(opts)] if self.prefs["range"] in opts else 0
        self.qs.setValue("range", self.prefs["range"])
        self._sync_buttons()

    def _ask_video(self) -> None:
        url, ok = QInputDialog.getText(self, "관측소 영상 주소",
                                       "video_link.py 주소 (Pi 핫스팟이면 http://10.42.0.1:8090, 비우면 영상 끔)",
                                       text=self.video.base or VIDEO_URL)
        if ok:
            self.qs.setValue("video", url.strip())
            self.video.set_base(url.strip())
            self._go_live()

    def _ask_lora(self) -> None:
        try:
            from serial.tools import list_ports
            ports = [f"{p.device}  {p.description}" for p in list_ports.comports()]
        except ImportError:
            QMessageBox.warning(self, "LoRa", "pyserial이 없음 → pip install pyserial")
            return
        items = ports + ["끄기"]
        cur = next((i for i, s in enumerate(items) if self.lora and s.split()[0] == self.lora.port), 0)
        item, ok = QInputDialog.getItem(self, "LoRa 수신기",
                                        "E22가 붙은 USB-UART 포트 (M0·M1 점퍼 GND)\n"
                                        "alert_rx.py를 따로 켜 둔 상태면 '끄기'로 두세요", items, cur, False)
        if ok:
            self.set_lora(None if item == "끄기" else item.split()[0])

    def set_lora(self, port) -> None:
        if self.lora:
            self.lora.stop.set()
            self.lora = None
        if port:
            self.lora = LoraThread(port, self.udp.port, self.lora_gpio, self.bridge)
        self.qs.setValue("lora_port", port or "")
        self._sync_buttons()

    def set_demo(self, on: bool) -> None:
        if self.demo_stop is not None:
            self.demo_stop.set()
            self.demo_thread.join(1.0)
            self.demo_stop = None
            if not on:                            # 가짜 표적·관측소 기록 지움 (안 지우면 15초 뒤 '신호 끊김'이 뜸)
                self.udp.drain()
                self.model.reset()
                self._sys("데모 끝 (가짜 경보 지움)")
        if on:
            self.demo_stop = threading.Event()
            self.demo_thread = threading.Thread(target=demo_loop, args=(self.udp.port, self.demo_stop),
                                                name="demo", daemon=True)
            self.demo_thread.start()
            self._sys("데모 시작 (가짜 경보)")
        self._sync_buttons()

    def _toggle_full(self) -> None:
        self.showMaximized() if self.isFullScreen() else self.showFullScreen()

    def _exit_full(self) -> None:
        if self.isFullScreen():
            self.showMaximized()

    # ---- 주기 갱신
    def _animate(self) -> None:
        self.model.tick()
        for wdg in (self.scope, self.dial, self.orb, self.r_lora, self.r_wifi):
            wdg.update()
        if self.model.level == 3:
            self.chip.update()
        self.edge.tick()
        if not (self.video.online and self.view.img is not None) or self.view.recording:
            self.view.update()

    def _refresh(self) -> None:
        now = time.monotonic()
        m, v = self.model, self.video
        tr = m.primary()
        if tr:
            tt = tr.tta_now(now)
            self.c_speed.set(speed_text(tr.speed))
            self.c_tta.set("해당 없음" if tt is None else "도달" if tt <= 0 else f"{tt:.1f} s",
                           W.RED if tr.level == 3 and tt is not None else W.CORE)
            self.c_mode.set("스테레오" if tr.stereo else "단안 추정 (오차 큼)", W.CORE if tr.stereo else W.AMBER)
        else:
            for c in (self.c_speed, self.c_tta, self.c_mode):
                c.set("--")
        links = m.links(now)
        self.node_line.setText("&nbsp;&nbsp;&nbsp;".join(self._link_html(*x) for x in links) if links
                               else "관측소 신호 대기")
        self.chip.update()
        self.dirg.update()
        self.tlist.update()
        cnt, lost, rate = m.delivery()
        if rate is None:
            self.r_lora.set(0, "--", "경보 없음")
        else:
            self.r_lora.set(rate / 100, f"{rate:.1f}", f"받음 {cnt}  빠짐 {lost}", W.EDGE if rate >= 95 else W.AMBER, .95)
        host = v.base.split("://")[-1]
        if not v.base:
            self.r_wifi.set(0, "끔", "", W.DIM)
        elif v.online:
            fps = v.state.get("fps_out") or 0
            self.r_wifi.set(1, "연결", f"{host}  {fps:.0f} fps" if fps else host, W.TEAL)
        else:
            self.r_wifi.set(1, "끊김", host, W.AMBER, dashed=True)
        self.t_video.set_aside("Wi-Fi 연결됨" if v.online else "연결 안 됨" if v.base else "꺼짐",
                               W.TEAL if v.online else W.DIM)
        self.view.has_base, self.view.online = bool(v.base), v.online
        self.view.recording = bool(v.state.get("recording")) and v.online
        self.strip.online = v.online
        self.warn.setText("" if not self.lora or self.lora.status == "ok" else f"LoRa {self.lora.port} 연결 안 됨")
        self.warn.setToolTip(self.lora.status if self.lora else "")

    @staticmethod
    def _link_html(nid: int, state: str, age: float) -> str:
        """머리줄 관측소 상태: ● 정상(초록) / 신호 끊김(주황) / 생존 신호 없는 관측소는 마지막 수신 시각"""
        ago = f"{age:.0f}초" if age < 60 else f"{age / 60:.0f}분"
        if state == "down":
            return f'<span style="color:rgb{W.AMBER}">● 관측소 {nid} 신호 끊김 ({ago}째)</span>'
        if state == "ok":
            return f'<span style="color:rgb{W.TEAL}">●</span> 관측소 {nid} 정상'
        return f"● 관측소 {nid} 마지막 경보 {ago} 전"

    def _clock(self) -> None:
        t = time.localtime()
        self.clock.setText(time.strftime("%H:%M:%S", t))
        self.date.setText(f"{t.tm_mon}월 {t.tm_mday}일 ({'월화수목금토일'[t.tm_wday]})")

    def closeEvent(self, e):
        self.qs.setValue("geometry", self.saveGeometry())
        if self.lora:
            self.lora.stop.set()
        if self.demo_stop:
            self.demo_stop.set()
        super().closeEvent(e)


def _dark_titlebar(win: QWidget) -> None:
    if sys.platform != "win32":
        return
    try:                                          # 윈도우 제목 표시줄도 어둡게
        import ctypes
        v = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(int(win.winId()), 20, ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


def make_app(argv) -> QApplication:
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("SP_DEWS HUD")
    app.setStyle("Fusion")
    W.load_fonts(BASE / "hud" / "fonts")
    scr = app.primaryScreen().availableGeometry() if app.primaryScreen() else None
    W.SCALE = 1.0 if scr is None else max(.8, min(1.35, min(scr.width() / 1600, scr.height() / 900)))
    app.setStyleSheet(stylesheet(W.SCALE))
    return app


# ---------------------------------------------------------------- 자체 시험
def selftest() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = make_app(sys.argv[:1])
    from protocol import Alert, decode, encode_alert

    def alert(seq, track, level, dist, speed=4.5, az=-12.5):
        a = Alert(node_id=1, seq=seq, track_id=track, level=level, stereo=True, dist_m=dist, speed_mps=speed,
                  tta_s=dist / speed if speed > 0.3 else None, az_deg=az, el_deg=8.0)
        return asdict(decode(encode_alert(a)))

    m = Model()
    logs, says = [], []
    m.log.connect(lambda *a: logs.append(a))
    m.say.connect(lambda *a: says.append(a))
    t0 = 1000.0
    m.on_alert(alert(10, 7, 1, 45.0, 1.0), t0)
    m.on_alert(alert(11, 9, 2, 35.0), t0 + .1)
    m.on_alert(alert(13, 7, 3, 18.4), t0 + .2)          # seq 12 빠짐
    assert m.level == 3 and m.primary().id == 7 and [t.id for t in m.others()] == [9]
    assert m.delivery()[:2] == (3, 1), m.delivery()
    assert abs(m.primary().tta_now(t0 + 1.2) - (18.4 / 4.5 - 1.0)) < .06
    assert says[-1][0].startswith("위험. 드론 18미터, 좌측 12도.") and says[-1][1] == 3, says[-1]
    m.on_alert(alert(14, 7, 0, 2.0), t0 + .3)           # 없음 → 소실
    assert m.level == 2 and 7 not in [t.id for t in m.tracks.values()]
    m.tick(t0 + .1 + 8.5)                               # 갱신 8초 넘게 없음 → 정리
    assert m.level == 0 and not m.tracks and any(l[3] == "끊김" for l in logs)
    m.tick(t0 + 60)                                     # 생존 신호를 안 보내는 관측소는 '끊김'으로 안 봄
    assert not m.link_down and m.links(t0 + 60)[0][1] == "old"

    m3 = Model()                                        # 생존 신호 → '드론 없음' / '신호 끊김' 구분
    says3 = []
    m3.say.connect(lambda *a: says3.append(a[0]))
    hb = lambda seq: alert(seq, 0, 0, 0.0, 0.0)        # 등급 0 + track_id 0
    m3.on_alert(hb(200), t0)
    assert m3.total == 0 and not m3.tracks and m3.links(t0) == [(1, "ok", 0.0)] and says3 == ["관측소 1 연결."]
    m3.tick(t0 + 14.9)
    assert not m3.link_down
    m3.tick(t0 + 15.5)
    assert m3.link_down and m3.links(t0 + 15.5)[0][1] == "down" and says3[-1].startswith("관측소 1 신호 끊김")
    m3.on_alert(hb(202), t0 + 20)                       # 다시 옴 → 복구 (seq 201 빠짐)
    assert not m3.link_down and says3[-1] == "관측소 1 신호 복구." and m3.delivery()[:2] == (2, 1)

    p = MjpegParser()
    jpg = b"\xff\xd8" + b"abc" + b"\xff\xd9"
    stream = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n--frame\r\n" + jpg[:4]
    got = [x for i in range(0, len(stream), 7) for x in p.feed(stream[i:i + 7])] + p.feed(jpg[4:])
    assert got == [jpg, jpg], got

    m2 = Model()
    udp = UdpInput(0, m2)
    assert udp.ok
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for data in (json.dumps(alert(1, 3, 2, 30.0)).encode(), b"not json", b'{"level": 3}'):
        s.sendto(data, ("127.0.0.1", udp.port))
    t_end = time.monotonic() + 3
    while udp.stats["udp"] + udp.stats["bad"] < 3 and time.monotonic() < t_end:
        app.processEvents()
        time.sleep(.01)
    assert udp.stats == {"udp": 1, "bad": 2} and m2.level == 2, udp.stats

    win = HudWindow({"video": "", "hfov": 66.0, "warn_m": 20.0, "watch_m": 40.0}, QSettings("SP_DEWS", "selftest"),
                    m2, udp, Bridge())
    win.voice.enabled = win.cues.enabled = False
    win.resize(1366, 768)
    win.show()
    for _ in range(5):
        app.processEvents()
    win.model.on_alert(alert(2, 3, 3, 12.0))
    win._refresh()
    img = win.grab()
    assert not img.isNull() and img.width() > 1000
    m2.on_alert(alert(3, 3, 0, 0.0, 0.0))               # 표적 소실 → 생존 신호 → 15초 조용 → 끊김 화면
    m2.on_alert(alert(4, 0, 0, 0.0, 0.0))
    m2.tick(time.monotonic() + 16)
    win._refresh()
    assert m2.link_down and "신호 끊김" in win.node_line.text() and not win.grab().isNull()
    win.set_demo(True)
    app.processEvents()
    win.set_demo(False)                                 # 데모 끝 → 가짜 기록 지움
    assert not m2.nodes and not m2.link_down and win.demo_stop is None
    win.close()
    print("gui.py 자체 시험 통과: 경보 처리·전달률·소실·정리, 생존 신호·신호 끊김·복구, MJPEG 분리, "
          "UDP 수신(깨진 것 2건 거름), 화면 그리기, 데모 켜고 끄기")


# ---------------------------------------------------------------- main
def main() -> None:
    if sys.stdout is None:                        # exe(창 모드)는 콘솔이 없음
        sys.stdout = sys.stderr = open(os.devnull, "w")
    ap = argparse.ArgumentParser(description="지휘소 경보 표시 앱")
    ap.add_argument("--udp-port", type=int, default=UDP_PORT, help="alert_rx.py --gui-port와 같게")
    ap.add_argument("--video", default=None, help=f"관측소 Pi 영상 주소 (기본 {VIDEO_URL}, 끄려면 none)")
    ap.add_argument("--hfov", type=float, default=66.0, help="카메라 수평 화각(도)")
    ap.add_argument("--warn-m", type=float, default=20.0, help="스코프 경고선 (detect_stereo --warn-m)")
    ap.add_argument("--watch-m", type=float, default=40.0, help="스코프 감시선 (detect_stereo --watch-m)")
    ap.add_argument("--lora", metavar="PORT", help="alert_rx까지 같이 실행 (예: COM4, /dev/ttyUSB0)")
    ap.add_argument("--lora-gpio", action="store_true", help="--lora를 Pi에서 GPIO로 M0/M1 제어할 때")
    ap.add_argument("--demo", action="store_true", help="LoRa 없이 가짜 경보")
    ap.add_argument("--size", help="창 크기 고정 (예: 1600x900). 없으면 최대화")
    ap.add_argument("--shot", metavar="PNG", help="--shot-at 초 뒤 화면을 PNG로 저장하고 종료 (보고서용)")
    ap.add_argument("--shot-at", type=float, default=20.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return

    app = make_app(sys.argv[:1])
    qs = QSettings("SP_DEWS", "CommandHUD")
    video = args.video if args.video is not None else qs.value("video", VIDEO_URL)
    cfg = {"video": "" if str(video).lower() == "none" else str(video), "hfov": args.hfov,
           "warn_m": args.warn_m, "watch_m": args.watch_m}
    model, bridge = Model(), Bridge()
    udp = UdpInput(args.udp_port, model)
    if not udp.ok:
        QMessageBox.critical(None, "SP_DEWS 지휘소",
                             f"UDP {args.udp_port} 포트를 다른 프로그램이 쓰는 중입니다.\n이 앱이 이미 켜져 있는지 확인하세요.")
        sys.exit(1)
    win = HudWindow(cfg, qs, model, udp, bridge, args.lora_gpio)
    lora = args.lora or qs.value("lora_port", "")
    if lora:
        win.set_lora(lora)
    if args.demo:
        win.set_demo(True)
    if args.size:
        w, h = (int(x) for x in args.size.lower().split("x"))
        win.resize(w, h)
        win.show()
    else:
        geo = qs.value("geometry")
        if geo is not None:
            win.restoreGeometry(geo)
        win.showMaximized()
    _dark_titlebar(win)
    if args.shot:
        QTimer.singleShot(int(args.shot_at * 1000), lambda: (win.grab().save(args.shot), app.quit()))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
