"""
hud/model.py — 경보 → 표적 상태 (화면과 상관없는 지휘소 GUI의 두뇌)

입력: alert_rx.py가 보내는 JSON 한 건 = protocol.Alert 필드 그대로
  node_id, seq, track_id, level(0 없음 1 관심 2 경계 3 위험), stereo, dist_m,
  speed_mps(+ = 다가옴), tta_s(None = 해당 없음), az_deg, el_deg, ts_ms
  등급 0 + track_id 0 = 관측소 생존 신호 (alert_gen.py가 표적 없을 때 5초마다)
출력(시그널): 기록 한 줄, 음성 문장, 경보음, 전체 등급 변화

관측소 상태 (links)
  ok   생존 신호를 보내는 관측소, 최근에 받음 → "드론 없음"이면 정말 없는 것
  down 생존 신호를 보내던 관측소가 15초 넘게 조용함 → LoRa·관측소 문제 (드론이 와도 못 받음)
  old  생존 신호를 안 보내는 관측소 (예전 송신 코드) → 마지막 수신 시각만 보여줌
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject, Signal

LEVEL_NAME = {0: "없음", 1: "관심", 2: "경계", 3: "위험"}
ALERT_KEYS = {"node_id", "seq", "track_id", "level", "stereo", "dist_m", "speed_mps",
              "tta_s", "az_deg", "el_deg"}
STALE_S = 4.0         # 이 시간 갱신이 없으면 흐리게 ("갱신 없음")
DROP_S = 8.0          # 이 시간 갱신이 없으면 화면에서 정리 (전송 정책: 같은 등급도 1~2초마다 갱신)
HEARTBEAT_TRACK = 0   # 등급 0 + 이 track_id = 생존 신호 (alert_gen.HEARTBEAT_TRACK)
LINK_LOST_S = 15.0    # 생존 신호(5초마다)가 3번 연달아 안 오면 '신호 끊김'


@dataclass
class Track:
    node: int
    id: int
    level: int = 0
    stereo: bool = True
    dist: float = 0.0
    speed: float = 0.0
    tta: Optional[float] = None
    az: float = 0.0
    el: float = 0.0
    at: float = 0.0                                   # 받은 시각 (monotonic)
    seq: int = 0
    hist: list = field(default_factory=list)          # 지나온 [(az, dist)]
    disp: Optional[tuple] = None                      # 스코프에서 부드럽게 옮길 때 쓰는 화면 위치

    def tta_now(self, now: float) -> Optional[float]:
        """받은 뒤 지난 시간만큼 빼서 실시간 카운트다운"""
        return None if self.tta is None else max(0.0, self.tta - (now - self.at))


# ---------------------------------------------------------------- 글자 만들기
def az_text(az: Optional[float]) -> str:
    if az is None:
        return "--"
    if abs(az) < 1.5:
        return "정면"
    return f"{'좌' if az < 0 else '우'} {abs(az):.0f}°"


def az_speech(az: float) -> str:
    if abs(az) < 1.5:
        return "정면"
    return f"{'좌측' if az < 0 else '우측'} {abs(az):.0f}도"


def speed_text(s: Optional[float]) -> str:
    if s is None:
        return "--"
    if s > 0.3:
        return f"{s:.1f} m/s 다가옴"
    if s < -0.3:
        return f"{-s:.1f} m/s 멀어짐"
    return "거의 정지"


def speed_short(s: float) -> str:
    if s > 0.3:
        return f"접근 {s:.1f} m/s"
    if s < -0.3:
        return f"이탈 {-s:.1f} m/s"
    return "정지"


def danger_key(t: Track):
    """위험한 순서: 등급 높은 것 → 도달 예상 짧은 것 → 가까운 것"""
    return (-t.level, 1e9 if t.tta is None else t.tta, t.dist)


class Model(QObject):
    log = Signal(int, str, float, str)        # 등급(-1 시스템), 내용, 시각(epoch), 꼬리표
    say = Signal(str, int)                    # 문장, 우선순위 (높으면 말하던 걸 끊고 말함)
    cue = Signal(int)                         # 경보음 (0 시스템, 1~3 등급)
    level_changed = Signal(int, int)          # 새 등급, 이전 등급

    def __init__(self):
        super().__init__()
        self.tracks: dict[tuple[int, int], Track] = {}
        self.nodes: dict[int, dict] = {}          # node_id → {count, lost, last_seq, at, hb, down}
        self.level = 0
        self.total = 0                            # 받은 경보 수 (생존 신호 빼고)
        self.last_at = 0.0                        # 마지막으로 뭐든 받은 시각
        self.link_down = False                    # 끊긴 관측소가 하나라도 있음
        self._beep = self._repeat = 0.0

    def reset(self) -> None:
        """데모 끝낼 때: 표적·관측소 기록을 말·기록 없이 비움"""
        prev = self.level
        self.tracks.clear()
        self.nodes.clear()
        self.level, self.total, self.last_at, self.link_down = 0, 0, 0.0, False
        if prev:
            self.level_changed.emit(0, prev)

    # ---- 경보 한 건
    def on_alert(self, a: dict, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self.last_at = now
        heartbeat = a["level"] == 0 and a["track_id"] == HEARTBEAT_TRACK
        self._node_stat(a, now, heartbeat)
        if heartbeat:                             # 생존 신호: 관측소 상태·전달률에만 씀
            return
        self.total += 1
        key = (a["node_id"], a["track_id"])
        tr = self.tracks.get(key)
        if a["level"] == 0:                       # 0 = 없음 → 표적 소실
            if tr is not None:
                del self.tracks[key]
                self.log.emit(0, f"{self.tag(tr)} 표적 소실", time.time(), "")
                if tr.level >= 2 and self.tracks:
                    self.say.emit(f"{LEVEL_NAME[tr.level]} 표적 소실. 남은 표적 {len(self.tracks)}.", 1)
            self._update_level()
            return
        new = tr is None
        if new:
            tr = self.tracks[key] = Track(node=a["node_id"], id=a["track_id"])
        prev = tr.level
        tr.level, tr.stereo = int(a["level"]), bool(a["stereo"])
        tr.dist, tr.speed, tr.tta = float(a["dist_m"]), float(a["speed_mps"]), a["tta_s"]
        tr.az, tr.el, tr.at, tr.seq = float(a["az_deg"]), float(a["el_deg"]), now, a["seq"]
        tr.hist.append((tr.az, tr.dist))
        del tr.hist[:-30]
        if new or prev != tr.level:
            what = "포착" if new else f"{LEVEL_NAME[prev]} → {LEVEL_NAME[tr.level]}"
            self.log.emit(tr.level, f"{self.tag(tr)} {what}   {tr.dist:.1f} m   {az_text(tr.az)}", time.time(), "")
            if new or tr.level > prev:            # 올라갈 때만 말함 (내려갈 땐 기록만)
                self.say.emit(self.phrase(tr, new, now), tr.level)
                self.cue.emit(tr.level)
                if tr.level == 3:
                    self._repeat = self._beep = now
        self._update_level()

    def _node_stat(self, a: dict, now: float, heartbeat: bool) -> None:
        nid = a["node_id"]
        n = self.nodes.get(nid)
        if n is None:
            n = self.nodes[nid] = {"count": 0, "lost": 0, "last_seq": None, "at": now, "hb": False, "down": False}
            self.log.emit(-1, f"관측소 {nid} 첫 수신" + (" (생존 신호)" if heartbeat else ""), time.time(), "")
            if heartbeat:                         # 경보면 바로 뒤에 경보 문장이 나오니까 생존 신호일 때만 말함
                self.say.emit(f"관측소 {nid} 연결.", 0)
        if n["down"]:
            n["down"] = False
            self.link_down = any(x["down"] for x in self.nodes.values())
            self.log.emit(-1, f"관측소 {nid} 신호 복구 ({now - n['at']:.0f}초 만에 수신)", time.time(), "")
            self.say.emit(f"관측소 {nid} 신호 복구.", 1)
            self.cue.emit(0)
        if n["last_seq"] is not None:
            gap = (a["seq"] - n["last_seq"]) % 256
            if 2 <= gap <= 32:                    # 그 사이 seq = 재전송까지 실패한 경보. 크게 튀면 송신 측 재시작
                n["lost"] += gap - 1
        n["last_seq"], n["at"] = a["seq"], now
        n["hb"] = n["hb"] or heartbeat
        n["count"] += 1

    def _update_level(self) -> None:
        lvl = max((t.level for t in self.tracks.values()), default=0)
        if lvl != self.level:
            prev, self.level = self.level, lvl
            if lvl == 0 and prev > 0:
                self.say.emit("표적 소실. 경계 해제.", 1)
            self.level_changed.emit(lvl, prev)

    # ---- 주기적으로 (화면 타이머가 부름)
    def tick(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        gone = [k for k, t in self.tracks.items() if now - t.at > DROP_S]
        for k in gone:
            tr = self.tracks.pop(k)
            self.log.emit(0, f"{self.tag(tr)} 갱신 {DROP_S:.0f}초 없음, 화면에서 정리", time.time(), "끊김")
            if tr.level >= 2:
                self.say.emit(f"{LEVEL_NAME[tr.level]} 표적 신호 끊김.", 1)
        if gone:
            self._update_level()
        for nid, n in self.nodes.items():         # 생존 신호를 보내던 관측소가 조용해짐 → 신호 끊김
            if n["hb"] and not n["down"] and now - n["at"] > LINK_LOST_S:
                n["down"] = self.link_down = True
                self.log.emit(-1, f"관측소 {nid} 신호 끊김: {LINK_LOST_S:.0f}초 넘게 생존 신호도 없음 "
                                  f"→ 관측소 전원·LoRa 안테나 확인", time.time(), "끊김")
                self.say.emit(f"관측소 {nid} 신호 끊김. 드론이 와도 경보를 받을 수 없습니다.", 2)
                self.cue.emit(2)
        if self.level == 3:                       # 위험이 이어지면 2초마다 경보음, 7초마다 거리
            if now - self._beep >= 2.0:
                self.cue.emit(3)
                self._beep = now
            if now - self._repeat >= 7.0:
                tr = self.primary()
                if tr is not None and tr.level == 3:
                    tt = tr.tta_now(now)
                    self.say.emit(f"{tr.dist:.0f}미터" + (f", {max(1, round(tt))}초." if tt else "."), 2)
                self._repeat = now

    # ---- 읽기
    def primary(self) -> Optional[Track]:
        return min(self.tracks.values(), key=danger_key, default=None)

    def others(self) -> list:
        p = self.primary()
        return sorted((t for t in self.tracks.values() if t is not p), key=danger_key)

    def links(self, now: Optional[float] = None) -> list:
        """[(관측소 번호, 'ok' | 'down' | 'old', 마지막 수신 뒤 초)] — 위 docstring 참고"""
        now = time.monotonic() if now is None else now
        return [(nid, "down" if n["down"] else "ok" if n["hb"] else "old", now - n["at"])
                for nid, n in sorted(self.nodes.items())]

    def delivery(self) -> tuple:
        """(받은 수, 빠진 수, 전달률 %) — seq 빠진 수로 계산 (생존 신호 포함)"""
        cnt = sum(n["count"] for n in self.nodes.values())
        lost = sum(n["lost"] for n in self.nodes.values())
        return cnt, lost, (cnt / (cnt + lost) * 100 if cnt else None)

    def tag(self, tr: Track) -> str:
        return (f"관측소{tr.node} " if len(self.nodes) > 1 else "") + f"#{tr.id}"

    def phrase(self, tr: Track, new: bool, now: float) -> str:
        d, where = f"{tr.dist:.0f}미터", az_speech(tr.az)
        if tr.level == 3:
            tt = tr.tta_now(now)
            return f"위험. 드론 {d}, {where}." + (f" {max(1, round(tt))}초 후 도달." if tt and tt < 30 else "")
        if tr.level == 2:
            return f"경계. 드론 접근 중. {d}, {where}."
        return f"{'표적 포착' if new else '관심 표적'}. {d}, {where}."
