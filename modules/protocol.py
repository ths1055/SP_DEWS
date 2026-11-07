"""
protocol.py — 드론 조기경보 LoRa 패킷 정의 (⑥ 경보 생성 / ⑨ 수신 처리 공용)

경보 패킷 17 B
  magic(1) type(1) node(1) seq(1) track(1) level_mode(1)
  dist_cm(2) speed_cms(2) tta(1) az(1) el(1) ts_ms(2) crc16(2)

ACK 패킷 6 B
  magic(1) type(1) node(1) seq(1) crc16(2)

- 모든 정수는 big-endian
- level_mode: 하위 2비트 = 등급(0 없음, 1 관심, 2 경계, 3 위험), 비트7 = 1이면 stereo, 0이면 mono
- speed: 접근 속도, + 가 다가오는 방향, cm/s
- tta: 도달 예상 시간 0.1 s 단위, 255 = 해당 없음
- az/el: 0.5° 단위 (±63.5°까지)
- CRC: CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF), magic부터 crc 직전까지

하드웨어 없이 `python protocol.py` 로 자체 시험 가능.
"""
from __future__ import annotations

import binascii
import struct
import time
from dataclasses import dataclass
from typing import Optional, Union

# ---------------------------------------------------------------- 상수
MAGIC = 0xAA
TYPE_ALERT = 0
TYPE_ACK = 1

_ALERT_BODY = struct.Struct("!BBBBBBHhBbbH")   # 15 B
_ACK_BODY = struct.Struct("!BBBB")             # 4 B
_CRC = struct.Struct("!H")                     # 2 B

ALERT_SIZE = _ALERT_BODY.size + _CRC.size      # 17
ACK_SIZE = _ACK_BODY.size + _CRC.size          # 6
_SIZE_BY_TYPE = {TYPE_ALERT: ALERT_SIZE, TYPE_ACK: ACK_SIZE}

TTA_NONE = 255

LEVEL_NONE, LEVEL_INTEREST, LEVEL_CAUTION, LEVEL_DANGER = 0, 1, 2, 3
LEVEL_NAMES = {0: "없음", 1: "관심", 2: "경계", 3: "위험"}


# ---------------------------------------------------------------- 데이터 형태
@dataclass
class Alert:
    node_id: int
    seq: int
    track_id: int
    level: int
    stereo: bool
    dist_m: float
    speed_mps: float          # + = 다가옴
    tta_s: Optional[float]    # None = 해당 없음
    az_deg: float
    el_deg: float
    ts_ms: int = 0


@dataclass
class Ack:
    node_id: int
    seq: int


Packet = Union[Alert, Ack]


# ---------------------------------------------------------------- 보조 함수
def crc16(data: bytes) -> int:
    return binascii.crc_hqx(data, 0xFFFF)


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def now_ms16() -> int:
    """송신 시각(ms) 하위 16비트. 같은 기기 안에서 경과 시간 비교용."""
    return int(time.monotonic() * 1000) & 0xFFFF


class SeqCounter:
    """0~255 순환 시퀀스 번호."""

    def __init__(self, start: int = 0):
        self._n = start & 0xFF

    def next(self) -> int:
        s = self._n
        self._n = (self._n + 1) & 0xFF
        return s


# ---------------------------------------------------------------- 포장
def encode_alert(a: Alert) -> bytes:
    level_mode = (a.level & 0x03) | (0x80 if a.stereo else 0x00)
    tta = TTA_NONE if a.tta_s is None else _clamp(round(a.tta_s * 10), 0, 254)
    body = _ALERT_BODY.pack(
        MAGIC,
        TYPE_ALERT,
        a.node_id & 0xFF,
        a.seq & 0xFF,
        a.track_id & 0xFF,
        level_mode,
        _clamp(round(a.dist_m * 100), 0, 0xFFFF),
        _clamp(round(a.speed_mps * 100), -32768, 32767),
        tta,
        _clamp(round(a.az_deg * 2), -128, 127),
        _clamp(round(a.el_deg * 2), -128, 127),
        a.ts_ms & 0xFFFF,
    )
    return body + _CRC.pack(crc16(body))


def encode_ack(node_id: int, seq: int) -> bytes:
    body = _ACK_BODY.pack(MAGIC, TYPE_ACK, node_id & 0xFF, seq & 0xFF)
    return body + _CRC.pack(crc16(body))


