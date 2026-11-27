# 드론 탐지 + 경고 (Pi 5 + AI HAT + Camera Module 3 스테레오 / 아이폰)

```
train/   학습 + export
tools/   데이터셋 → YOLO 포맷 변환
pi/
  detect_pi.py       카메라 1대 버전 (bbox 크기로 경고)
  stereo_cam.py      카메라 2대 동시 캡처 (초점 무한대 고정, 동기화)
  capture_calib.py   캘리브레이션 사진 촬영
  calibrate.py       스테레오 캘리브레이션 → stereo_calib.npz
  detect_stereo.py   스테레오 탐지 + 거리/접근속도 경고  ← 메인
  udp_receiver.py    아이폰 경고 수신
  lcd_ui.py          1602 LCD 상태 화면 (detect_*.py --lcd)
  video_link.py      Wi-Fi 영상 송출 (detect_*.py --video-link)
ios/     아이폰 앱 Swift 소스
```

## 0. 하드웨어 조립

- Camera Module 3 두 대를 **약 25cm 간격**으로, 같은 방향·같은 높이로 **단단한 막대**(알루미늄 프로파일 등)에 고정
  - 간격 넓을수록 먼 거리 정확도↑ (25cm면 40m 안쪽이 쓸 만함)
  - 캘리브레이션 후 카메라가 조금이라도 움직이면 다시 해야 함
- 바라보는 방향 기준 왼쪽 → CAM0, 오른쪽 → CAM1 (반대로 꽂았으면 `--swap`)
- 카메라 케이블은 Pi 5용 22핀 (Camera Module 3 박스에 들어있음)

## 1. 데이터셋 → YOLO 변환

SSD에 `datasets/` 만들고 받은 것들을 변환해서 넣기

```bash
python tools/convert_voc.py --imgs DUT/train/img --xmls DUT/train/xml --out datasets/dut --split train
python tools/convert_voc.py --imgs DUT/val/img   --xmls DUT/val/xml   --out datasets/dut --split val
python tools/convert_antiuav.py --src Anti-UAV/train --out datasets/antiuav --split train --step 10
python tools/convert_antiuav.py --src Anti-UAV/val   --out datasets/antiuav --split val   --step 10
```

Roboflow / MAV-VID는 이미 YOLO 포맷이라 그대로 복사. 압축 푼 폴더 구조가 다르면 경로만 맞추면 됨.
`train/data.yaml` 의 `path`랑 폴더 목록을 실제 받은 것에 맞게 수정 (안 받은 건 줄 지우기)

## 2. 학습

```bash
pip install ultralytics onnx          # 아이폰용 CoreML export는 맥에서: pip install coremltools
python train/train.py --data train/data.yaml --epochs 100 --device 0      # RTX
python train/train.py --data train/data.yaml --epochs 100 --device mps    # 맥
```

CoreML은 맥에서만 export 되니까, 데스크탑에서 학습했으면 `best.pt`를 맥으로 옮겨서
`python train/train.py --weights best.pt --device mps` 한 번 더 돌리면 됨

## 3. Hailo 변환 (x86 리눅스: 데스크탑 Ubuntu 또는 WSL2)

```bash
hailomz compile yolov8n --ckpt best.onnx --hw-arch hailo8l --calib-path calib/ --classes 1
mv yolov8n.hef drone.hef      # AI HAT+ 26TOPS면 --hw-arch hailo8
```

`calib/` 에는 학습 이미지 500~1000장 복사 (양자화 보정용)

## 4. 스테레오 캘리브레이션 (Pi)

1. 체커보드 출력: 안쪽 꼭짓점 9x6 (칸 10x7), 한 칸 25~40mm. **폼보드에 평평하게** 붙이기
2. 촬영 (20~40쌍, 거리·각도·위치 다양하게, 화면 가장자리도)
   ```bash
   python3 capture_calib.py --out calib_imgs          # SPACE 저장
   python3 capture_calib.py --out calib_imgs --auto   # 화면 없을 때
   ```
3. 계산
   ```bash
   python3 calibrate.py --imgs calib_imgs --square 30   # 칸 크기 mm
   ```
   RMS 0.5px 이하 / baseline이 자로 잰 간격이랑 비슷 / `rectify_check.jpg` 가로선 확인

## 5. 실행 (Pi)

```bash
sudo apt install hailo-all
python3 -m venv --system-site-packages .venv && source .venv/bin/activate
pip install supervision trackers opencv-python

python3 detect_stereo.py --backend hailo --hef drone.hef --calib stereo_calib.npz --show --buzzer-pin 18
python3 detect_stereo.py --backend hailo --hef drone.hef --calib stereo_calib.npz --lora --node-id 1   # 지휘소로 LoRa 경보
```

- 부저는 **GPIO18**. 17은 E22 AUX라 같이 쓰면 출력끼리 부딪힘 (E22 = 14·15·17·22·27, LCD = 2·3). 겹치면 실행 전에 에러로 막음
- `--lora [PORT]`: 레포 루트 `modules/alert_gen.py` → `alert_tx.py`로 지휘소에 경보 (E22 기본 `/dev/serial0`, 관측소 번호 `--node-id`). 등급 바뀌면 바로, 같은 등급은 관심 2초·경계 1초·위험 0.5초마다, 표적 없으면 5초마다 생존 신호. E22를 못 열면 경고만 찍고 탐지는 계속
- 화면 라벨: `#3 23.5m +4.2m/s` = 3번 표적, 23.5m, 초속 4.2m로 접근 중. `*` 붙으면 한쪽만 잡혀서 크기로 추정한 값 (표적 번호는 1부터, 지휘소 앱 번호와 같음)
- 경고: 20m 이내 / 40m 이내에서 2m/s 이상 접근 / 5초 내 도달 예상 → WARNING (1.5초 유지)
- 기준값은 `--warn-m --watch-m --approach-mps` 로 조절
- `--record rec/` 붙이면 좌우 원본 영상 저장 → 맥에서
  `python3 detect_stereo.py --backend ultralytics --weights best.pt --calib stereo_calib.npz --video rec/xxx_left.mp4 rec/xxx_right.mp4 --show`
