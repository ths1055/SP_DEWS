# SP_DEWS — Drone Early Warning System

소프트웨어프로젝트 팀 과제 「소형 드론 조기경보 시스템」 저장소입니다.
관측소(Raspberry Pi 5 + AI HAT + 스테레오 카메라)가 드론을 검출·판정하고, LoRa로 지휘소(노트북)에 경보를 보냅니다.

- 팀: 권우진(영상) · 노지헌(센서) · 최호재(통신) / 지도교수 최진구
- Claude Code로 작업할 때 이 문서를 먼저 읽고, 아래 "확정"과 "제안(팀 확인 전)"을 구분해서 다뤄야함.

---

## 1. 시스템 구조와 담당

```
[관측소 Pi]  detect_stereo.py → alert_gen.py → alert_tx.py → protocol.py → e22.py ──LoRa 900MHz──┐
[지휘소 PC]  alert_rx.py → protocol.py → e22.py ←─────────────────────────────────────────────────┘
                 └→ (UDP JSON) → gui.py
[관측소 Pi]  detect_stereo.py --video-link (video_link.py) ──Wi-Fi: 라이브 영상·경고 클립──→ gui.py
```

| 블록                  | 내용                                 | 담당             |
| --------------------- | ------------------------------------ | ---------------- |
| ① 스테레오 영상 입력 | 좌우 동기 캡처                       | 노지헌·권우진   |
| ② 드론 검출          | AI HAT · YOLOv8n                    | 권우진           |
| ③ 표적 추적          | ByteTrack                            | 권우진           |
| ④ 스테레오 거리 측정 | Z = f·B/d, 실패 시 단안             | 노지헌           |
| ⑤ 위협 판정          | 거리 + 접근 속도 → 관심·경계·위험 | 노지헌           |
| ⑥ 경보 생성          | 판정 결과 → 패킷, GPIO 부저, LCD    | **최호재** |
| ⑦⑧ LoRa 드라이버    | E22-900T22D (UART)                   | **최호재** |
| ⑨ 수신 처리          | CRC · ACK · 중복 제거              | **최호재** |
| ⑩ 경보 표시 GUI      | 등급·거리·속도·방향               | 공동             |

통신 성능 목표: **지휘소 표시 2초 이내, 재전송 시 전달률 95% 이상**

---

## 2. 파일 구성 (`modules/`)

| 파일                    | 실행 방식                                            | 상태                    | 설명                                               |
| ----------------------- | ---------------------------------------------------- | ----------------------- | -------------------------------------------------- |
| `protocol.py`         | import (자체 시험: `python protocol.py`)           | 완료, 자체 시험 통과    | 패킷 포장·개봉, CRC-16, 스트림 파서, seq 카운터   |
| `e22.py`              | import (설정 도구: `python e22.py --read/--write`, 자체 시험: `--test`) | 작성 완료, 실기 시험 전 | 모드 전환(M0/M1), AUX 대기, 송수신, 레지스터 설정  |
| `alert_gen.py`        | import (`detect_stereo.py --lora`, 자체 시험: `python alert_gen.py`) | 작성 (임시, 호재 확인 필요), 자체 시험 통과 | 관측소 경보 생성: 탐지 결과 → 관심·경계·위험 → 전송 정책대로 `AlertTx` 큐, 표적 없으면 생존 신호 |
| `alert_tx.py`         | import (실기 시험: `python alert_tx.py`)           | 작성 완료, 실기 시험 전 | 관측소 송신: 우선순위 TX 큐 + LoRa 스레드, ACK 대기·재전송 |
| `alert_rx.py`         | 직접 실행                                            | 작성 완료, 실기 시험 전 | 지휘소 수신: ACK 회신, `(node_id, seq)` 중복 제거, GUI로 UDP JSON |
| `test/test_alert.py`  | 직접 실행 (하드웨어 불필요)                          | 통과                    | 가짜 E22 2개로 우선순위·재전송·중복 제거·UDP 확인 |
| `test/test_send.py`   | 직접 실행                                            | 실기 송수신 확인        | 송신 시험 (`--raw` 글자 / 기본 경보 패킷)        |
| `test/test_recv.py`   | 직접 실행                                            | 실기 송수신 확인        | 수신 시험, 종료 시 전달률·CRC 오류·누락 seq 출력 |
| `test/rssi_check.py`  | 직접 실행                                            | 실기 확인               | 채널 잡음·마지막 수신 패킷 세기(dBm) — 무선부 고장 진단용 |
| `lcd.py`              | import                                               | 정리함                  | I2C 16×2 LCD (PCF8574, 0x27). ASCII만 가능        |
| `gui.py`              | 직접 실행 (`--demo`, `--lora COM4`, 자체 시험: `--selftest`) | 작성, 데모·가짜 LoRa로 시험 통과 | 지휘소 앱 (PySide6 창): 전술 스코프·주 표적·Wi-Fi 영상·기록·한국어 음성 안내·관측소 신호 끊김 표시 |
| `hud/`                | import                                               | 작성                    | gui.py 화면 조각: `model.py` 경보→표적 상태, `media.py` 영상·음성·경보음, `widgets.py` 그림, `fonts/` (SIL OFL) |
| `build_exe.py`        | 직접 실행 (윈도우)                                   | 빌드 스크립트 확인       | gui.py → `dist/SP_DEWS_HUD/SP_DEWS_HUD.exe` (파이썬 없는 PC에서도 실행) |

