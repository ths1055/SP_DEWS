"""
e22.py — EBYTE E22-900T22D (UART LoRa) 드라이버 (⑦ 관측소 / ⑧ 지휘소)

모드 (M0, M1)
  NORMAL (0,0) 송수신
  WOR    (1,0) 저전력 깨우기
  CONFIG (0,1) 레지스터 설정 (UART 9600 8N1 고정)
  SLEEP  (1,1) 절전

Pi 5        : use_gpio=True  → M0/M1/AUX를 GPIO로 제어
노트북(USB) : use_gpio=False → M0/M1은 점퍼로 고정 (송수신: 둘 다 GND, 설정: M1만 3.3V)

배선 (Pi 5 기준, 핀 번호는 BCM)
  E22 TXD → GPIO15(RXD)   E22 RXD → GPIO14(TXD)
  M0 → GPIO22   M1 → GPIO27   AUX → GPIO17   VCC → 5V   GND → GND
  ※ 안테나를 꼭 연결한 뒤 송신할 것 (안테나 없이 송신하면 모듈 손상 가능)

사용 예 (Pi)
  python e22.py --read            현재 설정 읽기
  python e22.py --write           기본 설정(아래 E22Config) 쓰기 — 두 모듈 모두 같은 설정이어야 통신됨
"""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

import serial

# ---------------------------------------------------------------- 모드
NORMAL, WOR, CONFIG, SLEEP = 0, 1, 2, 3
_MODE_PINS = {NORMAL: (0, 0), WOR: (1, 0), CONFIG: (0, 1), SLEEP: (1, 1)}
_MODE_NAMES = {NORMAL: "NORMAL", WOR: "WOR", CONFIG: "CONFIG", SLEEP: "SLEEP"}

MAX_PACKET = 240          # 서브패킷 크기 (REG1 = 00 → 240 B)

# ---------------------------------------------------------------- 레지스터 값
_AIR_RATE_CODE = {0.3: 0b000, 1.2: 0b001, 2.4: 0b010, 4.8: 0b011,
                  9.6: 0b100, 19.2: 0b101, 38.4: 0b110, 62.5: 0b111}
_AIR_RATE_FROM_CODE = {v: k for k, v in _AIR_RATE_CODE.items()}
_POWER_CODE = {22: 0b00, 17: 0b01, 13: 0b10, 10: 0b11}
_POWER_FROM_CODE = {v: k for k, v in _POWER_CODE.items()}
_UART_9600_8N1 = (0b011 << 5) | (0b00 << 3)


@dataclass
class E22Config:
    """
    송수신하는 두 모듈은 addr, net_id, channel, air_rate가 같아야 한다.
    channel: 주파수 = 850.125 + channel (MHz). 기본값 18(868 MHz)은 국내 대역이 아니므로
             국내 LoRa 대역(917~923.5 MHz) 안인 70(920.125 MHz)을 기본으로 둠.
    tx_power_dbm: 실내 시험은 가장 낮은 10 dBm. 출력 기준은 연구실·교수님께 확인 후 조정.
    """
    addr: int = 0x0000
    net_id: int = 0x00
    channel: int = 70
    air_rate: float = 2.4
    tx_power_dbm: int = 10

    @property
    def freq_mhz(self) -> float:
        return 850.125 + self.channel

    def to_regs(self) -> bytes:
        if self.air_rate not in _AIR_RATE_CODE:
            raise ValueError(f"air_rate는 {list(_AIR_RATE_CODE)} 중 하나")
        if self.tx_power_dbm not in _POWER_CODE:
            raise ValueError(f"tx_power_dbm은 {list(_POWER_CODE)} 중 하나")
        if not 0 <= self.channel <= 80:
            raise ValueError("channel은 0~80")
        reg0 = _UART_9600_8N1 | _AIR_RATE_CODE[self.air_rate]
        reg1 = (0b00 << 6) | _POWER_CODE[self.tx_power_dbm]   # 240 B, 주변 노이즈 RSSI 끔
        reg2 = self.channel
        reg3 = 0x03          # RSSI 바이트 끔, 투명 전송, 중계 끔, LBT 끔, WOR 2000 ms
        return bytes([(self.addr >> 8) & 0xFF, self.addr & 0xFF, self.net_id,
                      reg0, reg1, reg2, reg3])

    @classmethod
    def from_regs(cls, r: bytes) -> "E22Config":
        if (r[3] >> 5) != 0b011:
            print("⚠ 모듈 UART 속도가 9600이 아님 — 드라이버 baudrate와 맞출 것")
        if r[6] & 0x80:
            print("⚠ RSSI 바이트 출력이 켜져 있음 — 수신 패킷 뒤에 1바이트가 붙음")
        if r[6] & 0x40:
            print("⚠ 고정 지점 전송 모드 — 투명 전송이 아님")
        return cls(addr=(r[0] << 8) | r[1], net_id=r[2], channel=r[5],
                   air_rate=_AIR_RATE_FROM_CODE[r[3] & 0x07],
                   tx_power_dbm=_POWER_FROM_CODE[r[4] & 0x03])


