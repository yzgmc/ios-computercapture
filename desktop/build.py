"""
使用 PyInstaller 打包桌面端应用为可执行文件。

运行:
    python build.py

输出:
    dist/PhoneCam/
"""
import PyInstaller.__main__
import os
import shutil
import sys


def _copy_libsrt(dist_dir: str):
    """将 libsrt 运行时目录（libsrt.dll 及其依赖）复制到打包产物。

    桌面端 libsrt.py 的 _candidate_paths 会查找
    <exe_dir>/libsrt/ 或 <exe_dir>/_internal/libsrt/，两种布局都覆盖。
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(base_dir, "libsrt")
    if not os.path.isdir(src):
        print("[build] 未找到 libsrt 目录，跳过 SRT 运行时打包")
        return
    for rel in ("libsrt", os.path.join("_internal", "libsrt")):
        dst = os.path.join(dist_dir, rel)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        print(f"[build] 已复制 libsrt 运行时 -> {dst}")


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
        "--hidden-import", "cv2",
        "--hidden-import", "pyvirtualcam",
        "--hidden-import", "pyaudio",
        "--collect-all", "PyQt6",
    ]

    sys.argv = ["pyinstaller"] + args
    PyInstaller.__main__.run()

    # 打包完成后复制 libsrt 运行时
    dist_dir = os.path.join(base_dir, "dist", "PhoneCam")
    _copy_libsrt(dist_dir)


if __name__ == "__main__":
    main()
