import sys
import os
import asyncio
import logging

from PyQt6.QtWidgets import QApplication
from qasync import QEventLoop

from app import PhoneCamApp

# Windows GUI 模式下 stdout 不可见，日志写入文件便于排查连接问题
LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "phonecam.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PhoneCam")
    app.setApplicationDisplayName("PhoneCam")

    # 将 Qt 事件循环与 asyncio 集成
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    phone_cam_app = PhoneCamApp()
    phone_cam_app.show()

    # 应用退出时清理资源
    async def _on_quit():
        await phone_cam_app.shutdown()

    app.aboutToQuit.connect(lambda: loop.call_soon_threadsafe(
        asyncio.ensure_future, _on_quit()
    ))

    # 在事件循环启动后再调度 start()，避免 DeprecationWarning
    loop.call_soon_threadsafe(asyncio.ensure_future, phone_cam_app.start())

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
