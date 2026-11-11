@echo off
REM ===== 본 학습 (하룻밤). 결과: runs\detect\drone_v1\ =====
REM 사용법: train_full.bat E:/datasets
cd /d %~dp0
call .venv\Scripts\activate
set ROOT=%~1
if "%ROOT%"=="" set ROOT=E:/datasets
python tools\make_yaml.py --root %ROOT%
if errorlevel 1 (pause & exit /b 1)
REM 잠자기 방지: 윈도우 설정 > 전원 > 절전 '안 함'으로 해두기
python train\train.py --data train\data.yaml --epochs 100 --batch 16 --device 0 --workers 4 --name drone_v1
pause
