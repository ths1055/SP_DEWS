# 본체(윈도우 + RTX 3070) 학습 준비

## 1. 이 폴더 놓기

`drone_detect.zip`을 SSD에 풀기 (예: `E:\drone_detect`)

## 2. 설치 (1회)

NVIDIA 드라이버 최신화 → python.org에서 **Python 3.11** 설치("Add to PATH" 체크)
→ `setup_windows.bat` 더블클릭 → 마지막에 `CUDA OK: True NVIDIA GeForce RTX 3070` 나오면 성공

## 3. 데이터셋 다운로드

| 순서 | 데이터셋 | 링크 | 특징 | 형식 |
|---|---|---|---|---|
| ① 필수 | Roboflow 드론 | universe.roboflow.com → "drone detection" 검색 | 바로 학습 가능, 다양한 배경 | YOLOv8 |
| ② 추천 | DUT Anti-UAV | github.com/wangdongdut/DUT-Anti-UAV | 검출용 1만 장, 작은 드론 많음 | VOC → 변환 |
| ③ 추천 | MAV-VID | github.com/KostadinovShalon/UAVDetectionTrackingBenchmark (README 링크) | 먼 거리 드론 영상 프레임 | YOLO |
| ④ 선택 | Anti-UAV (RGB) | github.com/ZhaoJ9014/Anti-UAV | RGB+적외선 영상, 용량 큼 | 영상 → 변환 |
| ⑤ 선택 | 새/비행기 사진 | Roboflow에서 "bird" 검색 | 오탐 줄이는 배경용 | 이미지만 |
| 신청 | Drone-vs-Bird | github.com/wosdetc/challenge | 새 vs 드론 구분 최고, 신청서 필요 | 따로 변환 |

- Roboflow: 이미지 수 많고(5천 장 이상) 하늘 배경 위주인 걸로 1~2개 고르기 → Download → **Format: YOLOv8** → zip
- 처음엔 ①만으로 시작해도 됨. 나머지는 받는 대로 추가

## 4. 폴더 구조 맞추기

```
E:\datasets\
├─ roboflow_drone\        ← Roboflow zip 그대로 풀기
│   ├─ train\images, train\labels
│   └─ valid\images, valid\labels
├─ dut\                   ← tools\convert_voc.py 결과
├─ mavvid\                ← train\images, train\labels, val\images, val\labels
├─ antiuav\               ← tools\convert_antiuav.py 결과
└─ negatives\             ← tools\make_negatives.py 결과
```

변환 명령 (가상환경 켜고: `.venv\Scripts\activate`)

```bat
python tools\convert_voc.py --imgs <DUT압축푼곳>\train\img --xmls <DUT압축푼곳>\train\xml --out E:\datasets\dut --split train
python tools\convert_voc.py --imgs <DUT압축푼곳>\val\img   --xmls <DUT압축푼곳>\val\xml   --out E:\datasets\dut --split val
python tools\convert_antiuav.py --src <AntiUAV>\train --out E:\datasets\antiuav --split train --step 10
python tools\convert_antiuav.py --src <AntiUAV>\val   --out E:\datasets\antiuav --split val   --step 10
python tools\make_negatives.py --src <새사진폴더> --out E:\datasets\negatives --max 3000
```

DUT 압축 푼 폴더 이름이 다르면 `--imgs`/`--xmls`만 실제 경로로.
Roboflow 폴더 여러 개면 하나로 합치거나 이름 `roboflow_drone`으로 맞추기.

## 5. 학습

```bat
quick_test.bat E:/datasets      ← 먼저 5~10분 테스트
train_full.bat E:/datasets      ← 이상 없으면 본 학습 (하룻밤)
```

`make_yaml.py`가 실제 있는 데이터셋만 골라서 `train\data.yaml`을 자동으로 만들어줌 (없는 건 `[ ]`로 표시)

에러 나면
- `CUDA out of memory` → bat 파일에서 `--batch 16`을 `8`로
- 데이터로더/멀티프로세싱 에러 → `--workers 4`를 `0`으로

## 6. 끝나면 확인할 것

`runs\detect\drone_v1\`
- `results.png` → mAP50 그래프 (0.8 이상이면 양호)
- `val_batch0_pred.jpg` → 박스 잘 치는지 눈으로
- `weights\best.pt`, `weights\best.onnx` → 다음 단계(Hailo 변환, 아이폰 CoreML)에 씀

여기까지 하고 다음 스텝 물어보면 됨.
