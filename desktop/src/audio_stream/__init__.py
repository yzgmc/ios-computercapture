"""独立 UDP 音频通道模块。

通过 UDP 接收 iOS 端发送的 PCM 音频包，转交 AudioPlayer 用 PyAudio 播放。
协议帧头见 protocol.py，UDP 接收见 transport.py，播放器见 player.py。
"""
from .protocol import (
    MAGIC, HEADER_SIZE, AudioFormat,
    pack_header, unpack_header, is_valid_header,
)
from .transport import AudioStreamReceiver
from .player import AudioPlayer

__all__ = [
    "MAGIC", "HEADER_SIZE", "AudioFormat",
    "pack_header", "unpack_header", "is_valid_header",
    "AudioStreamReceiver", "AudioPlayer",
]
