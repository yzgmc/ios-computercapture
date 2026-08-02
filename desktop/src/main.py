import sys
import asyncio
import logging

from PyQt6.QtWidgets import QApplication
from qasync import QEventLoop

from app import PhoneCamApp

logging.basicConfig(level=logging.INFO)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PhoneCam")
    app.setApplicationDisplayName("PhoneCam")

    # 将 Qt 事件循环与 asyncio 集成
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    phone_cam_app = PhoneCamApp()
    phone_cam_app.show()

    # 启动后异步拉起内嵌信令服务器 + 自动连接
    asyncio.ensure_future(phone_cam_app.start())

    # 应用退出时清理资源
    async def _on_quit():
        await phone_cam_app.shutdown()

    app.aboutToQuit.connect(lambda: asyncio.ensure_future(_on_quit()))

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
