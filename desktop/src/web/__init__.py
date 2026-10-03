"""网页端传输模式：桌面端内置 HTTPS 服务器，浏览器直接推流。

与 LAN / USB / SRT 模式并列，是第四种接入方式：任何能打开网页的设备
（iPhone Safari、Android Chrome、另一台电脑）都可以通过 getUserMedia
采集摄像头与麦克风，经 WebSocket 推给桌面端。

优势：零安装、跨平台、不需要 iOS App；代价是浏览器只能在 HTTPS 下授权摄像头，
因此使用自签证书（手机首次访问需手动信任）。

协议沿用 RAW1 / AUD1，桌面端解码与虚拟设备输出链路完全复用。
"""
import logging

logger = logging.getLogger(__name__)

DEFAULT_WEB_PORT = 8443

_IMPORT_ERROR: BaseException | None = None
WebStreamReceiver = None

try:
    from .server import WebStreamReceiver as _WebStreamReceiver
    WebStreamReceiver = _WebStreamReceiver
except Exception as _e:  # aiohttp 未安装 / 版本不兼容
    _IMPORT_ERROR = _e
    logger.info("Web mode unavailable: %s", _e)


def is_web_available() -> bool:
    """网页模式所需依赖（aiohttp）是否可用。"""
    return WebStreamReceiver is not None


def web_import_error() -> str:
    """返回网页模式不可用的原因，用于 UI 提示。"""
    if _IMPORT_ERROR is None:
        return ""
    return f"{type(_IMPORT_ERROR).__name__}: {_IMPORT_ERROR}"


__all__ = [
    "WebStreamReceiver",
    "DEFAULT_WEB_PORT",
    "is_web_available",
    "web_import_error",
]
