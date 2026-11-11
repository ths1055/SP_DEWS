"""
드론 탐지 YOLO 학습 + export
  - 라즈베리(Hailo)용: ONNX  → 이후 Hailo 컴파일러로 HEF 변환
  - 아이폰용: CoreML(.mlpackage, NMS 포함)

예)
  python train.py --data data.yaml --epochs 100 --device 0        # RTX 데스크탑
  python train.py --data data.yaml --epochs 100 --device mps      # 맥 M칩
  python train.py --weights runs/detect/drone_v1/weights/best.pt  # 학습 건너뛰고 export만
"""
import argparse
import platform
from ultralytics import YOLO


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", default="data.yaml")
    p.add_argument("--model", default="yolov8n.pt")  # Hailo Model Zoo 컴파일 레시피가 제일 안정적
    p.add_argument("--weights", default=None, help="지정하면 학습 생략하고 export만")
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--epochs", type=int, default=100)
    p.add_argument("--batch", type=int, default=16)
    p.add_argument("--device", default="0", help="NVIDIA GPU: 0 / 맥 M칩: mps / 없으면 cpu")
    p.add_argument("--name", default="drone_v1")
    p.add_argument("--workers", type=int, default=4, help="윈도우에서 에러 나면 0~2로")
    p.add_argument("--fraction", type=float, default=1.0, help="0.05면 데이터 5%만 써서 빠른 테스트")
    args = p.parse_args()

    if args.weights:
        best_path = args.weights
    else:
        model = YOLO(args.model)
        model.train(
            data=args.data, imgsz=args.imgsz, epochs=args.epochs, batch=args.batch,
            device=args.device, name=args.name, patience=20,
            workers=args.workers, fraction=args.fraction,
            # 작은 물체/하늘 배경 대응 증강
            mosaic=1.0, scale=0.5, fliplr=0.5, degrees=5.0, hsv_v=0.4,
        )
        best_path = str(model.trainer.best)

    best = YOLO(best_path)
    best.val(data=args.data, imgsz=args.imgsz, device=args.device)

    onnx = best.export(format="onnx", imgsz=args.imgsz, opset=11)          # Hailo용
    print(f"\nONNX  : {onnx}")
    if platform.system() == "Darwin":  # CoreML은 맥에서만 export 가능
        coreml = best.export(format="coreml", imgsz=args.imgsz, nms=True)
        print(f"CoreML: {coreml}")
    else:
        print("CoreML(아이폰용)은 best.pt를 맥으로 옮겨서 --weights best.pt 로 다시 실행")


if __name__ == "__main__":
    main()
