"""libsrt C API 的 ctypes 桥接。

仅依赖运行时 libsrt 共享库（Windows: libsrt.dll / srt.dll；Linux: libsrt.so；
macOS: libsrt.dylib）。库路径解析顺序：
    1. 环境变量 PHONECAM_LIBSRT_PATH
    2. ctypes 默认搜索路径（PATH / LD_LIBRARY_PATH / DYLD_LIBRARY_PATH）
    3. 常见安装路径

SRT C API 关键值（libsrt v1.5.x）：
    SRTSOCKET = int32; SRT_INVALID_SOCK=-1; SRT_ERROR=-1
    SRT_TRANSTYPE: SRTT_LIVE=0, SRTT_FILE=1
    SRT_SOCKOPT 枚举值（非连续，使用精确值，避免按 0..N 推断）

参考: https://github.com/HaishinKit/libsrt-xcframework (v1.5.4)
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 类型与常量
# --------------------------------------------------------------------------- #

AF_INET = 2
INADDR_ANY = 0
SOCKADDR_IN_SIZE = 16  # sizeof(struct sockaddr_in)

SRT_INVALID_SOCK = -1
SRT_ERROR = -1
SRT_OK = 0

# SRT_TRANSTYPE
SRTT_LIVE = 0
SRTT_FILE = 1

# SRT_SOCKOPT 枚举值（非连续，按 srt.h v1.5.4 精确取值）
SRTO_MSS = 0
SRTO_SNDSYN = 1
SRTO_RCVSYN = 2
SRTO_SNDBUF = 5
SRTO_RCVBUF = 6
SRTO_SNDTIMEO = 13
SRTO_RCVTIMEO = 14
SRTO_REUSEADDR = 15
SRTO_MAXBW = 16
SRTO_SENDER = 21
SRTO_TSBPDMODE = 22
SRTO_LATENCY = 23
SRTO_PASSPHRASE = 26
SRTO_PBKEYLEN = 27
SRTO_TLPKTDROP = 31
SRTO_SNDDROPDELAY = 32
SRTO_NAKREPORT = 33
SRTO_CONNTIMEO = 36
SRTO_RCVLATENCY = 43
SRTO_PEERLATENCY = 44
SRTO_MINVERSION = 45
SRTO_STREAMID = 46
SRTO_CONGESTION = 47
SRTO_MESSAGEAPI = 48
SRTO_PAYLOADSIZE = 49
SRTO_TRANSTYPE = 50
SRTO_PEERIDLETIMEO = 55
SRTO_PACKETFILTER = 60
SRTO_RETRANSMITALGO = 61
# 发送缓冲占用（unacknowledged bytes in send buffer, int32）。
# 用于背压监测：值越大说明对端 ACK 跟不上，链路拥塞。
SRTO_SNDDATA = 19


class sockaddr_in(ctypes.Structure):
    """BSD sockaddr_in（IPv4）。"""
    _fields_ = [
        ("sin_family", ctypes.c_ushort),
        ("sin_port", ctypes.c_ushort),    # 网络字节序（htons）
        ("sin_addr", ctypes.c_uint32),    # INADDR_ANY=0
        ("sin_zero", ctypes.c_char * 8),
    ]


class LibSRTNotFoundError(OSError):
    """无法定位 libsrt 共享库。"""


class LibSRTOSError(OSError):
    """libsrt 调用返回错误（含 srt_getlasterror_str 描述）。"""


# --------------------------------------------------------------------------- #
# 库加载
# --------------------------------------------------------------------------- #

def _candidate_paths() -> list[str]:
    """构造 libsrt 共享库候选路径列表。"""
    candidates: list[str] = []
    env_path = os.environ.get("PHONECAM_LIBSRT_PATH")
    if env_path:
        candidates.append(env_path)

    if sys.platform == "win32":
        # Windows 常见安装位置
        progfiles = os.environ.get("ProgramFiles", r"C:\Program Files")
        progfiles86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            "srt.dll", "libsrt.dll",
            r"C:\Windows\System32\srt.dll",
            r"C:\Windows\System32\libsrt.dll",
            os.path.join(progfiles, "srt", "bin", "srt.dll"),
            os.path.join(progfiles, "srt", "bin", "libsrt.dll"),
            os.path.join(progfiles86, "srt", "bin", "srt.dll"),
            os.path.join(progfiles86, "srt", "bin", "libsrt.dll"),
        ]
        if localappdata:
            candidates.append(os.path.join(localappdata, "srt", "bin", "srt.dll"))
    elif sys.platform == "darwin":
        candidates += [
            "libsrt.dylib", "libsrt.1.dylib",
            "/usr/local/lib/libsrt.dylib",
            "/opt/homebrew/lib/libsrt.dylib",
            "/usr/lib/libsrt.dylib",
        ]
    else:
        candidates += [
            "libsrt.so", "libsrt.so.1",
            "/usr/local/lib/libsrt.so",
            "/usr/lib/x86_64-linux-gnu/libsrt.so",
            "/usr/lib/libsrt.so",
        ]
    return candidates


def load_libsrt() -> ctypes.CDLL:
    """加载 libsrt 共享库，返回已配置原型的 CDLL 实例。

    失败抛 LibSRTNotFoundError。
    """
    lib: ctypes.CDLL | None = None
    last_err: Exception | None = None

    # 1) 显式路径尝试
    for path in _candidate_paths():
        try:
            lib = ctypes.CDLL(path)
            logger.info("Loaded libsrt from %s", path)
            break
        except OSError as e:
            last_err = e
            continue

    # 2) 平台默认名（依赖系统搜索路径）
    if lib is None:
        for name in ("srt", "libsrt"):
            try:
                lib = ctypes.CDLL(name)
                logger.info("Loaded libsrt via default loader: %s", name)
                break
            except OSError as e:
                last_err = e

    if lib is None:
        raise LibSRTNotFoundError(
            "libsrt shared library not found. Set PHONECAM_LIBSRT_PATH to the "
            "libsrt path (e.g. C:\\path\\to\\libsrt.dll). Last error: "
            f"{last_err}"
        )

    _configure_prototypes(lib)
    return lib


def _configure_prototypes(lib: ctypes.CDLL) -> None:
    """为 libsrt 函数设置 ctypes 原型，避免调用约定/参数歧义。"""
    def _f(name, restype, argtypes):
        fn = getattr(lib, name, None)
        if fn is None:
            return
        fn.restype = restype
        fn.argtypes = argtypes

    _f("srt_startup", ctypes.c_int, [])
    _f("srt_cleanup", ctypes.c_int, [])
    _f("srt_create_socket", ctypes.c_int32, [])
    _f("srt_close", ctypes.c_int, [ctypes.c_int32])
    _f("srt_setsockflag", ctypes.c_int,
       [ctypes.c_int32, ctypes.c_int, ctypes.c_void_p, ctypes.c_int])
    _f("srt_getsockflag", ctypes.c_int,
       [ctypes.c_int32, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)])
    _f("srt_bind", ctypes.c_int,
       [ctypes.c_int32, ctypes.c_void_p, ctypes.c_int])
    _f("srt_listen", ctypes.c_int, [ctypes.c_int32, ctypes.c_int])
    _f("srt_accept", ctypes.c_int32,
       [ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)])
    _f("srt_connect", ctypes.c_int,
       [ctypes.c_int32, ctypes.c_void_p, ctypes.c_int])
    _f("srt_send", ctypes.c_int,
       [ctypes.c_int32, ctypes.c_char_p, ctypes.c_int])
    _f("srt_recvmsg", ctypes.c_int,
       [ctypes.c_int32, ctypes.c_char_p, ctypes.c_int])
    _f("srt_sendmsg", ctypes.c_int,
       [ctypes.c_int32, ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int])
    _f("srt_getlasterror_str", ctypes.c_char_p, [])
    _f("srt_getlasterror", ctypes.c_int, [ctypes.POINTER(ctypes.c_int)])


# --------------------------------------------------------------------------- #
# 高层辅助
# --------------------------------------------------------------------------- #

def make_sockaddr_in(host: str, port: int) -> sockaddr_in:
    """构造 sockaddr_in。host="0.0.0.0" 或 "" 表示 INADDR_ANY。"""
    addr = sockaddr_in()
    addr.sin_family = AF_INET
    addr.sin_port = socket_htons(port)
    addr.sin_addr = 0 if host in ("", "0.0.0.0", "0.0.0.0") else ip_to_uint32(host)
    addr.sin_zero = b"\x00" * 8
    return addr


def socket_htons(port: int) -> int:
    """端口转网络字节序（以 host 端 c_ushort 表示）。"""
    # ctypes c_ushort 接受 int；用 Python 标准库 htons 保证字节序正确
    import socket as _socket
    return _socket.htons(port) & 0xFFFF


def ip_to_uint32(ip: str) -> int:
    """点分十进制 IPv4 -> 网络字节序 uint32。"""
    import socket as _socket
    return int.from_bytes(_socket.inet_aton(ip), "big")


def last_error_string(lib: ctypes.CDLL) -> str:
    """读取 libsrt 最近一次错误描述。"""
    try:
        s = lib.srt_getlasterror_str()
        return s.decode("utf-8", "replace") if s else "<no error>"
    except Exception as e:
        return f"<failed to read srt error: {e}>"
