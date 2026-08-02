"""原画质传输协议帧头定义。

帧头布局（大端序，共 32 字节）：
    magic         4B   固定标识 'RAW1'
    frame_id      4B   帧序号（单调递增，用于分片重组）
    chunk_idx     4B   当前分片索引（从 0 开始）
    total_chunks  4B   该帧总分片数
    width         4B   像素宽
    height        4B   像素高
    format        4B   像素格式（见 PixelFormat）
    bytes_per_row 4B   每行字节数（含 stride，接收方据此跳过填充）

每个 UDP 数据报 = 帧头 + payload（payload <= MAX_PAYLOAD_SIZE）。
接收方按 frame_id 收齐 total_chunks 个分片后重组为完整帧。
"""
import struct

MAGIC = b"RAW1"
# magic(4s) frame_id(I) chunk_idx(I) total_chunks(I) width(I) height(I) format(I) bytes_per_row(I)
HEADER_FORMAT = ">4sIIIIIII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 32

# UDP 安全负载上限：以太网 MTU 1500 - IP 头 20 - UDP 头 8 - 帧头 32 = 1440，取 1400 留余量
MAX_PAYLOAD_SIZE = 1400


class PixelFormat:
    BGRA = 0
    RGBA = 1
    YUV422 = 2  # (U Y V Y 打包)


def pack_header(frame_id: int, chunk_idx: int, total_chunks: int,
                width: int, height: int, pixel_format: int,
                bytes_per_row: int) -> bytes:
    return struct.pack(
        HEADER_FORMAT, MAGIC, frame_id, chunk_idx, total_chunks,
        width, height, pixel_format, bytes_per_row,
    )


def unpack_header(data: bytes) -> dict:
    fields = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    return {
        "magic": fields[0],
        "frame_id": fields[1],
        "chunk_idx": fields[2],
        "total_chunks": fields[3],
        "width": fields[4],
        "height": fields[5],
        "format": fields[6],
        "bytes_per_row": fields[7],
    }


def is_valid_header(data: bytes) -> bool:
    return len(data) >= HEADER_SIZE and data[:4] == MAGIC