# ---------------------------------------------------------------- 개봉
def decode(frame: bytes) -> Optional[Packet]:
    """패킷 하나(정확한 길이)를 풀어서 반환. 형식·CRC가 틀리면 None."""
    if len(frame) < ACK_SIZE or frame[0] != MAGIC:
        return None
    size = _SIZE_BY_TYPE.get(frame[1])
    if size is None or len(frame) != size:
        return None

    body = frame[:-2]
    (crc_rx,) = _CRC.unpack(frame[-2:])
    if crc16(body) != crc_rx:
        return None

    if frame[1] == TYPE_ALERT:
        (_, _, node, seq, track, lm, dist, spd, tta, az, el, ts) = _ALERT_BODY.unpack(body)
        return Alert(
            node_id=node,
            seq=seq,
            track_id=track,
            level=lm & 0x03,
            stereo=bool(lm & 0x80),
            dist_m=dist / 100,
            speed_mps=spd / 100,
            tta_s=None if tta == TTA_NONE else tta / 10,
            az_deg=az / 2,
            el_deg=el / 2,
            ts_ms=ts,
        )

    _, _, node, seq = _ACK_BODY.unpack(body)
    return Ack(node_id=node, seq=seq)


class PacketParser:
    """
    UART에서 들어오는 바이트 조각을 계속 넣으면 완성된 패킷만 꺼내준다.
    UART는 패킷 경계가 없어서 한 번에 반 개, 1.5개씩 들어올 수 있기 때문.
    """

    def __init__(self):
        self._buf = bytearray()
        self.crc_errors = 0

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[Packet]:
        self._buf += data
        out: list[Packet] = []
        while True:
            i = self._buf.find(MAGIC)
            if i < 0:                       # 시작 바이트 없음 → 전부 버림
                self._buf.clear()
                break
            if i > 0:                       # 앞쪽 쓰레기 바이트 제거
                del self._buf[:i]
            if len(self._buf) < 2:
                break
            size = _SIZE_BY_TYPE.get(self._buf[1])
            if size is None:                # 0xAA가 우연히 나온 것 → 한 칸 밀기
                del self._buf[0]
                continue
            if len(self._buf) < size:       # 아직 덜 들어옴
                break
            pkt = decode(bytes(self._buf[:size]))
            if pkt is None:                 # CRC 실패 → 한 칸 밀고 다시 찾기
                self.crc_errors += 1
                del self._buf[0]
            else:
                out.append(pkt)
                del self._buf[:size]
        return out


# ---------------------------------------------------------------- 자체 시험
if __name__ == "__main__":
    a = Alert(node_id=1, seq=42, track_id=7, level=LEVEL_DANGER, stereo=True,
              dist_m=18.37, speed_mps=5.2, tta_s=3.5, az_deg=-12.5, el_deg=8.0,
              ts_ms=now_ms16())
    f = encode_alert(a)
    assert len(f) == ALERT_SIZE == 17, len(f)
    b = decode(f)
    print("경보 패킷:", f.hex(" "))
    print("복원 결과:", b)
    assert b is not None and b.seq == 42 and b.level == 3 and b.stereo
    assert abs(b.dist_m - 18.37) < 0.01 and abs(b.speed_mps - 5.2) < 0.01

    k = encode_ack(1, 42)
    assert len(k) == ACK_SIZE == 6 and decode(k) == Ack(1, 42)

    # 1바이트 깨지면 거부되는지
    bad = bytearray(f)
    bad[7] ^= 0x01
    assert decode(bytes(bad)) is None

    # 쓰레기 + 쪼개진 패킷 + 깨진 패킷 + 정상 패킷이 섞인 스트림
    p = PacketParser()
    stream = b"\x00\x13garbage" + f + k + bytes(bad) + f
    got = []
    for j in range(0, len(stream), 5):          # 5바이트씩 나눠서 투입
        got += p.feed(stream[j:j + 5])
    assert [type(x).__name__ for x in got] == ["Alert", "Ack", "Alert"], got
    print(f"파서 시험 통과: 패킷 {len(got)}개, CRC 오류 {p.crc_errors}회")
    print("protocol.py 자체 시험 모두 통과")
