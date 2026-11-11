@echo off
REM ===== 1회만 실행: 파이썬 가상환경 + PyTorch(CUDA) + Ultralytics 설치 =====
cd /d %~dp0
py -3.11 -m venv .venv
if errorlevel 1 (
  echo [ERROR] Python 3.11 not found. Install from python.org and check "Add to PATH".
  pause & exit /b 1
)
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
pip install ultralytics onnx onnxslim opencv-python
echo.
python -c "import torch; ok=torch.cuda.is_available(); print('CUDA OK:', ok, torch.cuda.get_device_name(0) if ok else '-> update NVIDIA driver')"
pause