`test/` 안의 스크립트는 상위 `modules/`를 import 경로에 넣으므로 어느 폴더에서 실행해도 됩니다 (예: `modules`에서 `python test/test_send.py`).

### 스레드 구조 (alert_tx / alert_rx)

- 하드웨어 하나는 스레드 하나만 만진다. 관측소는 `lora-tx` 스레드만 E22를 쓰고, 다른 스레드는 `AlertTx.submit()`으로 큐에 넣기만 한다
- 채널은 데이터 성격대로: 경보(빠지면 안 됨) = 크기 제한 `PriorityQueue`(위험 > 경계 > 관심, 가득 차면 새 것 버림), 종료 신호 = `threading.Event`
- 재전송은 같은 seq 그대로 → 지휘소는 중복이어도 ACK를 다시 보내고(앞 ACK 유실 대비) GUI에는 한 번만 넘김
- 지휘소는 장치가 LoRa 하나라 메인 루프가 E22를 독점(스레드 없음), GUI는 별도 프로세스(UDP `127.0.0.1:5005`)
- ⑤ → ⑥은 탐지 루프가 `AlertGen.update()`를 바로 부름. 큐에 넣기만 해서 금방 끝나므로 최신값 Mailbox는 안 씀

### 기존 코드에서 고친 문제 (재발 방지용 기록)

| 기존 파일          | 문제                                                                    | 새 코드에서의 처리                                                   |
| ------------------ | ----------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `transceiver.py` | 같은 GPIO 핀을 모듈 전역과 `__init__`에서 두 번 생성 → `GPIO busy` | 핀 객체는 클래스 안에서 한 번만 생성                                 |
| `transceiver.py` | `set_mode`에 `self` 없음                                            | 수정                                                                 |
| `transceiver.py` | 송신 전 AUX 확인 없음, 수신 함수 없음                                   | `send()`에서 AUX 대기, `recv()` 추가                             |
| `data.py`        | `crc32(전체) != 0`으로 검사 → 항상 실패                              | 본문 CRC를 계산해 꼬리 값과 비교                                     |
| `data.py`        | 16 B를 `'!iif'`(12 B)로 unpack → `struct.error`                    | 본문만 unpack                                                        |
| `data.py`        | seq 없음 → ACK 매칭·중복 제거 불가                                    | seq 필드 추가                                                        |
| 공통               | UART 스트림에 패킷 경계 없음                                            | 시작 바이트 0xAA + 타입별 고정 길이로 프레임 분리 (`PacketParser`) |

