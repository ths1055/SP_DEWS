@echo off
REM ===== 데이터 5%%로 2 epoch 돌려서 에러 없는지 확인 (5~10분) =====
REM 사용법: quick_test.bat E:/datasets
cd /d %~dp0
call .venv\Scripts\activate
set ROOT=%~1
if "%ROOT%"=="" set ROOT=E:/datasets
python tools\make_yaml.py --root %ROOT%
if errorlevel 1 (pause & exit /b 1)
python train\train.py --data train\data.yaml --epochs 2 --fraction 0.05 --batch 16 --device 0 --workers 2 --name quick_test
pause