# ---------------------------------------------------------------- 드라이버
class E22:
    def __init__(self, port: str = "/dev/serial0", use_gpio: bool = True,
                 m0_pin: int = 22, m1_pin: int = 27, aux_pin: int = 17,
                 baudrate: int = 9600, timeout: float = 0.05):
        self.ser = serial.Serial(port, baudrate, timeout=timeout)
        self.use_gpio = use_gpio
        self.mode = None
        if use_gpio:
            from gpiozero import DigitalInputDevice, DigitalOutputDevice
            self._m0 = DigitalOutputDevice(m0_pin)
            self._m1 = DigitalOutputDevice(m1_pin)
            self._aux = DigitalInputDevice(aux_pin)
        self.set_mode(NORMAL)

    # -- 수명 관리
    def close(self) -> None:
        self.ser.close()
        if self.use_gpio:
            for d in (self._m0, self._m1, self._aux):
                d.close()

    def __enter__(self) -> "E22":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- AUX / 모드
    def wait_aux(self, timeout: float = 1.0) -> bool:
        """AUX가 HIGH(모듈 준비 완료)가 될 때까지 대기. 시간 초과면 False."""
        if not self.use_gpio:
            time.sleep(0.01)
            return True
        t_end = time.monotonic() + timeout
        while not self._aux.value:
            if time.monotonic() > t_end:
                return False
            time.sleep(0.001)
        return True

    def set_mode(self, mode: int) -> None:
        if not self.use_gpio:         # 노트북: 점퍼로 이미 맞춰져 있다고 가정
            self.mode = mode
            return
        if self.mode == mode:
            return
        self.wait_aux()
        self._m0.value, self._m1.value = _MODE_PINS[mode]
        time.sleep(0.01)
        if not self.wait_aux():
            raise TimeoutError(f"{_MODE_NAMES[mode]} 전환 후 AUX가 HIGH가 되지 않음 — 배선 확인")
        time.sleep(0.005)             # 매뉴얼: AUX HIGH 후 약 2 ms 뒤 새 모드 동작
        self.ser.reset_input_buffer()
        self.mode = mode

    # -- 송수신 (NORMAL 모드)
    def send(self, data: bytes) -> None:
        if self.mode != NORMAL:
            raise RuntimeError("송신은 NORMAL 모드에서만")
        if len(data) > MAX_PACKET:
            raise ValueError(f"한 번에 {MAX_PACKET} B 이하만 전송 가능")
        if not self.wait_aux():
            raise TimeoutError("AUX LOW 지속 — 모듈이 바쁨")
        self.ser.write(data)
        self.ser.flush()

    def recv(self) -> bytes:
        """들어온 바이트를 있는 만큼 반환. 없으면 timeout(기본 50 ms) 후 b''."""
        n = self.ser.in_waiting
        return self.ser.read(n if n else 1)

    # -- 설정 (CONFIG 모드)
    def _read_exact(self, n: int, timeout: float) -> bytes:
        buf = b""
        t_end = time.monotonic() + timeout
        while len(buf) < n and time.monotonic() < t_end:
            buf += self.ser.read(n - len(buf))
        return buf

    def _config_cmd(self, cmd: bytes, resp_len: int) -> bytes:
        prev = self.mode if self.mode is not None else NORMAL
        self.set_mode(CONFIG)
        try:
            self.ser.reset_input_buffer()
            self.ser.write(cmd)
            self.ser.flush()
            return self._read_exact(resp_len, 1.0)
        finally:
            self.set_mode(prev)

    def read_config(self) -> E22Config:
        resp = self._config_cmd(bytes([0xC1, 0x00, 0x07]), 10)
        if len(resp) != 10 or resp[:3] != b"\xC1\x00\x07":
            raise IOError(f"설정 읽기 실패: {resp.hex(' ') or '응답 없음'} — CONFIG 모드·배선 확인")
        return E22Config.from_regs(resp[3:])

    def write_config(self, cfg: E22Config, permanent: bool = True) -> None:
        params = cfg.to_regs()
        head = 0xC0 if permanent else 0xC2      # C0 = 저장, C2 = 전원 끄면 사라짐
        resp = self._config_cmd(bytes([head, 0x00, 0x07]) + params, 10)
        if resp != b"\xC1\x00\x07" + params:
            raise IOError(f"설정 쓰기 실패: {resp.hex(' ') or '응답 없음'}")


# ---------------------------------------------------------------- 명령줄
def _print_cfg(cfg: E22Config) -> None:
    print(f"주소 0x{cfg.addr:04X} | NETID {cfg.net_id} | 채널 {cfg.channel} "
          f"({cfg.freq_mhz:.3f} MHz) | 공중속도 {cfg.air_rate} kbps | 출력 {cfg.tx_power_dbm} dBm")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="E22-900T22D 설정 도구")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--no-gpio", action="store_true", help="노트북 USB-UART (M0/M1 점퍼를 CONFIG로)")
    ap.add_argument("--read", action="store_true", help="현재 설정 읽기")
    ap.add_argument("--write", action="store_true", help="설정 쓰기")
    ap.add_argument("--channel", type=int, default=70)
    ap.add_argument("--air", type=float, default=2.4)
    ap.add_argument("--power", type=int, default=10)
    args = ap.parse_args()

    with E22(args.port, use_gpio=not args.no_gpio) as lora:
        if args.write:
            cfg = E22Config(channel=args.channel, air_rate=args.air, tx_power_dbm=args.power)
            lora.write_config(cfg)
            print("설정 쓰기 완료")
        if args.read or args.write:
            _print_cfg(lora.read_config())
        if not (args.read or args.write):
            ap.print_help()