---

## 3. 결정 사항

### 3-1. 확정 (코드에 반영됨)

| 항목          | 결정                                                                                                   |
| ------------- | ------------------------------------------------------------------------------------------------------ |
| 역할 분리     | `e22.py` = 바이트 운반(모듈 조작), `protocol.py` = 패킷 포장·개봉. 서로의 내부를 모름             |
| 바이트 순서   | big-endian                                                                                             |
| 무결성 검사   | CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF), `binascii.crc_hqx` 사용                               |
| 프레임 분리   | 시작 바이트 `0xAA` → 타입 바이트로 길이 결정 → CRC 실패 시 1바이트 밀고 재탐색                     |
| seq           | 1 B, 0~255 순환                                                                                        |
| E22 전송 방식 | 투명 전송(transparent), RSSI 바이트 출력 끔, 중계 끔, LBT 끔                                           |
| E22 UART      | 9600 8N1 (CONFIG 모드가 9600 고정이라 통일)                                                            |
| 서브패킷      | 240 B                                                                                                  |
| Pi 배선 (BCM) | M0 → GPIO22, M1 → GPIO27, AUX → GPIO17, E22 TXD → GPIO15, E22 RXD → GPIO14                        |
| 노트북        | USB-UART 어댑터, `use_gpio=False`(`--no-gpio`), M0/M1은 점퍼 (송수신: 둘 다 GND / 설정: M1만 3.3V) |

### 3-2. 제안 (구현했지만 팀 확인 전)

**경보 패킷 17 B** — 방향 정보를 넣고도 제안발표의 17 B를 유지하는 안

| 필드       | 크기 | struct | 단위·의미                                                                 |
| ---------- | ---- | ------ | -------------------------------------------------------------------------- |
| magic      | 1 B  | `B`  | 0xAA                                                                       |
| type       | 1 B  | `B`  | 0 경보, 1 ACK                                                              |
| node_id    | 1 B  | `B`  | 관측소 번호                                                                |
| seq        | 1 B  | `B`  | 0~255 순환                                                                 |
| track_id   | 1 B  | `B`  | 표적 번호 1~255 (ByteTrack ID를 1부터 접음). 0 + 등급 0 = 관측소 생존 신호 |
| level_mode | 1 B  | `B`  | 하위 2비트 등급(0 없음·1 관심·2 경계·3 위험), 비트7 = stereo(1)/mono(0) |
| dist       | 2 B  | `H`  | cm                                                                         |
| speed      | 2 B  | `h`  | 접근 속도 cm/s, + = 다가옴                                                 |
| tta        | 1 B  | `B`  | 도달 예상 시간 0.1 s, 255 = 해당 없음                                      |
| azimuth    | 1 B  | `b`  | 0.5° (±63.5°)                                                           |
| elevation  | 1 B  | `b`  | 0.5° (±63.5°)                                                           |
| ts_ms      | 2 B  | `H`  | 송신 시각 ms 하위 16비트                                                   |
| crc16      | 2 B  | `H`  | magic ~ ts_ms                                                              |

**ACK 6 B**: `magic(1) type=1(1) node_id(1) seq(1) crc16(2)`

**E22 기본 설정** (`E22Config` 기본값, 송수신 두 모듈은 addr·net_id·channel·air_rate가 같아야 함)

| 항목          | 값                | 비고                                                                     |
| ------------- | ----------------- | ------------------------------------------------------------------------ |
| addr / net_id | 0x0000 / 0        |                                                                          |
| channel       | 70 → 920.125 MHz | 주파수 = 850.125 + CH. 모듈 기본값 18(868 MHz)은 국내 대역이 아니라 변경 |
| air rate      | 2.4 kbps          | 모듈 기본값. 실측 송신 시간 보고 조정                                    |
| tx power      | 10 dBm (최저)     | **국내 출력 기준 미확인 → 교수님·연구실 확인 후 조정**           |

