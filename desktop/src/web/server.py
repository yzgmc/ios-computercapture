"""网页模式接收端：HTTPS 站点 + WebSocket 视频/音频通道。

为什么是 HTTPS + WebSocket 而不是裸 TCP/UDP：
- 浏览器的 getUserMedia（摄像头/麦克风）只在安全上下文下可用，
  局域网 http://192.168.x.x 不是安全上下文，必须 HTTPS。
- 浏览器无法建立裸 TCP/UDP 连接，可用的只有 HTTP/WebSocket/WebRTC。
  WebSocket 建立在 HTTPS 之上（wss://），天然复用同一张证书，
  且能直接承载二进制帧，与本工程既有的 RAW1 / AUD1 协议零成本对接。

帧协议与 iOS 端完全一致：
- 视频：28B RAW1 头 + payload（format 0=BGRA / 10=JPEG / 20=H264）
- 音频：16B AUD1 头 + PCM16LE payload
因此桌面端现有的解码链路（H264Decoder / PIL / numpy）与虚拟设备输出全部复用。
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Optional, Callable

logger = logging.getLogger(__name__)

DEFAULT_WEB_PORT = 8443

_STATIC_DIR = "static"


class WebStreamReceiver:
    """网页端接收器（HTTPS listener + WebSocket 帧通道）。

    接口与 :class:`RawStreamReceiver` 对齐（start/stop/on_frame），
    便于上层 app.py 用同一套多模式切换逻辑。
    """

    def __init__(self,
                 host: str = "0.0.0.0",
                 port: int = DEFAULT_WEB_PORT,
                 on_frame: Optional[Callable] = None,
                 on_audio_packet: Optional[Callable] = None,
                 on_client: Optional[Callable] = None,
                 on_disconnect: Optional[Callable] = None,
                 on_state: Optional[Callable[[str, str], None]] = None,
                 cert_dir: Optional[str] = None):
        self.host = host
        self.port = port
        self.on_frame = on_frame
        self.on_audio_packet = on_audio_packet
        self.on_client = on_client
        self.on_disconnect = on_disconnect
        self.on_state = on_state or (lambda lvl, msg: None)
        self.cert_dir = cert_dir

        self.is_running = False
        self.cert_path: Optional[str] = None
        self.fingerprint: Optional[str] = None

        self._runner = None          # aiohttp AppRunner
        self._video_ws = None        # 当前视频推流连接
        self._audio_ws = None
        self._local_ips: list = []
        self._frame_count = 0
        self._audio_packets = 0
        self._recv_t0 = None
        self._recv_n = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def _log(self, level: str, msg: str):
        try:
            self.on_state(level, msg)
        except Exception:
            pass

    async def start(self):
        """启动 HTTPS 站点。证书按需生成（首次会稍慢）。"""
        if self.is_running:
            return
        try:
            from aiohttp import web
        except ImportError as e:
            raise RuntimeError(
                "网页模式需要 aiohttp，请安装：pip install aiohttp") from e

        from .certs import ensure_cert, fingerprint, get_local_ips

        # 证书生成是同步且可能耗时（RSA 2048），丢到线程池避免卡 UI
        loop = asyncio.get_running_loop()
        self.cert_path, key_path = await loop.run_in_executor(
            None, ensure_cert, self.cert_dir)
        self.fingerprint = await loop.run_in_executor(
            None, fingerprint, self.cert_path)

        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(self.cert_path, key_path)

        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/app.js", self._handle_static)
        app.router.add_get("/audio-worklet.js", self._handle_static)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/ws/video", self._handle_video_ws)
        app.router.add_get("/ws/audio", self._handle_audio_ws)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port,
                           ssl_context=ssl_ctx)
        await site.start()

        self.is_running = True
        self._local_ips = get_local_ips()
        urls = " / ".join(f"https://{ip}:{self.port}"
                          for ip in self._local_ips if ip != "127.0.0.1")
        self._log("info", f"网页端已启动 {urls or f'https://localhost:{self.port}'}")
        logger.info("WebStreamReceiver listening on %s:%d (HTTPS)",
                    self.host, self.port)

    async def stop(self):
        if self._runner is None:
            self.is_running = False
            return
        for ws in (self._video_ws, self._audio_ws):
            if ws is not None:
                try:
                    await ws.close(code=1001, message=b"server shutdown")
                except Exception:
                    pass
        self._video_ws = None
        self._audio_ws = None
        try:
            await self._runner.cleanup()
        except Exception as e:
            logger.warning("Web runner cleanup error: %s", e)
        self._runner = None
        self.is_running = False
        logger.info("WebStreamReceiver stopped")

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #

    @staticmethod
    def _static_path(name: str) -> str:
        import os
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            _STATIC_DIR, name)

    async def _handle_index(self, request):
        from aiohttp import web
        try:
            with open(self._static_path("index.html"), "r", encoding="utf-8") as f:
                body = f.read()
        except OSError as e:
            return web.Response(status=500, text=f"index.html 读取失败: {e}")
        return web.Response(text=body, content_type="text/html")

    async def _handle_static(self, request):
        from aiohttp import web
        name = request.path.lstrip("/")
        # 白名单，防目录遍历
        if name not in ("app.js", "audio-worklet.js"):
            return web.Response(status=404, text="not found")
        try:
            with open(self._static_path(name), "r", encoding="utf-8") as f:
                body = f.read()
        except OSError as e:
            return web.Response(status=500, text=f"{name} 读取失败: {e}")
        return web.Response(text=body, content_type="application/javascript")

    async def _handle_health(self, request):
        from aiohttp import web
        import json
        return web.Response(
            text=json.dumps({
                "status": "ok",
                "video_client": self._video_ws is not None,
                "audio_client": self._audio_ws is not None,
                "frames": self._frame_count,
                "audio_packets": self._audio_packets,
            }),
            content_type="application/json")

    # ------------------------------------------------------------------ #
    # WebSocket
    # ------------------------------------------------------------------ #

    async def _handle_video_ws(self, request):
        from aiohttp import web, WSMsgType
        ws = web.WebSocketResponse(max_msg_size=0)  # 0 = 不限单帧大小
        await ws.prepare(request)

        # 单客户端：新连接踢掉旧连接
        if self._video_ws is not None:
            try:
                await self._video_ws.close(code=4000, message=b"replaced")
            except Exception:
                pass
        self._video_ws = ws
        self._log("info", "网页客户端已连接（视频）")
        if self.on_client:
            try:
                self.on_client()
            except Exception:
                pass

        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    self._handle_video_frame(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("Video WS error: %s", ws.exception())
                    break
        except Exception as e:
            logger.debug("Video WS loop ended: %s", e)
        finally:
            if self._video_ws is ws:
                self._video_ws = None
            self._log("warn", "网页客户端断开（视频）")
            if self.on_disconnect:
                try:
                    self.on_disconnect()
                except Exception:
                    pass
        return ws

    async def _handle_audio_ws(self, request):
        from aiohttp import web, WSMsgType
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)

        if self._audio_ws is not None:
            try:
                await self._audio_ws.close(code=4000, message=b"replaced")
            except Exception:
                pass
        self._audio_ws = ws
        self._log("info", "网页客户端已连接（音频）")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    self._handle_audio_frame(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("Audio WS error: %s", ws.exception())
                    break
        except Exception as e:
            logger.debug("Audio WS loop ended: %s", e)
        finally:
            if self._audio_ws is ws:
                self._audio_ws = None
        return ws

    # ------------------------------------------------------------------ #
    # 帧解析（复用 RAW1 / AUD1 协议）
    # ------------------------------------------------------------------ #

    def _handle_video_frame(self, data: bytes):
        from raw_stream.protocol import (HEADER_SIZE, is_valid_header,
                                    unpack_header)
        if len(data) < HEADER_SIZE or not is_valid_header(data):
            logger.warning("Web video frame: invalid header (%d bytes)", len(data))
            return
        fields = unpack_header(data)
        payload = data[HEADER_SIZE:HEADER_SIZE + fields["payload_length"]]
        if len(payload) != fields["payload_length"]:
            logger.warning("Web video frame truncated: %d < %d",
                           len(payload), fields["payload_length"])
            return

        self._frame_count += 1
        self._recv_n += 1
        import time
        now = time.monotonic()
        if self._recv_t0 is None:
            self._recv_t0 = now
        if self._frame_count == 1:
            logger.info("Web first frame: %dx%d format=%d payload=%d",
                        fields["width"], fields["height"],
                        fields["format"], len(payload))
        elapsed = now - self._recv_t0
        if elapsed >= 1.0:
            logger.info("Web recv %.1f fps (total=%d)",
                        self._recv_n / elapsed, self._frame_count)
            self._recv_t0 = now
            self._recv_n = 0

        if self.on_frame:
            try:
                self.on_frame(payload, fields["width"], fields["height"],
                              fields["format"], fields["bytes_per_row"])
            except Exception as e:
                logger.error("Web on_frame error: %s", e)

    def _handle_audio_frame(self, data: bytes):
        from audio_stream.protocol import (HEADER_SIZE, is_valid_header,
                                      unpack_header)
        if len(data) < HEADER_SIZE or not is_valid_header(data):
            logger.warning("Web audio packet: invalid header (%d bytes)", len(data))
            return
        fields = unpack_header(data)
        payload = data[HEADER_SIZE:HEADER_SIZE + fields["payload_length"]]
        if len(payload) != fields["payload_length"]:
            return

        self._audio_packets += 1
        if self._audio_packets == 1:
            logger.info("Web first audio packet: %dHz %dch",
                        fields["sample_rate"], fields["channels"])
        if self.on_audio_packet:
            try:
                self.on_audio_packet(payload, fields["sample_rate"],
                                     fields["channels"], fields["format"],
                                     fields["seq"])
            except Exception as e:
                logger.error("Web on_audio_packet error: %s", e)
