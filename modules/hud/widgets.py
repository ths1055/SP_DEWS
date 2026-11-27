"""
hud/widgets.py — 지휘소 HUD 화면 조각 (전부 QPainter로 직접 그림)

색은 위협 등급에만 씀: 관심(연하늘)·경계(주황)·위험(빨강). 나머지는 파란 홀로그램 톤.
글꼴: 숫자·영문 Michroma, 한글 Orbit (hud/fonts, SIL OFL). 못 읽으면 시스템 글꼴로 대체.
움직이는 건 스코프·다이얼·음성 원·경계 테두리뿐이고, 패널 바탕은 크기 바뀔 때만 다시 그려서 캐시.
"""
from __future__ import annotations

import math
import random
import time
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QRadialGradient, QRegion)
from PySide6.QtWidgets import (QAbstractItemView, QListWidget, QListWidgetItem, QSizePolicy,
                               QStyledItemDelegate, QWidget)

from .model import LEVEL_NAME, STALE_S, az_text, speed_short

# ---------------------------------------------------------------- 색·글꼴
BLUE = (58, 168, 255)
EDGE = (143, 227, 255)
CORE = (232, 250, 255)
TEAL = (60, 245, 180)
AMBER = (255, 181, 71)
RED = (255, 77, 94)
DIM = (104, 152, 190)
LEVEL_RGB = {0: EDGE, 1: (190, 238, 255), 2: AMBER, 3: RED}
RANGES = (50, 60, 80, 100, 150, 200, 300)
NUM, UI = "Michroma", "Orbit"
SCALE = 1.0          # 화면 크기에 맞춘 글자 배율 (gui.py가 정함)


def load_fonts(folder: Path) -> list:
    global NUM, UI
    got = []
    for f in ("Michroma-Regular.ttf", "Orbit-Regular.ttf"):
        fid = QFontDatabase.addApplicationFont(str(folder / f))
        if fid >= 0:
            got += QFontDatabase.applicationFontFamilies(fid)
    if "Michroma" not in got:
        NUM = "Bahnschrift"
    if "Orbit" not in got:
        UI = "Malgun Gothic"
    return got


def font(family: str, px: float) -> QFont:
    f = QFont(family)
    f.setPixelSize(max(7, int(round(px))))
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def rgba(rgb, a: float = 1.0) -> QColor:
    return QColor(rgb[0], rgb[1], rgb[2], max(0, min(255, int(a * 255))))


def mkpen(rgb, a: float, w: float, dash=None, cap=Qt.PenCapStyle.RoundCap) -> QPen:
    p = QPen(rgba(rgb, a))
    p.setWidthF(w)
    p.setCapStyle(cap)
    if dash:
        p.setDashPattern(dash)
    return p


def arc(cx, cy, r, start, span) -> QPainterPath:
    """Qt 각도(도, 3시 = 0, 반시계 +)로 호 경로"""
    rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
    path = QPainterPath()
    path.arcMoveTo(rect, start)
    path.arcTo(rect, start, span)
    return path


def glow(p: QPainter, path: QPainterPath, rgb, w: float, a: float = 1.0, cap=Qt.PenCapStyle.RoundCap) -> None:
    """빛 번지는 선: 굵고 흐린 선 두 번 + 가는 선"""
    p.strokePath(path, mkpen(rgb, a * .10, w * 5.5, cap=cap))
    p.strokePath(path, mkpen(rgb, a * .22, w * 2.4, cap=cap))
    p.strokePath(path, mkpen(rgb, a, w, cap=cap))


def text_w(fnt: QFont, s: str) -> float:
    return QFontMetricsF(fnt).horizontalAdvance(s)


def gtext(p: QPainter, x, y, s: str, fnt: QFont, rgb, a=1.0, halo=True, align="left") -> float:
    """빛나는 글자 (y = 글자 바닥선)"""
    w = text_w(fnt, s)
    x = x - w / 2 if align == "center" else x - w if align == "right" else x
    path = QPainterPath()
    path.addText(QPointF(x, y), fnt, s)
    if halo:
        p.strokePath(path, mkpen(rgb, a * .13, 6))
        p.strokePath(path, mkpen(rgb, a * .24, 2.4))
    p.fillPath(path, rgba(rgb, a))
    return w


def rounded_poly(pts, rad: float) -> QPainterPath:
    path = QPainterPath()
    n = len(pts)
    for i in range(n):
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]
        v1, v2 = p0 - p1, p2 - p1
        l1, l2 = math.hypot(v1.x(), v1.y()), math.hypot(v2.x(), v2.y())
        a = p1 + v1 * (min(rad, l1 / 2) / l1)
        b = p1 + v2 * (min(rad, l2 / 2) / l2)
        path.moveTo(a) if i == 0 else path.lineTo(a)
        path.quadTo(p1, b)
    path.closeSubpath()
    return path


def painter(w: QWidget) -> QPainter:
    p = QPainter(w)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    return p