**⑤ → ⑥ 인터페이스** (노지헌 님과 합의 필요)

위협 판정이 아래 형태의 객체를 큐로 넘겨주는 안입니다.

```python
Threat(track_id, level, dist_m, speed_mps, tta_s, az_deg, el_deg, mode)  # mode: "stereo" | "mono"
```

지금 `alert_gen.py`는 `detect_stereo.py`의 RangeJudge가 주는 표적 dict(`id, dist_m, src, closing_mps, tta_s, az_deg, el_deg, level`)를 그대로 받습니다. RangeJudge 등급은 1(감지)/2(WARNING)뿐이라 3단계는 `alert_gen`이 나눔: **위험** = 경고선(20 m) 안 또는 5초 안 도달, **경계** = 탐지 WARNING, **관심** = 나머지.

**전송 정책** (`alert_gen.py`에 구현, 호재 확인 필요)

- 매 프레임(30 fps) 전송 금지 — LoRa 대역폭 초과
- 새 표적·등급 변화는 즉시, 같은 등급은 관심 2초·경계 1초·위험 0.5초마다 갱신
- 송신이 밀리면(끝나지 않은 경보 3건 이상) 같은 등급 갱신은 미룸 → 큐에 옛날 위치가 안 쌓이고, 풀리면 오래 기다린 표적부터 최신 값으로
- 표적이 사라지면 '없음' 한 번
- 표적이 없으면 5초마다 **생존 신호**(등급 0 + track_id 0) → 지휘소가 15초 넘게 아무것도 못 받으면 "관측소 신호 끊김"(드론 없음과 구분)
- 재전송 타임아웃 = 실측 송신 시간 × 2 + 여유, 최대 재전송 횟수는 2초 목표 안에 들어가게
- 지휘소 중복 제거 키는 `(node_id, seq)`

**지휘소 내부 전달**: `alert_rx.py`가 수신 결과를 로컬 UDP JSON으로 보내고 `gui.py`가 받는 구조 → GUI를 LoRa 없이 따로 개발·시험 가능

**영상 전달 (Wi-Fi, LoRa와 따로)**

- LoRa로는 영상 불가: 2.4 kbps면 작은 JPEG 한 장(약 2 KB)에도 7초 넘게 걸리고, 그동안 경보 채널이 막힘
- 관측소 Pi 5 내장 Wi-Fi로 따로 보냄: Pi를 핫스팟(`SP_DEWS`, Pi 주소 10.42.0.1)으로 두고 노트북이 붙음. 20 m는 충분
- 라이브 MJPEG (가로 640, 10 fps, 약 2~3 Mbps) + 경고(WARNING) 앞 3초·끝난 뒤 4초 클립 자동 저장 (`yolo_module/pi/clips/`)
- 경보는 항상 LoRa로 먼저 오고, Wi-Fi가 닿을 때만 영상이 붙음 (Wi-Fi가 끊겨도 경보 표시는 그대로)

---

## 4. 실행 방법

### Pi 준비 (최초 1회)

`sudo raspi-config` → Interface Options → Serial Port → 로그인 셸 **No**, 하드웨어 **Yes** → 재부팅. `/dev/serial0`이 생기면 OK.

필요 패키지: `pyserial`, `gpiozero`(Pi 5는 `lgpio` 백엔드), `RPLCD`(LCD)

GPIO 정리 (BCM): E22 = 14·15·17·22·27, LCD = 2·3 (I2C), **부저 = 18**. 겹치게 주면 `detect_*.py`가 실행 전에 에러로 막음 (17은 E22 AUX라 부저 출력과 부딪힘)

### 시험 순서

