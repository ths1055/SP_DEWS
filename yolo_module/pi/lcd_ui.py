#!/usr/bin/env python3
"""
1602 I2C LCD 상태 화면 (PCF8574 백팩, 기본 주소 0x27 = modules/lcd.py랑 같은 설정)

  python3 lcd_ui.py --demo                 # 배선 확인 + 시나리오 재생 (탐지 없이)
  python3 lcd_ui.py --demo --addr fake     # LCD 없이 터미널 미리보기 (맥/PC)
  python3 lcd_ui.py --msg SP_DEWS stopped  # 글자 한 번 띄우고 끝 (systemd 종료 때 등)

탐지 코드에서는 --lcd 만 붙이면 됨
  python3 detect_stereo.py ... --lcd       (주소 다르면 --lcd 0x3f, 미리보기는 --lcd fake)

화면 (16x2)
  1줄  DET■ WARN□ ✣2  |   감지/경고 ON(■) OFF(□), 추적 중인 드론 수, 동작표시(돌고 있으면 정상)
       WARNING 땐 'WARNING ETA 4.6s'랑 번갈아 나오고 백라이트 깜빡임
  2줄  CLEAR    : SCANNING 30fps → 경고 횟수·마지막 시각 → IP (3초마다 넘어감)
       감지/경고 : 제일 위험한 표적 1개
         스테레오   23.5m* ↓4.2 L12°   거리(*=한쪽만 잡혀서 크기로 추정) 접근속도 m/s(↓접근 ↑멀어짐) 방향(L/R)
         카메라 1대 #3 87% APPROACH
  프레임이 2초 넘게 안 들어오면 DET? WARN? / NO FRAME 5s (멈춘 화면이 정상처럼 남지 않게)

LCD 쓰기는 백그라운드 스레드에서 바뀐 글자만 함. 한 화면 통째로 쓰면 I2C로 수십 ms라 탐지 루프랑 분리.
I2C 오류(점퍼선 빠짐 등)가 나도 탐지는 계속 돌고, LCD만 3초마다 재연결 시도.
"""
import argparse
import atexit
import socket
import sys
import threading
import time

COLS, ROWS = 16, 2
TICK = 0.25          # 화면 갱신 주기(초). 숫자가 너무 빨리 바뀌면 못 읽어서 4Hz
PAGE_TICKS = 12      # CLEAR 2줄 페이지 넘김 (3초)
ALT_TICKS = 3        # WARNING 1줄 번갈아 표시 (0.75초)

# ---------------------------------------------------------------- 커스텀 문자 (CGRAM 0~7)
# ROM 글꼴은 A00/A02 버전마다 \ ~ ° 가 달라서, ASCII 기본 글자 말고는 전부 직접 그림
ON, OFF, DRONE, DOWN, UP, DEG, BKSL = (chr(i) for i in range(7))
GLYPHS = [
    (0b00000, 0b11111, 0b11111, 0b11111, 0b11111, 0b11111, 0b00000, 0b00000),  # ■ ON
    (0b00000, 0b11111, 0b10001, 0b10001, 0b10001, 0b11111, 0b00000, 0b00000),  # □ OFF (○는 o랑 헷갈려서 네모)
    (0b11011, 0b11011, 0b00100, 0b01110, 0b00100, 0b11011, 0b11011, 0b00000),  # 드론 (위에서 본 쿼드)
    (0b00100, 0b00100, 0b00100, 0b00100, 0b10101, 0b01110, 0b00100, 0b00000),  # ↓ 접근
    (0b00100, 0b01110, 0b10101, 0b00100, 0b00100, 0b00100, 0b00100, 0b00000),  # ↑ 멀어짐
    (0b01100, 0b10010, 0b10010, 0b01100, 0b00000, 0b00000, 0b00000, 0b00000),  # °
    (0b00000, 0b10000, 0b01000, 0b00100, 0b00010, 0b00001, 0b00000, 0b00000),  # \ (A00 ROM은 ¥라서)
]
SPIN = ("|", "/", "-", BKSL)
PREVIEW = {ON: "■", OFF: "□", DRONE: "✣", DOWN: "↓", UP: "↑", DEG: "°", BKSL: "\\"}


# ---------------------------------------------------------------- 글자 만들기
def _fit(s):
    """16칸에 맞추고, LCD ROM에 확실히 있는 ASCII + 커스텀 문자만 남김"""
    out = []
    for ch in str(s):
        if ch == "\\":
            ch = BKSL
        elif not (ch < "\x07" or " " <= ch <= "}"):
            ch = "?"
        out.append(ch)
    return "".join(out)[:COLS].ljust(COLS)