def _cache_pixmap(w: QWidget) -> tuple:
    dpr = w.devicePixelRatioF()
    pm = QPixmap(max(1, int(w.width() * dpr)), max(1, int(w.height() * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    return pm, painter_on(pm)


def painter_on(pm) -> QPainter:
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    return p


# ---------------------------------------------------------------- 창 바탕
class Backdrop(QWidget):
    """깊은 남색 + 푸른 안개 + 흐린 빛망울 + 아주 옅은 격자 (크기 바뀔 때만 그림)"""

    def __init__(self):
        super().__init__()
        self._pm = None
        self.overlay = None

    def resizeEvent(self, e):
        self._pm = None
        if self.overlay is not None:
            self.overlay.setGeometry(self.rect())

    def paintEvent(self, e):
        if self._pm is None:
            self._pm = self._render()
        QPainter(self).drawPixmap(0, 0, self._pm)

    def _render(self) -> QPixmap:
        w, h = self.width(), self.height()
        pm, p = _cache_pixmap(self)
        p.fillRect(QRectF(0, 0, w, h), QColor(2, 8, 20))
        for fx, fy, fr, a in ((.5, .55, .62, .17), (.06, .92, .36, .11), (.96, .06, .32, .09), (.78, .78, .3, .06)):
            g = QRadialGradient(w * fx, h * fy, max(w, h) * fr)
            g.setColorAt(0, rgba(BLUE, a))
            g.setColorAt(1, rgba(BLUE, 0))
            p.fillRect(QRectF(0, 0, w, h), g)
        rnd = random.Random(3)
        p.setPen(Qt.PenStyle.NoPen)
        for _ in range(30):                              # 빛망울 (안 움직임)
            x, y, r = rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(5, 40)
            g = QRadialGradient(x, y, r)
            g.setColorAt(0, rgba(EDGE if rnd.random() < .3 else BLUE, rnd.uniform(.03, .09)))
            g.setColorAt(1, rgba(BLUE, 0))
            p.setBrush(g)
            p.drawEllipse(QPointF(x, y), r, r)
        p.setPen(mkpen(EDGE, .022, 1))
        for x in range(0, w, 46):
            p.drawLine(x, 0, x, h)
        for y in range(0, h, 46):
            p.drawLine(0, y, w, y)
        p.end()
        return pm


class EdgeOverlay(QWidget):
    """경계·위험 때 화면 가장자리가 등급 색으로 번짐 (위험이면 맥박처럼). 마우스는 통과"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.level = 0

    def set_level(self, level: int) -> None:
        self.level = level
        self.update()

    def tick(self) -> None:
        if self.level >= 3:                              # 테두리 띠만 다시 그림 (안쪽 위젯은 안 건드리게)
            r, t = self.rect(), 90
            reg = QRegion(r).subtracted(QRegion(r.adjusted(t, t, -t, -t)))
            self.update(reg)

    def paintEvent(self, e):
        if self.level < 2:
            return
        col = LEVEL_RGB[self.level]
        pulse = .5 + .5 * math.sin(time.monotonic() * 2 * math.pi) if self.level == 3 else .35
        p = painter(self)
        w, h, t = self.width(), self.height(), 70 + 30 * pulse
        a = .16 + .2 * pulse
        for (x0, y0, x1, y1, rect) in ((0, 0, 0, t, QRectF(0, 0, w, t)), (0, h, 0, h - t, QRectF(0, h - t, w, t)),
                                       (0, 0, t, 0, QRectF(0, 0, t, h)), (w, 0, w - t, 0, QRectF(w - t, 0, t, h))):
            g = QLinearGradient(x0, y0, x1, y1)
            g.setColorAt(0, rgba(col, a))
            g.setColorAt(1, rgba(col, 0))
            p.fillRect(rect, g)
        p.setPen(mkpen(col, .45 + .45 * pulse, 2))
        p.drawRect(QRectF(1, 1, w - 2, h - 2))


# ---------------------------------------------------------------- 유리판
class GlassPanel(QWidget):
    """반투명 유리판. tilt -1/+1이면 화면 가운데 쪽 변이 살짝 짧아서 안으로 꺾인 것처럼 보임"""

    def __init__(self, tilt: int = 0):
        super().__init__()
        self.tilt, self.accent, self._pm = tilt, EDGE, None

    def set_accent(self, rgb) -> None:
        if rgb != self.accent:
            self.accent, self._pm = rgb, None
            self.update()

    def resizeEvent(self, e):
        self._pm = None

    def paintEvent(self, e):
        if self._pm is None:
            self._pm = self._render()
        QPainter(self).drawPixmap(0, 0, self._pm)

    def _render(self) -> QPixmap:
        pm, p = _cache_pixmap(self)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        k = r.height() * .03 if self.tilt else 0
        tl, tr, br, bl = r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()
        if self.tilt < 0:
            tr, br = QPointF(r.right(), r.top() + k), QPointF(r.right(), r.bottom() - k)
        elif self.tilt > 0:
            tl, bl = QPointF(r.left(), r.top() + k), QPointF(r.left(), r.bottom() - k)
        path = rounded_poly([tl, tr, br, bl], 18)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, rgba(BLUE, .17))
        g.setColorAt(.5, rgba(BLUE, .05))
        g.setColorAt(1, rgba(BLUE, .11))
        p.fillPath(path, g)
        p.save()
        p.setClipPath(path)
        for x in range(int(r.left()), int(r.right()), 22):          # 안쪽 격자, 왼쪽 위에서 옅어짐
            p.setPen(mkpen(EDGE, .055 * max(0.0, 1 - (x - r.left()) / (r.width() * .85)), 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        for y in range(int(r.top()), int(r.bottom()), 22):
            p.setPen(mkpen(EDGE, .055 * max(0.0, 1 - (y - r.top()) / (r.height() * .85)), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        sheen = QLinearGradient(0, r.bottom() - 80, 0, r.bottom())
        sheen.setColorAt(0, rgba(BLUE, 0))
        sheen.setColorAt(1, rgba(BLUE, .09))
        p.fillRect(r, sheen)
        p.restore()
        p.strokePath(path, mkpen(self.accent, .09, 7))
        p.strokePath(path, mkpen(self.accent, .45, 1.2))
        c = QPainterPath()                                          # 왼쪽 위 모서리 강조
        c.moveTo(tl.x() + 1, tl.y() + 54)
        c.lineTo(tl.x() + 1, tl.y() + 18)
        c.quadTo(tl.x() + 1, tl.y() + 1, tl.x() + 18, tl.y() + 1)
        c.lineTo(tl.x() + 80, tl.y() + 1 + (tr.y() - tl.y()) * 80 / max(1, tr.x() - tl.x()))
        p.strokePath(c, mkpen(CORE, .22, 5))
        p.strokePath(c, mkpen(CORE, .85, 1.6))
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(3):                                         # 오른쪽 아래 데이터 점
            p.setBrush(rgba(TEAL, .85 - i * .28))
            p.drawEllipse(QPointF(br.x() - 20 - i * 9, br.y() - 13), 2.2, 2.2)
        p.end()
        return pm


class SectionTitle(QWidget):
    def __init__(self, text: str, aside: str = ""):
        super().__init__()
        self.text, self.aside, self.aside_rgb = text, aside, DIM
        self.setFixedHeight(int(26 * SCALE))

    def set_aside(self, text: str, rgb=DIM) -> None:
        if (text, rgb) != (self.aside, self.aside_rgb):
            self.aside, self.aside_rgb = text, rgb
            self.update()

    def paintEvent(self, e):
        p = painter(self)
        h = self.height()
        g = QRadialGradient(6, h / 2, 7)
        g.setColorAt(0, rgba(TEAL, .9))
        g.setColorAt(1, rgba(TEAL, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(6, h / 2), 7, 7)
        p.setBrush(rgba(TEAL))
        p.drawEllipse(QPointF(6, h / 2), 2.4, 2.4)
        f = font(UI, 13.5 * SCALE)
        gtext(p, 18, h / 2 + 5 * SCALE, self.text, f, DIM, halo=False)
        if self.aside:
            gtext(p, self.width() - 2, h / 2 + 5 * SCALE, self.aside, font(UI, 12.5 * SCALE), self.aside_rgb,
                  halo=False, align="right")


class Chip(QWidget):
    """둥근 캡슐: 왼쪽 이름, 오른쪽 값"""

    def __init__(self, label: str):
        super().__init__()
        self.label, self.value, self.rgb = label, "--", CORE
        self.setFixedHeight(int(32 * SCALE))

    def set(self, value: str, rgb=CORE) -> None:
        if (value, rgb) != (self.value, self.rgb):
            self.value, self.rgb = value, rgb
            self.update()

    def paintEvent(self, e):
        p = painter(self)
        r = QRectF(self.rect()).adjusted(1, 3, -1, -3)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.fillPath(path, rgba(BLUE, .10))
        p.strokePath(path, mkpen(EDGE, .26, 1))
        y = r.center().y() + 5 * SCALE
        gtext(p, r.left() + 16, y, self.label, font(UI, 12.5 * SCALE), DIM, halo=False)
        gtext(p, r.right() - 16, y, self.value, font(UI, 15 * SCALE), self.rgb, halo=self.rgb != CORE, align="right")


# ---------------------------------------------------------------- 주 표적 다이얼
class ThreatDial(QWidget):
    """가운데 거리, 바깥 호 = 얼마나 가까운지(60 m 기준), 조각 링 = 도달 예상(10초 = 한 바퀴)"""
    FULL_M = 60.0

    def __init__(self, model, cfg):
        super().__init__()
        self.model, self.cfg = model, cfg
        self.t0 = time.monotonic()
        self.setMinimumSize(int(150 * SCALE), int(150 * SCALE))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def _ang(self, d: float) -> float:                  # 거리 → 다이얼 각도 (멀면 225°, 가까우면 -45°)
        return 225 - 270 * (1 - min(d, self.FULL_M) / self.FULL_M)

    def paintEvent(self, e):
        p = painter(self)
        now = time.monotonic()
        w, h = self.width(), self.height()
        R = min(w, h) / 2 - 8
        cx, cy = w / 2, h / 2
        rev = min(1.0, (now - self.t0) / 1.4)
        tr = self.model.primary()
        col = LEVEL_RGB[tr.level] if tr else EDGE

        g = QRadialGradient(cx, cy, R)
        g.setColorAt(0, rgba(BLUE, .17))
        g.setColorAt(.75, rgba(BLUE, .05))
        g.setColorAt(1, rgba(BLUE, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), R, R)

        rot = now * 4                                     # 바깥 눈금 링 (아주 천천히 돎)
        for i in range(int(120 * rev)):
            a = math.radians(i * 3 + rot)
            long_ = i % 10 == 0
            r1 = R - (9 if long_ else 4)
            p.setPen(mkpen(EDGE, .55 if long_ else .2, 1.2 if long_ else 1))
            p.drawLine(QPointF(cx + R * math.cos(a), cy - R * math.sin(a)),
                       QPointF(cx + r1 * math.cos(a), cy - r1 * math.sin(a)))

        rt = R - 21
        flat = Qt.PenCapStyle.FlatCap
        p.strokePath(arc(cx, cy, rt, 225, -270 * rev), mkpen(BLUE, .17, 10, cap=flat))
        for d, c in ((self.cfg["watch_m"], EDGE), (self.cfg["warn_m"], AMBER)):
            a = math.radians(self._ang(d))
            p.setPen(mkpen(c, .9, 2, cap=flat))
            p.drawLine(QPointF(cx + (rt - 8) * math.cos(a), cy - (rt - 8) * math.sin(a)),
                       QPointF(cx + (rt + 8) * math.cos(a), cy - (rt + 8) * math.sin(a)))
        if tr:
            span = self._ang(tr.dist) - 225
            glow(p, arc(cx, cy, rt, 225, min(-2.0, span)), col, 8, cap=flat)
            a = math.radians(self._ang(tr.dist))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(rgba(CORE))
            p.drawEllipse(QPointF(cx + rt * math.cos(a), cy - rt * math.sin(a)), 3.5, 3.5)
        else:                                             # 대기: 빛 조각이 트랙을 따라 돎
            st = 225 - (now * 70) % 270
            glow(p, arc(cx, cy, rt, st, -min(36, st + 45)), EDGE, 4, .45, cap=flat)

        rs, n = R - 40, 24                                # 도달 예상 조각 링
        lit = 0
        tt = tr.tta_now(now) if tr else None
        if tt is not None:
            lit = math.ceil(min(1.0, tt / 10) * n)
        for i in range(int(n * rev)):
            on = i < lit
            p.strokePath(arc(cx, cy, rs, 225 - i * 11.25 - 1.6, -8.0),
                         mkpen(col if on else BLUE, .95 if on else .14, 5, cap=flat))

        pen = mkpen(EDGE, .3, 1, dash=[2, 6])
        pen.setDashOffset(-now * 6)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), R - 52, R - 52)

        if tr:
            gtext(p, cx, cy - R * .27, LEVEL_NAME[tr.level], font(UI, R * .15), col, align="center")
            s = f"{tr.dist:.1f}"
            fn = font(NUM, R * .31)
            while text_w(fn, s) > (R - 52) * 1.55 and fn.pixelSize() > 10:
                fn.setPixelSize(fn.pixelSize() - 2)
            fu = font(NUM, R * .1)
            wn, wu = text_w(fn, s), text_w(fu, "m")
            x0 = cx - (wn + wu + 6) / 2
            gtext(p, x0, cy + R * .13, s, fn, col)
            gtext(p, x0 + wn + 6, cy + R * .13, "m", fu, DIM, halo=False)
            gtext(p, cx, cy + R * .34, f"#{tr.id}   관측소 {tr.node}", font(UI, R * .085), DIM, halo=False,
                  align="center")
            if tt is not None:
                gtext(p, cx, cy + R * .86, f"T-{tt:4.1f}s" if tt > 0 else "도달", font(NUM, R * .085), col,
                      align="center")
        elif self.model.link_down:                       # 관측소가 조용함 → "없음"이 아니라 "모름"
            gtext(p, cx, cy + R * .05, "신호 끊김", font(UI, R * .15), AMBER, align="center")
            gtext(p, cx, cy + R * .25, "관측소 확인", font(UI, R * .085), DIM, halo=False, align="center")
        else:
            gtext(p, cx, cy + R * .05, "표적 없음", font(UI, R * .15), EDGE, align="center")
            gtext(p, cx, cy + R * .25, "경보 대기", font(UI, R * .085), DIM, halo=False, align="center")


class DirGauge(QWidget):
    """방위(반원, 좌60~우60 밝게 + 카메라 시야) / 고각(사분원)"""

    def __init__(self, model, cfg):
        super().__init__()
        self.model, self.cfg = model, cfg
        self.setFixedHeight(int(100 * SCALE))

    def paintEvent(self, e):
        p = painter(self)
        w, h = self.width(), self.height()
        tr = self.model.primary()
        col = LEVEL_RGB[tr.level] if tr else EDGE
        r = min(w * .23, h * .62)
        flat = Qt.PenCapStyle.FlatCap
        # 방위
        cx, cy = w * .30, h * .70
        p.strokePath(arc(cx, cy, r, 0, 180), mkpen(BLUE, .15, 6, cap=flat))
        p.strokePath(arc(cx, cy, r, 30, 120), mkpen(EDGE, .35, 6, cap=flat))
        hf = min(self.cfg["hfov"] / 2, 90)
        wedge = QPainterPath(QPointF(cx, cy))
        wedge.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), 90 - hf, 2 * hf)
        wedge.closeSubpath()
        p.fillPath(wedge, rgba(BLUE, .12))
        for d in range(0, 181, 15):
            a = math.radians(d)
            L = 7 if d % 45 == 0 else 4
            p.setPen(mkpen(EDGE, .5, 1))
            p.drawLine(QPointF(cx + (r + 5) * math.cos(a), cy - (r + 5) * math.sin(a)),
                       QPointF(cx + (r + 5 + L) * math.cos(a), cy - (r + 5 + L) * math.sin(a)))
        if tr:
            a = math.radians(90 - max(-90.0, min(90.0, tr.az)))
            needle = QPainterPath(QPointF(cx, cy))
            needle.lineTo(cx + (r - 3) * math.cos(a), cy - (r - 3) * math.sin(a))
            glow(p, needle, col, 1.8)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(rgba(CORE))
            p.drawEllipse(QPointF(cx + (r - 3) * math.cos(a), cy - (r - 3) * math.sin(a)), 3, 3)
        p.setBrush(rgba(EDGE, .9))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), 2.5, 2.5)
        fy = h - 4
        gtext(p, cx - 6, fy, "방위", font(UI, 12 * SCALE), DIM, halo=False, align="right")
        gtext(p, cx + 6, fy, az_text(tr.az) if tr else "--", font(UI, 14 * SCALE), CORE, halo=False)
        # 고각
        cx2 = w * .70
        p.strokePath(arc(cx2, cy, r, 0, 90), mkpen(BLUE, .15, 6, cap=flat))
        p.strokePath(arc(cx2, cy, r, 0, 60), mkpen(EDGE, .3, 6, cap=flat))
        for d in range(0, 91, 15):
            a = math.radians(d)
            p.setPen(mkpen(EDGE, .5, 1))
            p.drawLine(QPointF(cx2 + (r + 5) * math.cos(a), cy - (r + 5) * math.sin(a)),
                       QPointF(cx2 + (r + 10) * math.cos(a), cy - (r + 10) * math.sin(a)))
        if tr:
            a = math.radians(max(0.0, min(90.0, tr.el)))
            needle = QPainterPath(QPointF(cx2, cy))
            needle.lineTo(cx2 + (r - 3) * math.cos(a), cy - (r - 3) * math.sin(a))
            glow(p, needle, col, 1.8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(rgba(EDGE, .9))
        p.drawEllipse(QPointF(cx2, cy), 2.5, 2.5)
        gtext(p, cx2 + r * .5 - 6, fy, "고각", font(UI, 12 * SCALE), DIM, halo=False, align="right")
        gtext(p, cx2 + r * .5 + 6, fy, f"{tr.el:.0f}°" if tr else "--", font(UI, 14 * SCALE), CORE, halo=False)


class TrackList(QWidget):
    """다른 표적 (최대 3개)"""

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.setFixedHeight(int(3 * 23 * SCALE + 4))

    def paintEvent(self, e):
        p = painter(self)
        rows = self.model.others()[:3]
        f, fb = font(UI, 13 * SCALE), font(NUM, 11.5 * SCALE)
        rh = 23 * SCALE
        if not rows:
            gtext(p, 2, rh * .7, "없음", f, DIM, halo=False)
            return
        w = self.width()
        for i, t in enumerate(rows):
            y = i * rh + rh * .72
            gtext(p, 2, y, self.model.tag(t), fb, CORE, halo=False)
            gtext(p, w * .50, y, f"{t.dist:.1f} m", fb, CORE, halo=False, align="right")
            gtext(p, w * .56, y, LEVEL_NAME[t.level], f, LEVEL_RGB[t.level], halo=False)
            gtext(p, w - 2, y, az_text(t.az), f, DIM, halo=False, align="right")


# ---------------------------------------------------------------- 위쪽 띠
class Brand(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(int(250 * SCALE), int(40 * SCALE))

    @staticmethod
    def draw_logo(p: QPainter, cx, cy, r) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        glow(p, arc(cx, cy, r, 0, 360), EDGE, 1.4, .9)
        for st in (20, 140, 260):
            p.strokePath(arc(cx, cy, r * .68, st, 80), mkpen(EDGE, .85, 2, cap=Qt.PenCapStyle.FlatCap))
        g = QRadialGradient(cx, cy, r * .45)
        g.setColorAt(0, rgba(CORE))
        g.setColorAt(1, rgba(BLUE, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), r * .45, r * .45)

    def paintEvent(self, e):
        p = painter(self)
        h = self.height()
        self.draw_logo(p, h / 2, h / 2, h * .36)
        x = h + 6
        x += gtext(p, x, h / 2 + 7 * SCALE, "SP_DEWS", font(NUM, 17 * SCALE), CORE) + 10
        gtext(p, x, h / 2 + 6 * SCALE, "지휘소", font(UI, 15 * SCALE), DIM, halo=False)


class StatusChip(QWidget):
    def __init__(self, model):
        super().__init__()
        self.model = model
        self.setFixedSize(int(270 * SCALE), int(36 * SCALE))

    def paintEvent(self, e):
        p = painter(self)
        lvl = self.model.level
        blind = lvl == 0 and self.model.link_down        # 표적이 없는 게 아니라 못 받는 중
        col = AMBER if blind else LEVEL_RGB[lvl]
        r = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        pulse = .5 + .5 * math.sin(time.monotonic() * 2 * math.pi) if lvl == 3 else 0
        p.fillPath(path, rgba(col, .10 + .16 * pulse if lvl or blind else .07))
        glow(p, path, col, 1.1, .8)
        n = len(self.model.tracks)
        s = f"{LEVEL_NAME[lvl]}   표적 {n}" if lvl else "관측소 신호 끊김" if blind else "표적 없음"
        f = font(UI, 15.5 * SCALE)
        tw = text_w(f, s)
        x = r.center().x() - (tw + 18) / 2
        g = QRadialGradient(x + 5, r.center().y(), 8)
        g.setColorAt(0, rgba(col))
        g.setColorAt(1, rgba(col, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(x + 5, r.center().y()), 8, 8)
        gtext(p, x + 18, r.center().y() + 5.5 * SCALE, s, f, col)


# ---------------------------------------------------------------- 전술 스코프
class ScopeWidget(QWidget):
    """관측소 기준 방위(좌우 60°)·거리 부채꼴. 표적마다 이름표, 제일 위험한 표적엔 조준 링 + 도달 예상 링"""

    def __init__(self, model, cfg, settings):
        super().__init__()
        self.model, self.cfg, self.settings = model, cfg, settings
        self.range_now = 50.0
        self.t0 = self.last = time.monotonic()
        rnd = random.Random(11)
        self.nodes = sorted(rnd.uniform(22, 158) for _ in range(9))
        self.segs = [(i * 22 + rnd.uniform(0, 8), rnd.uniform(5, 15), rnd.random() < .35) for i in range(17)]
        self.setMinimumSize(380, 320)

    def target_range(self) -> float:
        if self.settings.get("range"):
            return float(self.settings["range"])
        m = max((t.dist for t in self.model.tracks.values()), default=0.0)
        return float(next((r for r in RANGES if m * 1.08 <= r), RANGES[-1]))

    def rings(self, rng: float) -> list:
        step = 10 if rng <= 60 else 20 if rng <= 120 else 50
        out = {self.cfg["warn_m"], self.cfg["watch_m"], *range(step, int(rng), step)}
        return sorted(r for r in out if 0 < r < rng - .01)

    def paintEvent(self, e):
        p = painter(self)
        now = time.monotonic()
        dt, self.last = min(.1, now - self.last), now
        W, H = self.width(), self.height()
        pad, DEC = 22, 72                                 # 바깥 장식은 ±72°까지
        R = max(60.0, min((W / 2 - pad) / math.sin(math.radians(DEC)) - 46, H - 2 * pad - 150))
        ox, oy = W / 2, H / 2 + (R - 20) / 2
        rT = self.target_range()
        self.range_now += (rT - self.range_now) * (1 - math.exp(-dt / .45))
        rng = self.range_now
        rev = 1 - (1 - min(1.0, (now - self.t0) / 1.6)) ** 3      # 켜질 때 한 번 펼쳐짐
        lvl = self.model.level
        acc = LEVEL_RGB[lvl] if lvl >= 2 else EDGE
        half = 60 * rev
        flat = Qt.PenCapStyle.FlatCap

        p.save()                                         # 바닥에 깔리는 투영 빛
        p.translate(ox, oy)
        p.scale(1, .3)
        g = QRadialGradient(0, 0, R)
        g.setColorAt(0, rgba(BLUE, .30))
        g.setColorAt(1, rgba(BLUE, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(0, 0), R, R)
        p.restore()
        for i, (k, sp) in enumerate(((.16, 30), (.26, -20), (.36, 12))):   # 관측소 아래 투영 받침 (아래 반쪽 타원)
            er = QRectF(ox - R * k, oy + 4 - R * k * .32, 2 * R * k, 2 * R * k * .32)
            for j in range(4):
                p.setPen(mkpen(EDGE, .45 - i * .1, 1.2, cap=Qt.PenCapStyle.FlatCap))
                p.drawArc(er, int((190 + j * 42 + (now * sp) % 42) * 16), 30 * 16)

        r1, r2 = R + 16, R + 30                           # 바깥 장식: 눈금 반원 + 돌아가는 조각 띠 + 데이터 점
        for d in range(90 - DEC, 91 + DEC, 3):
            if abs(d - 90) > DEC * rev:
                continue
            a = math.radians(d)
            L = 7 if d % 15 == 0 else 3
            p.setPen(mkpen(EDGE, .45 if d % 15 == 0 else .2, 1))
            p.drawLine(QPointF(ox + r1 * math.cos(a), oy - r1 * math.sin(a)),
                       QPointF(ox + (r1 + L) * math.cos(a), oy - (r1 + L) * math.sin(a)))
        ph = (now * 3) % 374
        for st, ln, bright in self.segs:
            a0 = (st + ph) % 374 - 7
            if 90 - DEC <= a0 and a0 + ln <= 90 + DEC and abs(a0 + ln / 2 - 90) <= DEC * rev:
                p.strokePath(arc(ox, oy, r2, a0, ln), mkpen(EDGE if bright else BLUE, .7 if bright else .35, 3, cap=flat))
        p.setPen(Qt.PenStyle.NoPen)
        for d in self.nodes:
            if abs(d - 90) > DEC * rev:
                continue
            a = math.radians(d)
            q = QPointF(ox + r1 * math.cos(a), oy - r1 * math.sin(a))
            g = QRadialGradient(q, 6)
            g.setColorAt(0, rgba(TEAL, .8))
            g.setColorAt(1, rgba(TEAL, 0))
            p.setBrush(g)
            p.drawEllipse(q, 6, 6)

        rect = QRectF(ox - R, oy - R, 2 * R, 2 * R)
        fan = QPainterPath(QPointF(ox, oy))               # 부채꼴 유리
        fan.arcTo(rect, 90 - half, 2 * half)
        fan.closeSubpath()
        g = QRadialGradient(ox, oy, R)
        g.setColorAt(0, rgba(BLUE, .24))
        g.setColorAt(.6, rgba(BLUE, .10))
        g.setColorAt(1, rgba(BLUE, .05))
        p.fillPath(fan, g)

        wr = min(1.0, self.cfg["warn_m"] / rng) * R       # 경고선 안쪽
        z = QPainterPath(QPointF(ox, oy))
        z.arcTo(QRectF(ox - wr, oy - wr, 2 * wr, 2 * wr), 90 - half, 2 * half)
        z.closeSubpath()
        p.fillPath(z, rgba(AMBER, .09 if lvl >= 2 else .04))

        hf = min(self.cfg["hfov"] / 2, 60) * rev          # 카메라 시야
        fov = QPainterPath(QPointF(ox, oy))
        fov.arcTo(rect, 90 - hf, 2 * hf)
        fov.closeSubpath()
        p.fillPath(fov, rgba(EDGE, .06))
        for s in (-1, 1):
            a = math.radians(90 - s * hf)
            p.setPen(mkpen(EDGE, .3, 1, dash=[3, 5]))
            p.drawLine(QPointF(ox, oy), QPointF(ox + R * math.cos(a), oy - R * math.sin(a)))

        fr = font(UI, 12 * SCALE)
        for r in self.rings(rT):                          # 거리 링 (유리 띠 + 가는 선)
            if r > rng:
                continue
            rr = r / rng * R
            warn, watch = r == self.cfg["warn_m"], r == self.cfg["watch_m"]
            c = AMBER if warn else EDGE
            path = arc(ox, oy, rr, 90 - half, 2 * half)
            p.strokePath(path, mkpen(c, .07, 7, cap=flat))
            p.strokePath(path, mkpen(c, .85 if warn else .6 if watch else .28, 1.2 if warn or watch else 1,
                                     dash=[5, 4] if warn else [2, 5] if watch else None, cap=flat))
            if rev > .95:
                a = math.radians(30)
                label = f"{r:g} m" + (" 경고선" if warn else " 감시선" if watch else "")
                gtext(p, ox + rr * math.cos(a) + 9, oy - rr * math.sin(a) + 4, label, fr,
                      AMBER if warn else EDGE if watch else DIM, halo=warn or watch)

        rim = arc(ox, oy, R, 90 - half, 2 * half)         # 테두리
        p.strokePath(rim, mkpen(acc, .12, 9, cap=flat))
        glow(p, rim, acc, 1.6, .9)
        for s in (-1, 1):
            a = math.radians(90 - s * half)
            edge = QPainterPath(QPointF(ox, oy))
            edge.lineTo(ox + R * math.cos(a), oy - R * math.sin(a))
            glow(p, edge, acc, 1.2, .6)
        if rev > .95:
            for d in (-60, -30, 0, 30, 60):
                a = math.radians(90 - d)
                s = "정면" if d == 0 else f"{'좌' if d < 0 else '우'} {abs(d)}°"
                gtext(p, ox + (R + 52) * math.cos(a), oy - (R + 52) * math.sin(a) + 4, s, fr, DIM, halo=False,
                      align="center")

        sw = 60 * math.sin(now / 6 * 2 * math.pi)        # 훑는 빛
        dirn = 1 if math.cos(now / 6 * 2 * math.pi) >= 0 else -1
        if rev >= 1:
            for i in range(14):
                az = sw - dirn * i * .9
                if abs(az) > 60:
                    continue
                a = math.radians(90 - az)
                p.setPen(mkpen(EDGE, .2 * (1 - i / 14), 1.2))
                p.drawLine(QPointF(ox, oy), QPointF(ox + R * math.cos(a), oy - R * math.sin(a)))

        self._post(p, ox, oy, now, R)
        gtext(p, 16, 22, f"범위 {rT:g} m" + ("" if self.settings.get("range") else " (자동)"), fr, DIM, halo=False)
        p.fillRect(QRectF(16, 32, 11, 11), rgba(EDGE, .16))
        gtext(p, 33, 42, f"카메라 시야 {self.cfg['hfov']:g}°", fr, DIM, halo=False)
        if not self.model.tracks:                         # 비었을 때 안내 (부채꼴 위 빈 곳)
            links = self.model.links()
            ok = [str(nid) for nid, st, _ in links if st == "ok"]
            if self.model.link_down:
                msg = ("관측소 신호 끊김. 지금은 드론이 와도 표시되지 않습니다. "
                       "관측소 전원·LoRa 안테나·수신기 연결을 확인하세요.")
            elif ok:
                msg = f"드론 없음. 관측소 {', '.join(ok)} 연결 정상 (5초마다 생존 신호)."
            elif self.model.total:
                msg = "표적 없음. 관측소가 다시 경보를 보내면 바로 표시됩니다."
            else:
                msg = "받은 경보 없음. LoRa 수신(alert_rx.py 또는 위쪽 LoRa 버튼)이 켜져 있으면 오는 즉시 표시됩니다."
            top, bottom = 50, oy - R - 66
            box = QRectF(ox - R * .9, top, R * 1.8, bottom - top) if bottom - top > 34 else \
                QRectF(ox - R * .45, oy - R * .5, R * .9, R * .3)
            p.setPen(rgba(AMBER if self.model.link_down else DIM))
            p.setFont(font(UI, 13 * SCALE))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, msg)
        self._tracks(p, now, dt, ox, oy, R, rng, rev)

    def _post(self, p, ox, oy, now, R) -> None:
        """관측소: 빛나는 핵 + 철사 지구본"""
        g = QRadialGradient(ox, oy, 26)
        g.setColorAt(0, rgba(CORE, .9))
        g.setColorAt(.35, rgba(BLUE, .5))
        g.setColorAt(1, rgba(BLUE, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(ox, oy), 26, 26)
        r = 13
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(mkpen(EDGE, .95, 1.2))
        p.drawEllipse(QPointF(ox, oy), r, r)
        p.setPen(mkpen(EDGE, .55, 1))
        for k in range(3):
            rx = abs(r * math.cos(now * .8 + k * math.pi / 3))
            p.drawEllipse(QPointF(ox, oy), rx, r)
        p.drawEllipse(QPointF(ox, oy), r, r * .35)
        gtext(p, ox, oy + R * .13 + 22, "관측소", font(UI, 12 * SCALE), DIM, halo=False, align="center")

    def _tracks(self, p, now, dt, ox, oy, R, rng, rev) -> None:
        tracks = sorted(self.model.tracks.values(), key=lambda t: (t.level, -(t.tta or 1e9), -t.dist))
        prim = self.model.primary()
        boxes = []
        k = 1 - math.exp(-dt / .22)

        def pos(az, dist):
            r, a = min(dist / rng, 1.06) * R, math.radians(az)
            return QPointF(ox + r * math.sin(a), oy - r * math.cos(a))

        for t in tracks:
            tp = pos(t.az, t.dist)
            if t.disp is None:
                t.disp = (tp.x(), tp.y())
            x = t.disp[0] + (tp.x() - t.disp[0]) * k
            y = t.disp[1] + (tp.y() - t.disp[1]) * k
            t.disp = (x, y)
            stale = now - t.at > STALE_S
            col = LEVEL_RGB[t.level]
            p.save()
            p.setOpacity((.4 if stale else 1.0) * rev)
            if len(t.hist) > 1:                           # 지나온 길
                trail = QPainterPath(pos(*t.hist[0]))
                for h in t.hist[1:]:
                    trail.lineTo(pos(*h))
                trail.lineTo(x, y)
                p.strokePath(trail, mkpen(col, .4, 1.2, dash=[2, 3]))
            if t is prim and not stale:
                self._reticle(p, x, y, col, now, t)
            g = QRadialGradient(x, y, 15)                 # 빛나는 점 + 등급 모양 (색 말고도 구분)
            g.setColorAt(0, rgba(CORE))
            g.setColorAt(.3, rgba(col, .9))
            g.setColorAt(1, rgba(col, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(QPointF(x, y), 15, 15)
            shape = QPainterPath()
            if t.level == 3:
                shape.moveTo(x, y + 10)
                shape.lineTo(x - 9, y - 6)
                shape.lineTo(x + 9, y - 6)
                shape.closeSubpath()
            elif t.level == 2:
                shape.moveTo(x, y - 9)
                shape.lineTo(x + 9, y)
                shape.lineTo(x, y + 9)
                shape.lineTo(x - 9, y)
                shape.closeSubpath()
            else:
                shape.addEllipse(QPointF(x, y), 7, 7)
            p.strokePath(shape, mkpen(col, .95, 1.5))
            if stale:
                p.strokePath(arc(x, y, 14, 0, 360), mkpen(col, .9, 1, dash=[2, 3]))
            self._callout(p, t, x, y, col, t is prim, stale, boxes, ox, now)
            p.restore()

    def _reticle(self, p, x, y, col, now, t) -> None:
        flat = Qt.PenCapStyle.FlatCap
        spin = now * (160 if t.level == 3 else 55)
        for i in range(3):
            p.strokePath(arc(x, y, 21, spin + i * 120, 70), mkpen(col, .95, 1.6, cap=flat))
        for i in range(6):
            p.strokePath(arc(x, y, 27, -spin * .7 + i * 60, 18), mkpen(col, .55, 1.2, cap=flat))
        tt = t.tta_now(now)
        if tt is not None and tt < 20:                    # 도달 예상: 10초 = 한 바퀴
            p.strokePath(arc(x, y, 33, 0, 360), mkpen(col, .16, 3))
            glow(p, arc(x, y, 33, 90, -360 * min(1.0, tt / 10)), col, 2.2, cap=flat)
        if t.level == 3:
            ph = now % 1.0
            p.strokePath(arc(x, y, 12 + 34 * ph, 0, 360), mkpen(col, (1 - ph) * .7, 1.5))

    def _callout(self, p, t, x, y, col, prim, stale, boxes, ox, now) -> None:
        side = 1 if x >= ox else -1
        fl, fs = font(NUM, 11.5 * SCALE), font(UI, 12 * SCALE)
        lines = [(f"#{t.id}  {t.dist:.1f} m", fl, col),
                 ("갱신 없음" if stale else f"{LEVEL_NAME[t.level]}  {speed_short(t.speed)}", fs, CORE)]
        tt = t.tta_now(now)
        if prim and not stale and tt is not None:
            lines.append((f"도달 {tt:.1f} s" if tt > 0 else "도달", fs, col))
        lh = 17 * SCALE
        bw = max(text_w(f, s) for s, f, _ in lines) + 18
        bh = len(lines) * lh + 10
        off = 36 if prim else 20
        ey = y - off
        lx = x + side * (off + 22)
        bx = lx if side > 0 else lx - bw
        by = ey - bh / 2
        for _ in range(10):                               # 다른 이름표와 겹치면 아래로
            hit = next((b for b in boxes if bx < b[0] + b[2] and bx + bw > b[0] and by < b[1] + b[3] and by + bh > b[1]), None)
            if hit is None:
                break
            by = hit[1] + hit[3] + 6
        nbx = min(max(bx, 4), self.width() - bw - 4)
        nby = min(max(by, 50), self.height() - bh - 4)
        lx += nbx - bx
        bx, by = nbx, nby
        ey = by + bh / 2
        boxes.append((bx, by, bw, bh))
        leader = QPainterPath(QPointF(x + side * 9, y - 9))
        leader.lineTo(x + side * off, ey)
        leader.lineTo(lx, ey)
        p.strokePath(leader, mkpen(col, .8, 1))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(rgba(col))
        p.drawEllipse(QPointF(x + side * off, ey), 2.2, 2.2)
        box = QPainterPath()
        box.addRoundedRect(QRectF(bx, by, bw, bh), 8, 8)
        p.fillPath(box, QColor(4, 22, 46, 170))
        p.strokePath(box, mkpen(col, .65, 1))
        for i, (s, f, c) in enumerate(lines):
            gtext(p, bx + 9, by + 5 + lh * (i + .78), s, f, c, halo=i == 0)


# ---------------------------------------------------------------- 링크 링·음성 원
class RingGauge(QWidget):
    def __init__(self, caption: str):
        super().__init__()
        self.caption, self.frac, self.value, self.sub = caption, 0.0, "--", ""
        self.rgb, self.marker, self.dashed = EDGE, None, False
        self.setMinimumSize(int(110 * SCALE), int(100 * SCALE))

    def set(self, frac, value, sub="", rgb=EDGE, marker=None, dashed=False) -> None:
        self.frac, self.value, self.sub, self.rgb, self.marker, self.dashed = frac, value, sub, rgb, marker, dashed

    def paintEvent(self, e):
        p = painter(self)
        now = time.monotonic()
        w, h = self.width(), self.height()
        cap_h = 40 * SCALE
        R = min(w, h - cap_h) / 2 - 6
        cx, cy = w / 2, R + 6
        pen = mkpen(EDGE, .35, 1, dash=[1, 5])
        pen.setDashOffset(now * 5)
        p.setPen(pen)
        p.drawEllipse(QPointF(cx, cy), R, R)
        rr = R - 9
        flat = Qt.PenCapStyle.FlatCap
        p.strokePath(arc(cx, cy, rr, 0, 360), mkpen(BLUE, .16, 7))
        if self.frac > 0:
            path = arc(cx, cy, rr, 90, -360 * min(1.0, self.frac))
            if self.dashed:
                p.strokePath(path, mkpen(self.rgb, .8, 6, dash=[1, 1.4], cap=flat))
            else:
                glow(p, path, self.rgb, 6, cap=flat)
        if self.marker is not None:
            a = math.radians(90 - 360 * self.marker)
            p.setPen(mkpen(CORE, .95, 2))
            p.drawLine(QPointF(cx + (rr - 8) * math.cos(a), cy - (rr - 8) * math.sin(a)),
                       QPointF(cx + (rr + 8) * math.cos(a), cy - (rr + 8) * math.sin(a)))
        fv = font(NUM, R * .30)
        while text_w(fv, self.value) > rr * 1.5 and fv.pixelSize() > 9:
            fv.setPixelSize(fv.pixelSize() - 1)
        gtext(p, cx, cy + R * .12, self.value, fv, self.rgb, align="center")
        if self.sub:
            gtext(p, cx, h - 22 * SCALE, self.sub, font(UI, 11.5 * SCALE), CORE, halo=False, align="center")
        gtext(p, cx, h - 4, self.caption, font(UI, 12 * SCALE), DIM, halo=False, align="center")


class VoiceOrb(QWidget):
    """말할 때 물결치는 원"""

    def __init__(self):
        super().__init__()
        self.speaking = False
        self.setFixedSize(int(92 * SCALE), int(92 * SCALE))

    def paintEvent(self, e):
        p = painter(self)
        now = time.monotonic()
        w = self.width()
        c, R = QPointF(w / 2, w / 2), w / 2 - 6
        g = QRadialGradient(c, R)
        g.setColorAt(0, rgba(CORE, .85 if self.speaking else .5 + .15 * math.sin(now * 2)))
        g.setColorAt(.35, rgba(BLUE, .45))
        g.setColorAt(1, rgba(BLUE, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(c, R * .7, R * .7)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(mkpen(EDGE, .5, 1))
        p.drawEllipse(c, R, R)
        amp = R * .13 if self.speaking else R * .02
        for k, (a, ph) in enumerate(((1.0, 0.0), (.6, 1.7))):
            path = QPainterPath()
            for i in range(73):
                th = i / 72 * 2 * math.pi
                rr = R * .74 + amp * a * (math.sin(5 * th + now * 7 + ph) * .6 + math.sin(3 * th - now * 5) * .4)
                q = QPointF(c.x() + rr * math.cos(th), c.y() + rr * math.sin(th))
                path.moveTo(q) if i == 0 else path.lineTo(q)
            glow(p, path, EDGE if k == 0 else TEAL, 1.2, .9 if self.speaking else .45)


# ---------------------------------------------------------------- 영상
class VideoView(QWidget):
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.img = None
        self.online, self.has_base, self.recording, self.ended = False, False, False, False
        self.tag = "라이브"
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return int(w * 9 / 16)

    def sizeHint(self) -> QSize:
        return QSize(400, 225)

    def set_image(self, img: QImage) -> None:
        self.img = img
        self.update()

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = painter(self)
        now = time.monotonic()
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 12, 12)
        p.fillPath(path, QColor(0, 8, 18))
        p.save()
        p.setClipPath(path)
        showing = self.img is not None and (self.online or self.ended)
        if showing:
            s = self.img.size().scaled(r.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio)
            tgt = QRectF(r.center().x() - s.width() / 2, r.center().y() - s.height() / 2, s.width(), s.height())
            p.drawImage(tgt, self.img)
            p.setPen(mkpen(BLUE, .06, 1))
            for y in range(int(r.top()), int(r.bottom()), 3):    # 홀로그램 주사선
                p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        else:
            c = QPointF(r.center().x(), r.top() + r.height() * .36)
            for i, (rad, sp) in enumerate(((r.height() * .25, 40), (r.height() * .18, -70))):
                pen = mkpen(EDGE, .35, 1.4, dash=[4, 6])
                pen.setDashOffset(now * sp / 10)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, rad, rad)
            msg = ("위쪽 '영상 주소'에서 관측소 Pi 주소를 넣으면 켜집니다." if not self.has_base else
                   "노트북을 Pi의 Wi-Fi(SP_DEWS)에 연결하면 뜹니다. 경보는 LoRa로 계속 받습니다.")
            gtext(p, c.x(), c.y() + 6 * SCALE, "영상 링크 끊김" if self.has_base else "영상 꺼짐",
                  font(UI, 15 * SCALE), EDGE, align="center")
            p.setPen(rgba(DIM))
            p.setFont(font(UI, 12 * SCALE))
            p.drawText(QRectF(r.left() + 20, r.top() + r.height() * .66, r.width() - 40, r.height() * .32),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, msg)
        if self.ended:
            p.fillRect(r, QColor(0, 8, 18, 150))
            gtext(p, r.center().x(), r.center().y(), "클립 재생 끝", font(UI, 15 * SCALE), CORE, align="center")
            gtext(p, r.center().x(), r.center().y() + 24 * SCALE, "누르면 다시 재생", font(UI, 12 * SCALE), DIM,
                  halo=False, align="center")
        p.restore()
        L = 16                                            # 뷰파인더 모서리
        for (x, y, dx, dy) in ((r.left() + 9, r.top() + 9, 1, 1), (r.right() - 9, r.top() + 9, -1, 1),
                               (r.left() + 9, r.bottom() - 9, 1, -1), (r.right() - 9, r.bottom() - 9, -1, -1)):
            c = QPainterPath(QPointF(x + dx * L, y))
            c.lineTo(x, y)
            c.lineTo(x, y + dy * L)
            glow(p, c, EDGE, 1.2, .85)
        if showing or self.ended:
            f = font(UI, 12 * SCALE)
            tw = text_w(f, self.tag)
            tag = QPainterPath()
            tag.addRoundedRect(QRectF(r.left() + 16, r.top() + 14, tw + 18, 22 * SCALE), 10, 10)
            p.fillPath(tag, QColor(2, 14, 30, 190))
            gtext(p, r.left() + 25, r.top() + 14 + 15.5 * SCALE, self.tag, f, CORE, halo=False)
        if self.recording and self.online:
            on = int(now * 2) % 2 == 0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(rgba(RED, 1 if on else .3))
            p.drawEllipse(QPointF(r.right() - 82 * SCALE, r.top() + 25), 4.5, 4.5)
            gtext(p, r.right() - 20, r.top() + 30, "녹화 중", font(UI, 12 * SCALE), RED, halo=False, align="right")
        p.strokePath(path, mkpen(EDGE, .4, 1.2))


class ClipStrip(QWidget):
    """경고 클립 썸네일 (2열, 최대 4개). 누르면 재생"""
    clicked = Signal(dict)

    def __init__(self, video):
        super().__init__()
        self.video = video
        self.clips, self.fresh, self.current, self.online = [], set(), None, False
        self._hit = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_clips(self, clips: list) -> None:
        self.clips = clips
        self.updateGeometry()
        self.update()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        if not self.clips:
            return int(46 * SCALE)
        tw = (w - 10) / 2
        rows = 1 if len(self.clips) <= 2 else 2
        return int(rows * (tw * 9 / 16 + 40 * SCALE) + (rows - 1) * 8)

    def sizeHint(self) -> QSize:
        return QSize(300, self.heightForWidth(300))

    def mousePressEvent(self, e):
        for rect, clip in self._hit:
            if rect.contains(e.position()):
                self.clicked.emit(clip)
                return

    def paintEvent(self, e):
        p = painter(self)
        self._hit = []
        w = self.width()
        if not self.clips:
            p.setPen(rgba(DIM))
            p.setFont(font(UI, 12.5 * SCALE))
            p.drawText(QRectF(2, 0, w - 4, self.height()), Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                       "저장된 경고 클립 없음. 관측소가 경고를 띄우면 앞뒤 몇 초가 자동 저장됩니다." if self.online
                       else "영상 링크가 연결되면 경고 클립 목록이 보입니다.")
            return
        tw = (w - 10) / 2
        th = tw * 9 / 16
        for i, c in enumerate(self.clips[:4]):
            x = (i % 2) * (tw + 10)
            y = (i // 2) * (th + 40 * SCALE + 8)
            rect = QRectF(x + 1, y + 1, tw - 2, th)
            self._hit.append((QRectF(x, y, tw, th + 40 * SCALE), c))
            path = QPainterPath()
            path.addRoundedRect(rect, 8, 8)
            p.fillPath(path, QColor(0, 10, 22))
            img = self.video.thumb_for(c["id"])
            if img is not None:
                p.save()
                p.setClipPath(path)
                p.drawImage(rect, img)
                p.restore()
            col = CORE if self.current == c["id"] else AMBER if c["id"] in self.fresh else EDGE
            p.strokePath(path, mkpen(col, .9 if col != EDGE else .35, 1.3))
            tm = time.strftime("%H:%M:%S", time.localtime(c["t_start"]))
            gtext(p, x + 2, y + th + 17 * SCALE, tm, font(NUM, 11 * SCALE), CORE, halo=False)
            md = c.get("min_dist")
            gtext(p, x + 2, y + th + 34 * SCALE,
                  f"{c['duration']:.1f}초  최근접 {'--' if md is None else f'{md:.1f} m'}", font(UI, 11.5 * SCALE),
                  DIM, halo=False)


# ---------------------------------------------------------------- 기록
class _LogDelegate(QStyledItemDelegate):
    def paint(self, p, opt, idx):
        level, msg, epoch, label = idx.data(Qt.ItemDataRole.UserRole)
        r = QRectF(opt.rect)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = r.center().y() + 5 * SCALE
        gtext(p, r.left() + 4, y, time.strftime("%H:%M:%S", time.localtime(epoch)), font(NUM, 11 * SCALE), DIM,
              halo=False)
        lab = label or (LEVEL_NAME[level] if level > 0 else "해제" if level == 0 else "시스템")
        gtext(p, r.left() + 102 * SCALE, y, lab, font(UI, 12.5 * SCALE),
              LEVEL_RGB[level] if level > 0 else DIM, halo=level >= 2)
        fm = font(UI, 13.5 * SCALE)
        x = r.left() + 158 * SCALE
        msg = QFontMetricsF(fm).elidedText(msg, Qt.TextElideMode.ElideRight, max(20.0, r.right() - x - 6))
        gtext(p, x, y, msg, fm, CORE, halo=False)        # 긴 줄은 … 로 (창이 좁을 때)
        p.restore()

    def sizeHint(self, opt, idx):
        return QSize(opt.rect.width(), int(24 * SCALE))


class LogView(QListWidget):
    def __init__(self):
        super().__init__()
        self.setItemDelegate(_LogDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)   # 세로 막대가 생겨도 가로 막대 안 뜨게
        self.setStyleSheet(
            "QListWidget{background:transparent;border:none;}"
            "QScrollBar:vertical{background:transparent;width:6px;margin:2px;}"
            "QScrollBar::handle:vertical{background:rgba(143,227,255,70);border-radius:3px;min-height:24px;}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0;}"
            "QScrollBar::add-page,QScrollBar::sub-page{background:transparent;}")
        self.viewport().setAutoFillBackground(False)

    def add(self, level: int, msg: str, epoch: float, label: str = "") -> None:
        it = QListWidgetItem()
        it.setData(Qt.ItemDataRole.UserRole, (level, msg, epoch, label))
        self.insertItem(0, it)
        while self.count() > 150:
            self.takeItem(self.count() - 1)
