"""SRT 接收器：监听端口，等待 iOS caller 主动连接。

与 RawStreamReceiver 接口对齐（start/stop/on_frame/on_disconnect），
便于上层 app.py 复用相同的多模式切换逻辑。

传输模式: LIVE + 消息 API（默认）。每帧（28B 头 + payload）作为一个 SRT 消息发送，
消息 API 自动保持帧边界，无需像 TCP 那样按字节流分帧。

线程模型:
    - 主 asyncio 线程: 调度 start/stop
    - 后台线程: 阻塞 srt_accept + srt_recvmsg
    - on_frame 回调: 在后台线程调用（Qt 信号 emit 线程安全，OK）
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time

from . import libsrt as _srt
from .protocol import HEADER_SIZE, is_valid_header, unpack_header

logger = logging.getLogger(__name__)

# 单条 SRT 消息最大长度（H.264 关键帧 ~100KB，BGRA 1080p ~8MB，BGRA 4K ~32MB）
# SRT 消息 API 单消息上限默认 8MB（live 模式）。设大一些防御 BGRA 帧。
_RECV_BUF_SIZE = 64 * 1024 * 1024  # 64MB，防御极端 BGRA 帧
# accept 轮询检查 stop 标志的间隔
_STOP_POLL_INTERVAL = 0.5


class SRTStreamReceiver:
    """SRT 视频帧接收器（listener 模式）。

    与 :class:`RawStreamReceiver` 同接口，便于上层替换。
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 5000,
                 on_frame=None, on_disconnect=None,
                 latency_ms: int = 120,
                 passphrase: str | None = None,
                 pbkeylen: int = 16):
        self.host = host
        self.port = port
        self.on_frame = on_frame
        self.on_disconnect = on_disconnect
        self.latency_ms = latency_ms
        self.passphrase = passphrase
        self.pbkeylen = pbkeylen

        self._lib: ctypes.CDLL | None = None
        self._listener_sock: int = _srt.SRT_INVALID_SOCK
        self._client_sock: int = _srt.SRT_INVALID_SOCK
        self._accept_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._startup_done = False

        # 接收统计（仿 RawStreamReceiver）
        self._frame_count = 0
        self._recv_t0: float | None = None
        self._recv_n = 0
        self.is_running = False

    async def start(self):
        """启动 listener：bind + listen + 后台 accept 线程。"""
        if self.is_running:
            logger.warning("SRTStreamReceiver: already running")
            return
        lib = _srt.load_libsrt()
        self._lib = lib

        # srt_startup 全局仅一次，进程退出时 srt_cleanup
        rc = lib.srt_startup()
        if rc == _srt.SRT_ERROR:
            raise _srt.LibSRTOSError(
                f"srt_startup failed: {_srt.last_error_string(lib)}")
        self._startup_done = True

        sock = lib.srt_create_socket()
        if sock == _srt.SRT_INVALID_SOCK:
            raise _srt.LibSRTOSError(
                f"srt_create_socket failed: {_srt.last_error_string(lib)}")
        self._listener_sock = sock

        self._apply_listener_options(sock)

        addr = _srt.make_sockaddr_in(self.host, self.port)
        rc = lib.srt_bind(sock, ctypes.byref(addr), _srt.SOCKADDR_IN_SIZE)
        if rc == _srt.SRT_ERROR:
            err = _srt.last_error_string(lib)
            self._cleanup_socket(sock)
            raise _srt.LibSRTOSError(f"srt_bind {self.host}:{self.port} failed: {err}")

        rc = lib.srt_listen(sock, 1)  # 单客户端
        if rc == _srt.SRT_ERROR:
            err = _srt.last_error_string(lib)
            self._cleanup_socket(sock)
            raise _srt.LibSRTOSError(f"srt_listen failed: {err}")

        self.is_running = True
        self._stop_evt.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="srt-accept",
            daemon=True,
        )
        self._accept_thread.start()
        logger.info("SRTStreamReceiver listening on %s:%d (SRT)", self.host, self.port)

    async def connect_client(self, host: str, port: int):
        """SRT 客户端模式（保留接口对齐，本工程未使用）。"""
        raise NotImplementedError(
            "SRT caller mode not implemented; iOS 端为 caller，桌面端为 listener")

    async def stop(self):
        """关闭 listener 与客户端连接，join 后台线程。"""
        if not self.is_running:
            return
        self._stop_evt.set()
        # 关闭 listener 让 srt_accept 立即返回
        with self._lock:
            if self._listener_sock != _srt.SRT_INVALID_SOCK:
                try:
                    self._lib.srt_close(self._listener_sock)
                except Exception:
                    pass
                self._listener_sock = _srt.SRT_INVALID_SOCK
            if self._client_sock != _srt.SRT_INVALID_SOCK:
                try:
                    self._lib.srt_close(self._client_sock)
                except Exception:
                    pass
                self._client_sock = _srt.SRT_INVALID_SOCK
        # join 线程（accept/recv 会因 socket 关闭而返回错误）
        if self._accept_thread and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=2.0)
        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=2.0)
        self._accept_thread = None
        self._recv_thread = None
        self.is_running = False

        if self._startup_done and self._lib is not None:
            try:
                self._lib.srt_cleanup()
            except Exception:
                pass
            self._startup_done = False

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _apply_listener_options(self, sock: int):
        """对 listener socket 应用 LIVE 低延迟选项。"""
        lib = self._lib
        yes = ctypes.c_int(1)
        latency = ctypes.c_int(self.latency_ms)
        conntimeo = ctypes.c_int(3000)
        peeridle = ctypes.c_int(5000)
        rcvtimeo = ctypes.c_int(1000)  # 1s，便于 stop 检查
        transtype = ctypes.c_int(_srt.SRTT_LIVE)

        def _set(opt, val):
            rc = lib.srt_setsockflag(sock, ctypes.c_int(opt),
                                     ctypes.byref(val), ctypes.sizeof(val))
            if rc == _srt.SRT_ERROR:
                logger.warning("SRT setsockflag opt=%d failed: %s",
                               opt, _srt.last_error_string(lib))

        # SRTO_TRANSTYPE=LIVE 自动启用：消息 API + TSBPD + TLPKTDROP 等
        _set(_srt.SRTO_TRANSTYPE, transtype)
        _set(_srt.SRTO_TSBPDMODE, yes)
        _set(_srt.SRTO_LATENCY, latency)
        _set(_srt.SRTO_TLPKTDROP, yes)
        _set(_srt.SRTO_CONNTIMEO, conntimeo)
        _set(_srt.SRTO_PEERIDLETIMEO, peeridle)
        _set(_srt.SRTO_RCVTIMEO, rcvtimeo)
        _set(_srt.SRTO_REUSEADDR, yes)

        if self.passphrase:
            pb = ctypes.c_char_p(self.passphrase.encode("utf-8"))
            klen = ctypes.c_int(self.pbkeylen)
            lib.srt_setsockflag(sock, ctypes.c_int(_srt.SRTO_PASSPHRASE),
                                pb, len(self.passphrase))
            lib.srt_setsockflag(sock, ctypes.c_int(_srt.SRTO_PBKEYLEN),
                                ctypes.byref(klen), ctypes.sizeof(klen))

    def _cleanup_socket(self, sock: int):
        if sock != _srt.SRT_INVALID_SOCK and self._lib is not None:
            try:
                self._lib.srt_close(sock)
            except Exception:
                pass

    def _accept_loop(self):
        """阻塞 srt_accept 循环。接受到客户端后启动 recv 线程。"""
        lib = self._lib
        while not self._stop_evt.is_set():
            addr = _srt.sockaddr_in()
            addrlen = ctypes.c_int(_srt.SOCKADDR_IN_SIZE)
            client = lib.srt_accept(
                ctypes.c_int32(self._listener_sock),
                ctypes.byref(addr),
                ctypes.byref(addrlen),
            )
            if client == _srt.SRT_INVALID_SOCK:
                if self._stop_evt.is_set():
                    return
                logger.warning("SRT accept failed: %s",
                               _srt.last_error_string(lib))
                # 短暂退避防止日志爆炸
                if self._stop_evt.wait(_STOP_POLL_INTERVAL):
                    return
                continue
            logger.info("SRT client connected (sock=%d)", client)
            with self._lock:
                # 单客户端：先关掉旧的（若有）
                if self._client_sock != _srt.SRT_INVALID_SOCK:
                    try:
                        lib.srt_close(self._client_sock)
                    except Exception:
                        pass
                self._client_sock = client
            self._recv_thread = threading.Thread(
                target=self._recv_loop, args=(client,),
                name="srt-recv", daemon=True,
            )
            self._recv_thread.start()

    def _recv_loop(self, sock: int):
        """阻塞 srt_recvmsg 循环。每条消息解析 28B 头 + payload。"""
        lib = self._lib
        buf = ctypes.create_string_buffer(_RECV_BUF_SIZE)
        try:
            while not self._stop_evt.is_set():
                n = lib.srt_recvmsg(
                    ctypes.c_int32(sock),
                    buf, ctypes.c_int(_RECV_BUF_SIZE),
                )
                if n == _srt.SRT_ERROR:
                    if self._stop_evt.is_set():
                        return
                    err = _srt.last_error_string(lib)
                    logger.warning("SRT recvmsg error: %s", err)
                    break
                if n < HEADER_SIZE:
                    logger.warning("SRT short message: %d bytes (< header %d)",
                                   n, HEADER_SIZE)
                    continue
                msg = ctypes.string_at(buf, n)
                if not is_valid_header(msg[:HEADER_SIZE]):
                    logger.warning("SRT invalid magic, dropping message")
                    continue
                fields = unpack_header(msg[:HEADER_SIZE])
                payload_len = fields["payload_length"]
                if payload_len != n - HEADER_SIZE:
                    # 消息 API 保证边界，理论上 payload_len 应等于 n-HEADER_SIZE
                    logger.warning("SRT payload_length mismatch: hdr=%d, msg=%d, payload=%d",
                                   payload_len, n, n - HEADER_SIZE)
                    # 截断/补零防御
                    if n - HEADER_SIZE < payload_len:
                        continue
                payload = msg[HEADER_SIZE:HEADER_SIZE + payload_len]
                self._on_message(payload, fields)
        finally:
            with self._lock:
                if self._client_sock == sock:
                    self._client_sock = _srt.SRT_INVALID_SOCK
            try:
                lib.srt_close(sock)
            except Exception:
                pass
            logger.info("SRT client disconnected (sock=%d)", sock)
            if self.on_disconnect and not self._stop_evt.is_set():
                try:
                    self.on_disconnect()
                except Exception:
                    pass

    def _on_message(self, payload: bytes, fields: dict):
        """处理一条完整 SRT 消息（= 一帧）。"""
        self._frame_count += 1
        self._recv_n += 1
        now = time.monotonic()
        if self._recv_t0 is None:
            self._recv_t0 = now
        if self._frame_count == 1:
            logger.info("SRT first frame: %dx%d bpr=%d payload=%d",
                        fields["width"], fields["height"],
                        fields["bytes_per_row"], len(payload))
        elapsed = now - self._recv_t0
        if elapsed >= 1.0:
            fps = self._recv_n / elapsed
            logger.info("SRT recv %.1f fps (total=%d, payload=%d KB)",
                        fps, self._frame_count, len(payload) // 1024)
            self._recv_t0 = now
            self._recv_n = 0
        if self.on_frame:
            try:
                self.on_frame(
                    payload,
                    fields["width"], fields["height"],
                    fields["format"], fields["bytes_per_row"],
                )
            except Exception as e:
                logger.error("on_frame callback error: %s", e)
