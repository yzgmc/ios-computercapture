"""
使用 PyInstaller 打包桌面端应用为可执行文件。

运行:
    python build.py

输出:
    dist/PhoneCam/
"""
import PyInstaller.__main__
import os
import sys


def main():
    # 确保在 desktop 目录下运行
    base_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(base_dir)

    args = [
        "src/main.py",
        "--name", "PhoneCam",
        "--windowed",
        "--one-dir",
        "--clean",
        "--noconfirm",
        "--hidden-import", "aiortc",
        "--hidden-import", "aiohttp",
        "--hidden-import", "cv2",
        "--hidden-import", "pyvirtualcam",
        "--hidden-import", "pyaudio",
        "--hidden-import", "zeroconf",
        "--collect-all", "PyQt6",
    ]

    sys.argv = ["pyinstaller"] + args
    PyInstaller.__main__.run()


if __name__ == "__main__":
    main()
