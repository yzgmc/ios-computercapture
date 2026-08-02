"""原画质无压缩视频传输模块（TCP 流式）。

通过 TCP 传输原始像素帧（BGRA），适用于局域网内对画质有无损要求的场景。
协议帧头定义见 protocol.py，TCP 接收见 transport.py。
"""
from .protocol import MAGIC, HEADER_SIZE, PixelFormat, pack_header, unpack_header, is_valid_header
from .transport import RawStreamReceiver

__all__ = [
    "MAGIC", "HEADER_SIZE", "PixelFormat",
    "pack_header", "unpack_header", "is_valid_header",
    "RawStreamReceiver",
]