- UDP 출력 JSON에 거리, 접근속도, 도달예상시간, 방위각/고각이 들어감 → 요격 담당 팀원한테 넘길 인터페이스로 쓰면 됨

## 6. LCD 상태 화면 (1602 I2C, `pi/lcd_ui.py`)

배선: VCC → 2번(5V), GND → 6번, SDA → 3번(GPIO2), SCL → 5번(GPIO3)

**⚠ I2C 전압 먼저 재기.** Pi 핀은 3.3V용인데, LCD를 5V에 물리면 백팩(PCF8574)에 달린 풀업 저항 때문에 SDA·SCL이 3.3V보다 높아질 수 있음 (보통 3.7V 안팎)
1. Pi에 SDA·SCL을 꽂기 전에 LCD에 VCC·GND만 연결하고, 멀티미터로 백팩의 SDA–GND, SCL–GND 전압을 잰다
2. 5V 가까이 나오면 백팩 풀업이 5V에 걸린 것 → 백팩의 풀업 저항 2개(보통 4.7 kΩ, SDA·SCL 핀 근처)를 떼거나 I2C 레벨 시프터를 사이에 둔다. 0V면 풀업이 없는 백팩이라 그냥 연결해도 됨 (Pi 쪽 풀업이 3.3V로 잡아줌)
3. 연결 후 SDA·SCL이 3.3V 근처(3.6V 이하)인지 한 번 더 확인

```bash
sudo raspi-config nonint do_i2c 0 && sudo reboot     # I2C 켜기 (한 번만)
sudo apt install i2c-tools && i2cdetect -y 1         # 27 (또는 3f) 보이면 OK
pip install RPLCD smbus2
python3 lcd_ui.py --demo                             # 탐지 없이 화면만 확인 (25초 시나리오)
python3 detect_stereo.py --backend hailo --hef drone.hef --calib stereo_calib.npz --lcd   # detect_pi.py도 같음
```

| 화면 | 뜻 |
|---|---|
| `DET■ WARN□ ✣2  \|` | 감지 ON(■) / 경고 OFF(□) / 추적 중 드론 2대 / 끝 글자가 돌면 정상 동작 |
| `23.5m* ↓4.2 L12°` | 제일 위험한 표적: 거리(`*` = 한쪽만 잡혀 크기로 추정), 4.2m/s로 접근(↑면 멀어짐), 왼쪽 12° |
| `WARNING ETA 4.6s` | 경고 중 1줄이 번갈아 바뀜 + 백라이트 깜빡임 (`--lcd-no-blink`로 끔) |
| `SCANNING 30fps` → `WARN x2 @14:03` → `IP ...` | CLEAR일 때 3초마다 넘어감 (경고 횟수·마지막 시각, SSH용 IP) |
| `DET? WARN?` / `NO FRAME 5s` | 2초 넘게 프레임이 안 들어옴 (카메라/파이프라인 멈춤) |
| `SP_DEWS` / `stopped` 또는 `ERR ...` | 프로그램 종료 / 에러로 죽음 |

- 주소가 0x3f면 `--lcd 0x3f`, LCD 없이 맥에서 보려면 `--lcd fake` (터미널에 출력)
- 글자 대신 네모만 보이거나 아무것도 안 보이면 → 백팩 뒤 가변저항(명암) 돌리기
- LCD 선이 빠져도 탐지는 계속 돌고, 다시 꽂으면 3초 안에 알아서 재연결

## 7. Wi-Fi 영상 송출 (`pi/video_link.py`, 지휘소 `modules/gui.py`가 받음)

LoRa는 경보만, 영상은 Pi 5 내장 Wi-Fi로 따로 보냄 (LoRa로 영상은 너무 느림).

```bash
sudo nmcli device wifi hotspot ifname wlan0 ssid SP_DEWS password <8자 이상>   # Pi 주소 10.42.0.1
python3 detect_stereo.py --backend hailo --hef drone.hef --calib stereo_calib.npz --video-link   # detect_pi.py도 같음
python3 video_link.py --demo           # 카메라 없이 가짜 영상 + 가짜 경고 (노트북에서도 됨)
```

- 라이브 `/live.mjpg` (가로 640, 10 fps), 상태 `/state`, 경고 클립 목록 `/clips`
- 탐지 등급이 WARNING이 되면 앞 3초 + 끝난 뒤 4초를 `pi/clips/`에 JPEG로 저장 (최대 15초, 최근 30개)
- 메인 루프는 프레임 참조만 넘기고 바로 리턴. 줄이기·JPEG·전송·저장은 별도 스레드라 탐지 fps에 영향 거의 없음

## 튜닝

- 오탐 많음 → `--conf` 올리기, `--min-hits` 늘리기, negatives 이미지 추가
- 스테레오 짝이 잘 안 잡힘(`*` 많음) → 캘리브레이션 다시, `--y-tol` 8~10으로
- 화면 상단 `skew`가 계속 10ms 넘음 → 동기화가 안 되는 것. `sudo apt full-upgrade` 로 libcamera/picamera2 최신화