| 순서 | 어디서          | 명령                                                          | 확인                        |
| ---- | --------------- | ------------------------------------------------------------- | --------------------------- |
| 1    | 아무 PC         | `python protocol.py`, `python e22.py --test`, `python test/test_alert.py` | 모두 "통과"       |
| 2    | Pi 2대 각각     | `python e22.py --read`                                      | 설정값 출력 → 배선 OK      |
| 3    | Pi 2대 각각     | `python e22.py --write`                                     | 두 모듈 설정 일치           |
| 4    | 수신 → 송신 순 | `python test/test_recv.py --raw` / `python test/test_send.py --raw` | `hello 0, 1, ...` 수신    |
| 5    | 수신 → 송신 순 | `python test/test_recv.py` / `python test/test_send.py --count 100` | 패킷 출력, Ctrl+C 후 전달률 |
| 6    | 수신 → 송신 순 | `python alert_rx.py` / `python alert_tx.py --count 20`  | ACK 20/20, 재전송 횟수 확인 |
| 7    | 지휘소 → 관측소 | `python gui.py --lora COM4` / `python3 detect_stereo.py ... --lora --node-id 1` (`yolo_module/pi`) | 앱 머리줄 "● 관측소 1 정상", 드론 보이면 관심→경계→위험 |

노트북은 `--no-gpio --port COM3`(또는 `/dev/ttyUSB0`)을 붙입니다.

**아두이노 Uno를 USB-UART로 쓸 때** (11/17 실기 시험에서 확인)

- 배선: E22 TXD → D1, D0 → (1 kΩ/2 kΩ 분압) → E22 RXD, VCC → 5V, M0 → GND, M1 → GND(송수신)/3.3V(설정), AUX 비움
- **RESET → GND 필수**. 안 하면 Uno 칩이 D1을 붙잡아, 포트를 연 직후 응답 1번만 오고 수신이 전혀 안 됨 (`--read`만 되는 것처럼 보임)
- GND 배선이 빵판 레일 끊김 등으로 떠 있으면 같은 증상 → 멀티미터 통전 확인
- 모듈 설정이 서로 다르면(채널·키 등) 안 됨 → 두 모듈 모두 `e22.py --write` 1회

`--read`에서 "응답 없음"이면 TX↔RX 교차 연결, M0/M1/AUX 핀 번호부터 확인하세요.

**⚠ 안테나를 연결하지 않은 상태로 송신하지 말 것** (모듈 손상 가능)

### 지휘소 앱 (`gui.py`)

```bash
pip install PySide6-Addons pyserial      # 처음 한 번 (음성·소리 모듈이 Addons에 있음)
python gui.py --demo                     # LoRa 없이 가짜 경보로 화면 확인
python gui.py --lora COM4                # alert_rx까지 한 번에 (앱 위쪽 'LoRa' 버튼으로 골라도 됨)
python gui.py                            # alert_rx.py를 따로 켤 때 (위 1장 구조 그대로)
python gui.py --selftest                 # 하드웨어·화면 없이 자체 시험
pip install pyinstaller && python build_exe.py   # exe 만들기 → dist/SP_DEWS_HUD/SP_DEWS_HUD.exe
```

- 머리줄 관측소 상태: **● 정상**(생존 신호 받는 중 → "표적 없음"이면 정말 없는 것) / **신호 끊김**(15초 넘게 아무것도 안 옴 → 주황 표시 + 음성 "관측소 1 신호 끊김", 다시 오면 "복구"). 생존 신호를 안 보내는 예전 송신 코드면 마지막 수신 시각만 보여줌
- 데모를 끄면 가짜 표적·관측소 기록은 지워짐
- 버튼으로 바꾼 설정(LoRa 포트, 영상 주소, 음성·경보음, 스코프 범위)은 저장돼서 다음 실행 때 그대로
- 음성 안내는 Windows 내장 한국어 음성. 없으면 자막만 나옴 (설정 > 시간 및 언어 > 음성에서 한국어 추가)
- 단축키: F11 전체 화면, V 음성, S 경보음, R 범위, L 라이브

**Wi-Fi 영상 붙이기**