def _dist(d):
    if d is None:
        return "  --m"
    return f"{d:4.1f}m" if d < 99.95 else f"{min(d, 9999):4.0f}m"


def _speed(c):
    if c is None:
        return "    "
    arrow = DOWN if c >= 0.3 else UP if c <= -0.3 else " "
    v = abs(c)
    return arrow + (f"{v:3.1f}" if v < 9.95 else f"{min(v, 999):3.0f}")


def _dir(az):
    if az is None:
        return "    "
    if abs(az) < 1:
        return " CTR"
    return f"{'L' if az < 0 else 'R'}{min(abs(az), 99):2.0f}{DEG}"


def _eta(t):
    return f"{t:4.1f}s" if t < 9.95 else f"{min(t, 999):4.0f}s"


def _primary(tracks):
    """제일 위험한 표적: 레벨 높은 것 → 도달예상 짧은 것 → 가까운 것 → 큰 것"""
    def key(t):
        tta, d = t.get("tta_s"), t.get("dist_m")
        return (-t.get("level", 1), 1e9 if tta is None else tta, 1e9 if d is None else d,
                -t.get("area", 0.0), -t.get("conf", 0.0))
    return min(tracks, key=key)


def _target_line(t):
    if "dist_m" in t:                                  # detect_stereo
        mark = "*" if t.get("src") == "mono" else " "
        return f"{_dist(t['dist_m'])}{mark} {_speed(t.get('closing_mps'))} {_dir(t.get('az_deg'))}"
    s = f"#{t.get('id', '?')}"                         # detect_pi / 아이폰
    if t.get("conf") is not None:
        s += f" {t['conf'] * 100:.0f}%"
    tag = " APPROACH" if t.get("approaching") else " CLOSE" if t.get("level") == 2 else ""
    if len(s) + len(tag) > COLS:
        tag = tag[:5]
    return s + tag


def _runs(old, new):
    """바뀐 구간만 (한 칸 사이 끼인 건 그냥 같이 씀: 커서 옮기는 비용이랑 같아서)"""
    spans, i, n = [], 0, len(new)
    while i < n:
        if old[i] == new[i]:
            i += 1
            continue
        j = i + 1
        while j < n and (old[j] != new[j] or (j + 1 < n and old[j + 1] != new[j + 1])):
            j += 1
        spans.append((i, j))
        i = j
    return spans


def _release(dev):
    """RPLCD close()는 I2C 버스를 안 닫아서(fd 남음) 직접 닫음"""
    try:
        dev.close(clear=False)
    except Exception:
        pass
    bus = getattr(dev, "bus", None)
    if bus is not None:
        try:
            bus.close()
        except Exception:
            pass


def _hint(e):
    no = getattr(e, "errno", None)
    if isinstance(e, FileNotFoundError):
        return "I2C 꺼져 있음 → sudo raspi-config nonint do_i2c 0 후 재부팅"
    if isinstance(e, PermissionError):
        return "권한 없음 → sudo usermod -aG i2c $USER"
    if no in (5, 121):
        return "응답 없음 → 배선/주소 확인 (i2cdetect -y 1)"
    return str(e)


