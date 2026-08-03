"""原画质传输协议帧头定义（TCP 流式）。

帧头布局（大端序，共 28 字节）：
    magic           4B   固定标识 'RAW1'
    frame_id        4B   帧序号（单调递增，便于诊断乱序）
    width           4B   像素宽
    height          4B   像素高
    format          4B   像素格式（见 PixelFormat）
    bytes_per_row   4B   每行字节数（含 stride，接收方据此跳过填充）
    payload_length  4B   紧随其后的 payload 字节数

TCP 流式分帧：发送方 write(header) + write(payload)，接收方先读满 28B 帧头，
解析 payload_length 后再精确读 payload_length 字节，得到完整一帧。
不再需要 UDP 分片 / chunk_idx / total_chunks。
"""
import struct

MAGIC = b"RAW1"
# magic(4s) frame_id(I) width(I) height(I) format(I) bytes_per_row(I) payload_length(I)
HEADER_FORMAT = ">4sIIIIII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 28


class PixelFormat:
    BGRA = 0
    RGBA = 1
    YUV422 = 2  # (U Y V Y 打包)
    JPEG = 10   # JPEG 编码的 BGRA，payload 是 JPEG 字节流
    H264 = 20   # H.264 编码，payload 是 Annex-B 格式 Access Unit（关键帧含 SPS/PPS+IDR）


def pack_header(frame_id: int, width: int, height: int,
                pixel_format: int, bytes_per_row: int,
                payload_length: int) -> bytes:
    return struct.pack(
        HEADER_FORMAT, MAGIC, frame_id, width, height,
        pixel_format, bytes_per_row, payload_length,
    )


def unpack_header(data: bytes) -> dict:
    fields = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    return {
        "magic": fields[0],
        "frame_id": fields[1],
        "width": fields[2],
        "height": fields[3],
        "format": fields[4],
        "bytes_per_row": fields[5],
        "payload_length": fields[6],
    }


def is_valid_header(data: bytes) -> bool:
    return len(data) >= HEADER_SIZE and data[:4] == MAGIC
