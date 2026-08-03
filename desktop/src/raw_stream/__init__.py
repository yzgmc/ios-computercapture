"""原画质视频传输模块（TCP 流式 / SRT 消息）。

通过 TCP 或 SRT 传输原始像素帧（BGRA / JPEG / H.264）。
协议帧头定义见 protocol.py；TCP 接收见 transport.py；SRT 接收见
srt_transport.py（依赖运行时 libsrt 共享库，详见 libsrt.py）；H.264 解码见 h264_decoder.py。
"""
from .protocol import MAGIC, HEADER_SIZE, PixelFormat, pack_header, unpack_header, is_valid_header
from .transport import RawStreamReceiver
from .h264_decoder import H264Decoder

# SRT 是可选传输方式；libsrt 共享库未安装时导入仍可成功（仅在实例化时失败）
try:
    from .srt_transport import SRTStreamReceiver
except Exception as _e:  # pragma: no cover - 仅在 libsrt 缺失时
    SRTStreamReceiver = None  # type: ignore[assignment]

__all__ = [
    "MAGIC", "HEADER_SIZE", "PixelFormat",
    "pack_header", "unpack_header", "is_valid_header",
    "RawStreamReceiver", "H264Decoder",
    "SRTStreamReceiver",
]
