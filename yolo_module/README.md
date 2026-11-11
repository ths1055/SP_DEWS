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

python3 detect_stereo.py --backend hailo --hef drone.hef --calib stereo_calib.npz --show --buzzer-pin 17
```

- 화면 라벨: `#3 23.5m +4.2m/s` = 3번 표적, 23.5m, 초속 4.2m로 접근 중. `*` 붙으면 한쪽만 잡혀서 크기로 추정한 값
- 경고: 20m 이내 / 40m 이내에서 2m/s 이상 접근 / 5초 내 도달 예상 → WARNING (1.5초 유지)
- 기준값은 `--warn-m --watch-m --approach-mps` 로 조절
- `--record rec/` 붙이면 좌우 원본 영상 저장 → 맥에서
  `python3 detect_stereo.py --backend ultralytics --weights best.pt --calib stereo_calib.npz --video rec/xxx_left.mp4 rec/xxx_right.mp4 --show`
- UDP 출력 JSON에 거리, 접근속도, 도달예상시간, 방위각/고각이 들어감 → 요격 담당 팀원한테 넘길 인터페이스로 쓰면 됨

## 튜닝

- 오탐 많음 → `--conf` 올리기, `--min-hits` 늘리기, negatives 이미지 추가
- 스테레오 짝이 잘 안 잡힘(`*` 많음) → 캘리브레이션 다시, `--y-tol` 8~10으로
- 화면 상단 `skew`가 계속 10ms 넘음 → 동기화가 안 되는 것. `sudo apt full-upgrade` 로 libcamera/picamera2 최신화
