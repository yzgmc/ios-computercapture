"""Fake iPhone sender for testing desktop-side raw stream receiver.

按 iOS 端 RawStreamServer 的 28B 帧头格式发送假 BGRA 视频帧，
用于在 Windows 上验证桌面端能否正常解码并显示画面。

帧头布局（大端序，28 字节）：
    magic(4)="RAW1" | frame_id(4) | width(4) | height(4)
    | format(4=0 BGRA) | bytes_per_row(4) | payload_length(4)
"""
import argparse
import socket
import struct
import time

MAGIC = b"RAW1"
HEADER_FORMAT = ">4sIIIIII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 28


def make_bgra_frame(width: int, height: int, frame_id: int) -> bytes:
    """生成一帧带渐变 + 帧序号水印的 BGRA 图像（每像素 4 字节）。"""
    bytes_per_row = width * 4
    payload_size = bytes_per_row * height
    payload = bytearray(payload_size)

    for y in range(height):
        for x in range(width):
            i = y * bytes_per_row + x * 4
            # 颜色随时间和位置变化，便于在桌面端观察
            t = frame_id * 4
            payload[i + 0] = (x * 255 // width) & 0xFF  # B
            payload[i + 1] = (y * 255 // height) & 0xFF  # G
            payload[i + 2] = ((x + y + t) * 255 // (width + height + 255)) & 0xFF  # R
            payload[i + 3] = 255  # A

    header = struct.pack(
        HEADER_FORMAT, MAGIC, frame_id, width, height,
        0,  # format=0 BGRA
        bytes_per_row, payload_size,
    )
    return header + bytes(payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    print(f"Connecting to {args.host}:{args.port}...")
    sock.connect((args.host, args.port))
    print(f"Connected. Sending {args.width}x{args.height}@{args.fps}fps frames...")

    frame_interval = 1.0 / args.fps
    frame_id = 0
    try:
        while True:
            data = make_bgra_frame(args.width, args.height, frame_id)
            sock.sendall(data)
            if frame_id % 30 == 0:
                print(f"  sent frame {frame_id}, payload={len(data) - HEADER_SIZE}B")
            frame_id += 1
            time.sleep(frame_interval)
    except KeyboardInterrupt:
        print(f"\nStopped. Sent {frame_id} frames.")
    except Exception as e:
        print(f"\nError after {frame_id} frames: {e}")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
