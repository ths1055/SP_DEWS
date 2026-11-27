#!/usr/bin/env python3
"""
build_exe.py — 지휘소 앱(gui.py)을 exe로 묶기 (PyInstaller). 윈도우에서 실행

  pip install pyinstaller
  python build_exe.py
  → dist/SP_DEWS_HUD/SP_DEWS_HUD.exe   (SP_DEWS_HUD 폴더째 옮겨서 쓰면 됨, 파이썬 없는 PC에서도 실행)

exe에 붙이는 옵션은 python gui.py랑 같음 (예: SP_DEWS_HUD.exe --demo, SP_DEWS_HUD.exe --lora COM4).
옵션 없이 더블클릭해도 앱 위쪽 'LoRa'·'영상 주소'·'데모' 버튼으로 다 고를 수 있고, 고른 건 저장됨.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def make_icon(path: Path) -> bool:
    """앱 로고를 아이콘 파일로 (Qt로 그림)"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(HERE))
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtWidgets import QApplication

    from hud.widgets import Brand
    app = QApplication.instance() or QApplication([])
    pm = QPixmap(256, 256)
    pm.fill(0)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    Brand.draw_logo(p, 128, 128, 96)
    p.end()
    ok = pm.save(str(path))
    del app
    return ok


def main() -> None:
    try:
        import PyInstaller.__main__ as pyi
    except ImportError:
        print("PyInstaller가 없음 → pip install pyinstaller")
        sys.exit(1)
    build = HERE / "build"
    build.mkdir(exist_ok=True)
    icon = build / ("SP_DEWS_HUD.ico" if sys.platform == "win32" else "SP_DEWS_HUD.png")
    args = [
        str(HERE / "gui.py"), "--name", "SP_DEWS_HUD", "--noconfirm", "--clean", "--windowed",
        "--add-data", f"{HERE / 'hud' / 'fonts'}{os.pathsep}hud/fonts",
        "--paths", str(HERE),                        # protocol·alert_rx·e22 (데모·LoRa 버튼용)
        "--hidden-import", "PySide6.QtTextToSpeech",
        "--distpath", str(HERE / "dist"), "--workpath", str(build), "--specpath", str(build),
    ]
    for mod in ("PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick",
                "PySide6.QtPdf", "tkinter"):
        args += ["--exclude-module", mod]
    if sys.platform == "win32":                      # 윈도우는 경보음에 winsound를 써서 Qt 멀티미디어(FFmpeg)가 필요 없음
        args += ["--exclude-module", "PySide6.QtMultimedia"]
    if make_icon(icon):
        args += ["--icon", str(icon)]
    pyi.run(args)
    exe = HERE / "dist" / "SP_DEWS_HUD" / ("SP_DEWS_HUD.exe" if sys.platform == "win32" else "SP_DEWS_HUD")
    print(f"\n완료: {exe}" if exe.exists() else "\n빌드 실패 — 위 메시지 확인")


if __name__ == "__main__":
    main()