# ---------------------------------------------------------------- 화면
class LcdUI:
    """탐지 루프에서는 update()만 부르면 됨 (저장만 하고 바로 리턴). LCD 쓰기는 전부 내부 스레드"""

    def __init__(self, addr=0x27, port=1, blink=True, stale_s=2.0, open_fn=None, start=True,
                 clock=time.monotonic):
        self.addr, self.port, self.blink, self.stale_s = addr, port, blink, stale_s
        self._open_fn = open_fn or self._open_i2c
        self._clock = clock
        self._lock = threading.Lock()
        # 탐지 쪽에서 넘어오는 상태 (lock)
        self._level, self._tracks, self._fps = 0, [], None
        self._last = None                  # 마지막 update() 시각
        self._frames = 0
        self._msg = ("SP_DEWS", "starting...")
        self._warn_n, self._warn_at = 0, None
        # 화면 쪽 상태 (스레드 전용)
        self._dev = self._shown = self._bl = None
        self._tick = self._spin = self._seen = 0
        self._mode, self._mode_tick = None, 0
        self._retry_at, self._err, self._err_logged = 0.0, None, set()
        self._ip, self._ip_at = None, -1e9
        self._closed = False
        self._stop = threading.Event()
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="lcd", daemon=True)
            self._thread.start()
            atexit.register(self.close)
            prev = sys.excepthook

            def hook(et, ev, tb):              # 초기화 중에 죽어도 LCD에 에러 남기기
                self.close("stopped" if issubclass(et, KeyboardInterrupt) else f"ERR {et.__name__}")
                prev(et, ev, tb)
            sys.excepthook = hook

    # ---- 탐지 쪽에서 부르는 것
    def update(self, level, tracks, fps=None):
        """매 프레임 호출. level 0/1/2 + judge가 준 tracks 그대로"""
        tracks = [dict(t) for t in tracks]
        with self._lock:
            if level >= 2 and self._level < 2:
                self._warn_n += 1
                self._warn_at = time.strftime("%H:%M")
            self._level, self._tracks, self._fps = int(level), tracks, fps
            self._last = self._clock()
            self._frames += 1
            self._msg = None

    def message(self, line1, line2=""):
        """부팅 진행상황 등. 다음 update() 전까지 표시"""
        with self._lock:
            self._msg = (line1, line2)

    def close(self, reason=None):
        """끝날 때 마지막 화면을 'stopped'(에러면 ERR 이름)로 바꿔둠"""
        if self._closed:
            return
        self._closed = True
        if reason is None:
            et = sys.exc_info()[0]           # finally 안에서 불리면 터진 예외가 보임
            reason = ("stopped" if et is None or issubclass(et, (KeyboardInterrupt, SystemExit))
                      else f"ERR {et.__name__}")
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            if self._dev is None:
                self._connect()
            self._draw((_fit("SP_DEWS"), _fit(reason)), True)
        except Exception:
            pass
        if self._dev is not None:
            _release(self._dev)
        self._dev = None

    # ---- 스레드
    def _run(self):
        nxt = self._clock()
        while not self._stop.is_set():
            self.step()
            nxt += TICK
            now = self._clock()
            if nxt < now:                       # 밀렸으면 따라잡지 말고 다음 칸부터
                nxt = now + TICK
            self._stop.wait(nxt - now)

    def step(self, now=None):
        """한 칸: (필요하면 연결) → 화면 구성 → 바뀐 글자만 쓰기"""
        now = self._clock() if now is None else now
        try:
            if self._dev is None and now >= self._retry_at:
                self._connect()
            if self._dev is not None:
                self._draw(*self._compose(now))
        except OSError as e:                    # I2C 끊김 → 재연결 대기
            self._fail(e, now)
        except Exception as e:                  # 화면 만들다 생긴 버그여도 스레드는 살려둠
            self._log_once(f"[lcd] {type(e).__name__}: {e}")
        self._tick += 1

    def _open_i2c(self):
        from RPLCD.i2c import CharLCD
        from smbus2 import SMBus
        with SMBus(self.port) as bus:           # 먼저 찔러보기 (없으면 여기서 에러, 버스도 바로 닫힘)
            bus.write_byte(self.addr, 0x08)
        return CharLCD(i2c_expander="PCF8574", address=self.addr, port=self.port, cols=COLS,
                       rows=ROWS, dotsize=8, charmap="A02", auto_linebreaks=False,
                       backlight_enabled=True)

    def _connect(self):
        dev = self._open_fn()
        try:
            for i, g in enumerate(GLYPHS):
                dev.create_char(i, g)
        except Exception:
            _release(dev)
            raise
        self._dev, self._shown, self._bl = dev, [" " * COLS] * ROWS, True   # CharLCD가 init 때 지움
        if self._err is not None:
            print("[lcd] 다시 연결됨")
            self._err = None

    def _fail(self, e, now):
        if self._err is None:
            print(f"[lcd] I2C 오류, LCD 없이 계속 + 재연결 시도 중: {_hint(e)}")
        self._err = e
        if self._dev is not None:
            _release(self._dev)
        self._dev, self._retry_at = None, now + 3.0

    def _log_once(self, s):
        if s not in self._err_logged:
            self._err_logged.add(s)
            print(s)

    def _draw(self, lines, bl):
        dev = self._dev
        for r in range(ROWS):
            for c0, c1 in _runs(self._shown[r], lines[r]):
                dev.cursor_pos = (r, c0)
                dev.write_string(lines[r][c0:c1])
            self._shown[r] = lines[r]
        if bl != self._bl:
            dev.backlight_enabled = bl
            self._bl = bl
        show = getattr(dev, "show", None)        # 터미널 미리보기용
        if show:
            show()

    def _compose(self, now):
        with self._lock:
            level, tracks, fps, last = self._level, self._tracks, self._fps, self._last
            frames, msg, warn_n, warn_at = self._frames, self._msg, self._warn_n, self._warn_at
        if frames != self._seen:                 # 새 프레임 들어왔을 때만 스피너 한 칸
            self._seen = frames
            self._spin = (self._spin + 1) % len(SPIN)
        spin, k = SPIN[self._spin], self._tick

        if msg is not None:
            mode = "msg"
        elif last is None or now - last > self.stale_s:
            mode = "stale"
        else:
            mode = level
        if mode != self._mode:                   # 화면 바뀌면 페이지/깜빡임 처음부터
            self._mode, self._mode_tick = mode, k
        rel = k - self._mode_tick

        if mode == "msg":
            return (_fit(msg[0]), _fit(msg[1])), True
        if mode == "stale":
            age = 0 if last is None else now - last
            return (_fit("DET? WARN?"), _fit(f"NO FRAME {min(age, 999):3.0f}s")), True

        n = len(tracks)
        cnt = f"{DRONE}{min(n, 99)}" if n else ""
        l1 = f"DET{ON if level >= 1 else OFF} WARN{ON if level >= 2 else OFF} {cnt:<3}"
        l1 = l1[:COLS - 1].ljust(COLS - 1) + spin
        bl = True
        if level == 0:
            pages = ["SCANNING" if fps is None else f"SCANNING {fps:3.0f}fps"]
            if warn_n:
                pages.append(f"WARN x{warn_n} @{warn_at}")
            ip = self._get_ip(now)
            if ip:
                pages.append(f"IP {ip}" if len(ip) <= COLS - 3 else ip)
            l2 = pages[(rel // PAGE_TICKS) % len(pages)]
        else:
            top = _primary(tracks) if tracks else None
            l2 = _target_line(top) if top else ""
            if level >= 2:
                if (rel // ALT_TICKS) % 2:
                    tta = top.get("tta_s") if top else None
                    l1 = f"WARNING ETA{_eta(tta)}" if tta is not None else " !! WARNING !! " + spin
                bl = (not self.blink) or rel % ALT_TICKS != ALT_TICKS - 1
        return (_fit(l1), _fit(l2)), bl

    def _get_ip(self, now):
        if now - self._ip_at > 30:
            self._ip_at = now
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("10.255.255.255", 1))     # 패킷은 안 나감, 내 IP만 확인
                    ip = s.getsockname()[0]
                self._ip = None if ip.startswith(("0.", "127.")) else ip
            except OSError:
                self._ip = None
        return self._ip


class NullUI:
    """--lcd 안 줬을 때. 탐지 코드에서 if 없이 쓰려고"""

    def update(self, *a, **k):
        pass

    def message(self, *a, **k):
        pass

    def close(self, *a, **k):
        pass


class TerminalLCD:
    """RPLCD CharLCD 흉내 (쓰는 것만). LCD 없을 때 터미널로 미리보기"""

    def __init__(self, inplace=False, min_interval=0.5):
        self.buf = [[" "] * COLS for _ in range(ROWS)]
        self.cursor_pos = (0, 0)
        self.backlight_enabled = True
        self.inplace, self.min_interval = inplace, min_interval
        self._printed, self._last_t = None, -1e9

    def create_char(self, location, bitmap):
        pass

    def write_string(self, s):
        r, c = self.cursor_pos
        for ch in s:
            if c < COLS:
                self.buf[r][c] = ch
            c += 1
        self.cursor_pos = (r, c)

    def show(self, force=False):
        rows = ["".join(PREVIEW.get(ch, ch) for ch in row) for row in self.buf]
        bl = self.backlight_enabled
        state = (tuple(rows), bl if self.inplace else True)
        now = time.monotonic()
        if state == self._printed or (not force and now - self._last_t < self.min_interval):
            return
        tty = sys.stdout.isatty()
        dim, rst, clr = ("" if bl else "\x1b[2m", "\x1b[0m", "\x1b[2K") if tty else ("", "", "")
        box = [f"{dim}┌{'─' * COLS}┐", *(f"│{r}│" for r in rows), f"└{'─' * COLS}┘{rst}"]
        if self.inplace and tty and self._printed is not None:
            sys.stdout.write("\x1b[4F")
        sys.stdout.write("".join(f"{clr}{line}\n" for line in box))
        sys.stdout.flush()
        self._printed, self._last_t = state, now

    def close(self, clear=False):
        self.show(force=True)


def make_lcd(spec, blink=True):
    """--lcd 값으로 만들기: None → 안 씀 / 'fake' → 터미널 미리보기 / '0x27' 같은 주소 → I2C LCD"""
    if not spec:
        return NullUI()
    if str(spec).lower() == "fake":
        return LcdUI(open_fn=TerminalLCD, blink=blink)
    try:
        import RPLCD.i2c  # noqa: F401
        import smbus2  # noqa: F401
    except ImportError:
        print("[lcd] RPLCD/smbus2 없음 → pip install RPLCD smbus2 (LCD 없이 계속)")
        return NullUI()
    return LcdUI(addr=int(str(spec), 16), blink=blink)   # i2cdetect처럼 16진수 (27 = 0x27)


# ---------------------------------------------------------------- 데모
def demo_frame(t):
    """데모 시나리오 t초 → (level, tracks). None = 프레임 멈춤 흉내, 'end' = 끝"""
    if t < 3:
        return 0, []
    if t < 16:
        v = 1.5 if t < 8 else 4.5
        d = 55 - 1.5 * (min(t, 8) - 3) - 4.5 * max(0.0, t - 8)
        closing = v if t > 4.5 else None
        tta = d / closing if closing else None
        warn = d <= 20 or (closing is not None and closing >= 2 and d <= 40) or (tta is not None and tta <= 5)
        tracks = [dict(id=3, conf=0.82, dist_m=round(d, 1), src="mono" if 6 < t < 7 else "stereo",
                       closing_mps=closing, tta_s=None if tta is None else round(tta, 1),
                       az_deg=round(-14 + 1.2 * (t - 3), 1), el_deg=20.0, level=2 if warn else 1)]
        if 11 < t < 15:
            tracks.append(dict(id=5, conf=0.55, dist_m=round(60 + 2 * (t - 11), 1), src="stereo",
                               closing_mps=-2.0, tta_s=None, az_deg=24.0, el_deg=31.0, level=1))
        return max(tr["level"] for tr in tracks), tracks
    if t < 22:
        return 0, []
    if t < 26:
        return None
    return "end"


def run_demo(ui, loop=False):
    while True:
        ui.message("SP_DEWS  demo", "LCD OK")
        time.sleep(1.5)
        t0 = time.monotonic()
        while True:
            f = demo_frame(time.monotonic() - t0)
            if f == "end":
                break
            if f is not None:
                ui.update(*f, fps=29.7)
            time.sleep(1 / 30)
        if not loop:
            return


def main():
    ap = argparse.ArgumentParser(description="1602 I2C LCD 상태 화면")
    ap.add_argument("--addr", default="0x27", help="I2C 주소 (i2cdetect -y 1 로 확인) 또는 fake")
    ap.add_argument("--port", type=int, default=1)
    ap.add_argument("--demo", action="store_true", help="탐지 없이 시나리오 재생")
    ap.add_argument("--loop", action="store_true", help="데모 반복")
    ap.add_argument("--msg", nargs="+", metavar="LINE", help="글자 띄우고 끝 (최대 2줄)")
    ap.add_argument("--no-blink", action="store_true", help="WARNING 때 백라이트 안 깜빡임")
    args = ap.parse_args()

    if not (args.demo or args.msg):
        ap.print_help()
        return
    fake = args.addr.lower() == "fake"
    addr = 0 if fake else int(args.addr, 16)
    open_fn = (lambda: TerminalLCD(inplace=sys.stdout.isatty(), min_interval=0)) if fake else None
    if args.msg:
        ui = LcdUI(addr=addr, port=args.port, open_fn=open_fn, start=False)
        ui._connect()
        ui._draw((_fit(args.msg[0]), _fit(" ".join(args.msg[1:]))), True)
        _release(ui._dev)
        return
    ui = LcdUI(addr=addr, port=args.port, blink=not args.no_blink, open_fn=open_fn)
    try:
        run_demo(ui, args.loop)
    except KeyboardInterrupt:
        pass
    finally:
        ui.close()


if __name__ == "__main__":
    main()
