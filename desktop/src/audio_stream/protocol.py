"""UDP 音频传输协议帧头定义。

帧头布局（大端序，共 16 字节）：
    magic           4B   固定标识 'AUD1'
    seq             4B   帧序号（uint32，单调递增；用于诊断丢包/乱序）
    sample_rate     4B   采样率（uint32，Hz，如 48000）
    channels        1B   声道数（uint8，1=mono / 2=stereo）
    format          1B   PCM 格式（uint8，见 AudioFormat）
    payload_length  2B   payload 字节数（uint16，应等于 UDP 数据报长度 - 16）

UDP 数据报 = 16B 帧头 + payload_length 字节 PCM 数据。
UDP 不保证可靠性，丢包直接丢弃对应音频片段，不影响后续播放。
"""
import struct

MAGIC = b"AUD1"
# magic(4s) seq(I) sample_rate(I) channels(B) format(B) payload_length(H)
HEADER_FORMAT = ">4sIIBBH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 16


class AudioFormat:
    PCM16_LE = 0      # 16-bit signed little-endian PCM
    PCM_FLOAT32_LE = 1  # 32-bit float little-endian PCM


def pack_header(seq: int, sample_rate: int, channels: int,
                audio_format: int, payload_length: int) -> bytes:
    return struct.pack(
        HEADER_FORMAT, MAGIC, seq, sample_rate, channels,
        audio_format, payload_length,
    )


def unpack_header(data: bytes) -> dict:
    fields = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    return {
        "magic": fields[0],
        "seq": fields[1],
        "sample_rate": fields[2],
        "channels": fields[3],
        "format": fields[4],
        "payload_length": fields[5],
    }


def is_valid_header(data: bytes) -> bool:
    return len(data) >= HEADER_SIZE and data[:4] == MAGIC