1. Pi: `sudo nmcli device wifi hotspot ifname wlan0 ssid SP_DEWS password <8자 이상>` (Pi 주소가 10.42.0.1이 됨. 이후 SSH도 이 Wi-Fi로)
2. Pi: `python3 detect_stereo.py ... --video-link` (기본 포트 8090)
3. 노트북: Wi-Fi `SP_DEWS`에 연결 → 앱이 `http://10.42.0.1:8090`을 알아서 찾음 (다르면 '영상 주소' 버튼)

---

## 5. 진행 현황과 다음 작업

현재 시점: 11월 1주차 종료 (2026-11-07)

| 시기        | 작업                                   | 파일                               | 상태                     |
| ----------- | -------------------------------------- | ---------------------------------- | ------------------------ |
| 11/1주      | 패킷 형식 설계, 기존 코드 수정         | `protocol.py`, `e22.py`        | 완료 (형식은 팀 확인 전) |
| 11/1주      | 보드 2대 송수신 시험                   | `test/test_send.py`, `test/test_recv.py` | 송수신 확인 (11/17, 노트북↔PC) |
| 11/2주      | ACK 대기·재전송 (송신 측)             | `alert_tx.py`                    | 작성, 실기 시험 전       |
| 11/2주      | ACK 회신·중복 제거 (수신 측)          | `alert_rx.py`                    | 작성, 실기 시험 전       |
| 11/2~3주    | 판정 결과 → 경보 생성 (부저·LCD는 탐지 쪽 `--buzzer-pin`·`--lcd`) | `alert_gen.py`, `detect_stereo.py --lora` | 작성 (임시, 호재 확인 필요). 가짜 카메라 → 가짜 E22 → 지휘소 앱까지 시험 통과 |
| 11/3주      | 지휘소 GUI 연동 (공동)                 | `gui.py`, `hud/`                 | 작성 (데모·가짜 LoRa 시험 통과, 실기 전) |
| 11/4주      | Wi-Fi 영상 (라이브·경고 클립)          | `yolo_module/pi/video_link.py`   | 작성 (가짜 카메라로 시험 통과) |
| 11/3~4주    | 표시 지연·전달률 측정                 | `test_link.py`                   | 예정                     |
| 11/4~12/1주 | 실외 20 m 시험, 다중 표적, 데모 리허설 | —                                 | 예정                     |

### 실기 시험 때 기록할 것

- `test/test_send.py`가 출력하는 **패킷당 송신 시간(ms)** → `alert_tx.py`의 `ACK_TIMEOUT_S`·`RETRIES` 근거
- 거리·장애물별 전달률, CRC 오류 횟수

### 열린 질문

- 경보 패킷 17 B 형식 (특히 방향 필드 포함) 팀 확정
- ⑤ → ⑥ `Threat` 인터페이스 노지헌 님과 합의
- 국내 LoRa 출력 기준 확인 후 `tx_power_dbm` 조정
- 임베디드 요건: TF-Luna 제외 후 GPIO 부저·I2C LCD로 입출력 장치 요건 충족 여부 (교수님 상담 사항)
- 생존 신호(등급 0 + track_id 0, 표적 없을 때 5초마다)와 지휘소 "신호 끊김" 기준 15초 — `alert_gen.py`·`gui.py`에 구현함, 주기·기준 팀 확인 필요 (LoRa 대역폭: 경보 + ACK 1회가 5초에 한 번)
- 3단계 등급(관심·경계·위험)을 ⑤ 위협 판정이 직접 줄지, 지금처럼 `alert_gen.py`가 나눌지

---

## 6. 코드 작성 규칙

- 주석과 출력 메시지는 한국어
- 하드웨어 없이 검증 가능한 부분은 `if __name__ == "__main__":` 자체 시험을 둔다
- 표는 마크다운 문법으로 작성 (옵시디언 호환)
- 패킷 형식을 바꾸면 이 문서의 3-2 표와 `protocol.py` 상단 docstring을 함께 수정
