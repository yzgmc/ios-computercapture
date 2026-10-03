import asyncio
import logging
import socket

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from qasync import asyncSlot

from ui.main_window import MainWindow
from capture.virtual_device import (
    VirtualCameraOutput, find_default_virtual_audio_device
)
from raw_stream import RawStreamReceiver, PixelFormat
try:
    from raw_stream import SRTStreamReceiver
except ImportError:  # srt_transport 导入失败（libsrt 缺失等）
    SRTStreamReceiver = None
from audio_stream import AudioStreamReceiver, AudioPlayer
from web import (
    WebStreamReceiver, DEFAULT_WEB_PORT, is_web_available, web_import_error,
)
from discovery import DiscoveryService, DISCOVERY_PORT
from usb import (
    UsbBridgeManager, is_usb_available, list_ios_devices,
    diagnose_usb, is_usbmuxd_reachable, get_windows_amds_status,
    USB_DEFAULT_TCP_PORT, USB_DEFAULT_UDP_PORT, USB_DEFAULT_AUDIO_TCP_PORT,
)

logger = logging.getLogger(__name__)

RAW_STREAM_PORT = 5000   # TCP/SRT 视频原画质
AUDIO_STREAM_PORT = 5001  # UDP 音频
WEB_HTTPS_PORT = DEFAULT_WEB_PORT  # 网页端 HTTPS（视频/音频均走 WebSocket）

# 传输模式
MODE_LAN = "lan"          # 通过 Wi-Fi / 局域网 TCP/UDP
MODE_USB = "usb"          # 通过 USB + usbmuxd 桥接
MODE_SRT = "srt"          # 通过 SRT 推流（公网/不稳定网络）
MODE_WEB = "web"          # 网页浏览器通过 HTTPS + WebSocket 推流


class PhoneCamApp(QObject):
    status_changed = pyqtSignal(str)
    raw_frame_received = pyqtSignal(bytes, int, int, int, int)

    def __init__(self):
        super().__init__()
        self.window = MainWindow()
        self.window.virtual_camera_toggled.connect(self._on_virtual_camera_toggled)
        self.window.virtual_audio_toggled.connect(self._on_virtual_audio_toggled)
        self.window.flip_changed.connect(self._on_flip_changed)
        self.window.volume_changed.connect(self._on_volume_changed)
        self.window.usb_mode_requested.connect(self.enable_usb_mode)
        self.window.lan_mode_requested.connect(self.enable_lan_mode)
        self.window.srt_mode_requested.connect(self.enable_srt_mode)
        self.window.web_mode_requested.connect(self.enable_web_mode)

        self.status_changed.connect(self.window.set_status)

        self.virtual_camera = VirtualCameraOutput()
        # 局域网自动发现服务：在 start() 中实例化并监听 UDP 50000
        self.discovery: DiscoveryService | None = None

        # UDP 音频播放器：默认输出到虚拟音频设备（VB-Cable），
        # 用户在 UI 中切换"启动虚拟麦克风"时 start/stop。
        self.audio_player = AudioPlayer(
            output_device_index=find_default_virtual_audio_device()
        )

        # 原画质 TCP 视频接收器（监听 0.0.0.0:5000，等待 iOS 推流）
        self.raw_receiver = RawStreamReceiver(
            host="0.0.0.0", port=RAW_STREAM_PORT, on_frame=self._on_raw_frame
        )

        # UDP 音频接收器（监听 0.0.0.0:5001）
        self.audio_receiver = AudioStreamReceiver(
            host="0.0.0.0", port=AUDIO_STREAM_PORT,
            on_packet=self._on_audio_packet,
        )

        self.raw_frame_received.connect(self._display_raw_frame)

        # H.264 解码器（懒初始化，仅在收到 format=H264 帧时创建）
        self._h264_decoder = None

        # USB 直连管理器（pymobiledevice3 + usbmuxd）
        self.usb_manager: UsbBridgeManager | None = None
        self.usb_devices: list[dict] = []
        self._mode = MODE_LAN  # 当前传输模式
        # 接收器 listen 在 127.0.0.1 时只接受 USB 桥接过来的连接，
        # 在 0.0.0.0 时同时接受 LAN / USB。默认 LAN 全接受。
        self._listen_host = "0.0.0.0"

    @property
    def mode(self) -> str:
        return self._mode

    def _emit_state(self, level: str, msg: str):
        # 前缀跟随当前模式，避免 SRT / Web 模式的消息被误标成 [USB]
        tag = {"lan": "[LAN]", "usb": "[USB]", "srt": "[SRT]", "web": "[WEB]"}.get(
            self._mode, "[APP]")
        prefix = {"info": f"{tag} ", "warn": f"{tag} ⚠ ", "error": f"{tag} ✗ "}.get(
            level, f"{tag} ")
        self.status_changed.emit(prefix + msg)
        logger.log({"info": logging.INFO, "warn": logging.WARNING,
                    "error": logging.ERROR, "debug": logging.DEBUG}.get(level, logging.INFO),
                   "USB: %s", msg)

    def _emit_devices(self):
        if hasattr(self.window, "set_usb_devices"):
            try:
                self.window.set_usb_devices(self.usb_devices, self._mode)
            except Exception as e:
                logger.debug("set_usb_devices error: %s", e)

    def _sync_virtual_camera_format(self, width: int, height: int):
        """按实际收到的帧尺寸同步虚拟摄像头输出格式。

        之前只在虚拟摄像头未启用时才改 width/height，导致"先启动虚拟摄像头、
        后接入视频流"时输出分辨率停留在默认 720p，与真实帧不匹配。
        update_format() 会在已启用时自动重启设备。
        """
        if width <= 0 or height <= 0:
            return
        if self.virtual_camera.width == width and self.virtual_camera.height == height:
            return
        try:
            self.virtual_camera.update_format(width, height, self.virtual_camera.fps)
            logger.info("Virtual camera format synced to %dx%d", width, height)
        except Exception as e:
            logger.warning("Virtual camera format sync failed: %s", e)

    def _on_raw_frame(self, raw: bytes, width: int, height: int,
                      pixel_format: int, bytes_per_row: int):
        """原画质帧到达（asyncio 线程），通过信号转发到主线程显示。"""
        self.raw_frame_received.emit(raw, width, height, pixel_format, bytes_per_row)

    def _display_raw_frame(self, raw: bytes, width: int, height: int,
                           pixel_format: int, bytes_per_row: int):
        """在主线程将原始帧渲染到预览控件。支持 H264(20) / JPEG(10) / BGRA(0)。"""
        try:
            import cv2
            import numpy as np
            from PIL import Image
            import io

            if pixel_format == PixelFormat.H264:
                # H.264 解码：raw 是 Annex-B Access Unit
                if self._h264_decoder is None:
                    from raw_stream.h264_decoder import H264Decoder
                    self._h264_decoder = H264Decoder()
                    if self._h264_decoder._codec is None:
                        self._emit_state("error",
                                         "H.264 解码器初始化失败，请确认已安装 av 包 (pip install av)")
                rgb = self._h264_decoder.decode(raw)
                if rgb is None:
                    return  # 解码器缓冲，等待更多输入
                if not getattr(self, "_raw_first_frame_logged", False):
                    logger.info("Raw stream first H264 frame: %dx%d payload=%d",
                                width, height, len(raw))
                    self._raw_first_frame_logged = True
                    self.window.set_actual_resolution(width, height)
                    self._sync_virtual_camera_format(width, height)
            elif pixel_format == PixelFormat.JPEG:
                # JPEG 解码：raw 是 JPEG 字节流
                try:
                    img = Image.open(io.BytesIO(raw))
                    img = img.convert("RGB")
                except Exception as e:
                    logger.warning("JPEG decode failed: %s", e)
                    return
                if not getattr(self, "_raw_first_frame_logged", False):
                    logger.info("Raw stream first JPEG frame: %dx%d payload=%d",
                                width, height, len(raw))
                    self._raw_first_frame_logged = True
                    self.window.set_actual_resolution(width, height)
                    self._sync_virtual_camera_format(width, height)
                rgb = np.array(img)
            elif pixel_format == PixelFormat.BGRA:
                expected = bytes_per_row * height
                if len(raw) < expected:
                    logger.warning("Raw payload truncated: got %d, expected %d (w=%d h=%d bpr=%d)",
                                   len(raw), expected, width, height, bytes_per_row)
                    return
                if not getattr(self, "_raw_first_frame_logged", False):
                    logger.info("Raw stream first frame: %dx%d bpr=%d payload=%d",
                                width, height, bytes_per_row, len(raw))
                    self._raw_first_frame_logged = True
                    self.window.set_actual_resolution(width, height)
                    self._sync_virtual_camera_format(width, height)
                arr = np.frombuffer(raw[:expected], dtype=np.uint8)
                stride_bytes = max(bytes_per_row, width * 4)
                arr = arr.reshape(height, stride_bytes)[:, :width * 4].reshape(height, width, 4)
                # BGRA -> BGR (供 OpenCV/Qt 使用)
                bgr = arr[:, :, :3]
                rgb = bgr[:, :, ::-1]
            else:
                logger.warning("Unsupported raw pixel format: %d", pixel_format)
                return

            # 应用翻转（与只读控件状态一致）
            if self.window.flip_horizontal_checkbox.isChecked() and self.window.flip_vertical_checkbox.isChecked():
                rgb = cv2.flip(rgb, -1)
            elif self.window.flip_horizontal_checkbox.isChecked():
                rgb = cv2.flip(rgb, 1)
            elif self.window.flip_vertical_checkbox.isChecked():
                rgb = cv2.flip(rgb, 0)
            rgb = np.ascontiguousarray(rgb)
            # 同步到虚拟摄像头
            if self.virtual_camera.enabled:
                self.virtual_camera.send_ndarray(rgb)
            h, w, _ = rgb.shape
            # 必须 copy()，否则 QImage 持有的指针在 rgb 被回收后悬空
            qt_image = QImage(rgb.copy(), w, h, 3 * w, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(qt_image)
            scaled = pixmap.scaled(
                self.window.video_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.window.video_label.setPixmap(scaled)
        except Exception as e:
            logger.error("Display raw frame error: %s", e)

    def _on_audio_packet(self, pcm: bytes, sample_rate: int, channels: int,
                         audio_format: int, seq: int):
        """UDP 音频包到达，转交 AudioPlayer 播放。在 asyncio 线程中调用。

        AudioPlayer.feed 是线程安全的（内部有锁），可直接调用。
        """
        self.audio_player.feed(pcm, sample_rate, channels, audio_format)

    def _get_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0)
            try:
                s.connect(("10.254.254.254", 1))
                ip = s.getsockname()[0]
            except Exception:
                ip = "127.0.0.1"
            finally:
                s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    async def start(self):
        """应用启动：监听 TCP 5000 视频 + UDP 5001 音频，等待 iOS 连接。"""
        local_ip = self._get_local_ip()
        self.window.set_server_address(f"TCP :{RAW_STREAM_PORT} / UDP :{AUDIO_STREAM_PORT}")

        # 1. 启动原画质 TCP 接收器
        try:
            await self.raw_receiver.start()
            self.status_changed.emit(f"视频监听 :{RAW_STREAM_PORT} (TCP) · 等待 iPhone 连接 {local_ip}")
        except Exception as e:
            logger.error("Failed to start raw stream receiver: %s", e)
            self.status_changed.emit(f"视频端口 {RAW_STREAM_PORT} 启动失败: {e}")

        # 2. 启动 UDP 音频接收器
        try:
            await self.audio_receiver.start()
            self.status_changed.emit(f"音频监听 :{AUDIO_STREAM_PORT} (UDP)")
        except Exception as e:
            logger.error("Failed to start audio stream receiver: %s", e)
            self.status_changed.emit(f"音频端口 {AUDIO_STREAM_PORT} 启动失败: {e}")

        # 3. 启动局域网自动发现（UDP 50000），应答 iPhone 端的"搜索桌面端"。
        #    DiscoveryService 之前只在 __init__ 里声明为 None 却从未实例化，
        #    导致 iOS 端 DiscoveryClient 的广播永远等不到回包。
        try:
            self.discovery = DiscoveryService(
                host_ip=local_ip,
                tcp_port=RAW_STREAM_PORT,
                udp_port=AUDIO_STREAM_PORT,
            )
            await self.discovery.start()
            logger.info("Discovery service started on UDP %d", DISCOVERY_PORT)
        except Exception as e:
            # 端口被占用等问题不应影响主流程，降级为手动填 IP
            logger.warning("Failed to start discovery service: %s", e)
            self.discovery = None
            self.status_changed.emit("自动发现未启动（端口占用），请手动填写 IP")

    @asyncSlot()
    async def enable_usb_mode(self):
        """用户点击"切换到 USB 模式"：建桥接，等待 iPhone USB 接入。

        架构：iOS 端作为 TCP 服务器监听 5000，桌面端作为 TCP 客户端连接
        PC 127.0.0.1:5000（由 UsbmuxTcpForwarder 转发到 iOS 127.0.0.1:5000）。

        工作流：
        1. 切换模式时先跑诊断（pymobiledevice3 / AMDS / usbmuxd 端口 / 设备列表），
           把结果作为状态消息展示给用户，让"连不上"有明确原因；
        2. 启动 monitor 循环（2s 轮询设备插拔）；
        3. 若已有设备接入 → 立即建桥，桥接 ready 后触发 on_bridge_ready 回调连接 receiver；
        4. 若暂无设备 → 仅启动 monitor 与 receiver 监听，等用户插上 iPhone 后
           monitor 自动建桥并通过 on_bridge_ready 触发 receiver 连接（无需用户重新点击）。
        """
        if self._mode == MODE_USB:
            return
        if not is_usb_available():
            self.status_changed.emit("USB 直连不可用：未安装 pymobiledevice3 (pip install pymobiledevice3)")
            return
        self._mode = MODE_USB
        self._emit_state("info", "切换到 USB 直连模式")

        # 1. 跑环境诊断，把结果反馈给用户（常见故障：AMDS 未运行 / 未信任设备 / 未插 iPhone）
        try:
            issues = await diagnose_usb()
            for it in issues:
                lvl = it.get("level", "info")
                msg = it.get("message", "")
                hint = it.get("hint", "")
                line = msg + (f" — {hint}" if hint else "")
                self._emit_state(lvl, line)
        except Exception as e:
            self._emit_state("warn", f"USB 诊断异常: {e}")

        # 2. 创建/复用 USB 管理器，注册 on_bridge_ready 回调
        if self.usb_manager is None:
            self.usb_manager = UsbBridgeManager(
                tcp_port=RAW_STREAM_PORT,
                udp_port=AUDIO_STREAM_PORT,
                audio_tcp_port=USB_DEFAULT_AUDIO_TCP_PORT,
                on_state=lambda lvl, msg: self._emit_state(lvl, msg),
                on_devices_changed=self._on_usb_devices_changed,
                on_bridge_ready=self._on_usb_bridge_ready,
            )
        else:
            # 复用管理器，刷新回调（避免上次设置的回调丢失）
            self.usb_manager.on_bridge_ready = self._on_usb_bridge_ready

        # 3. 启动 monitor（不主动指定 target_udid，让 monitor 自动发现并建桥）
        await self.usb_manager.start()

        # 4. 重启接收器：先绑到 127.0.0.1，但 TCP 客户端连接推迟到 on_bridge_ready 触发
        #    （若设备已接入，monitor 会立即建桥并触发回调；若无设备，等用户插上后再触发）
        await self._restart_receivers(listen_host="127.0.0.1", use_tcp_client=False, usb_pending_bridge=True)
        # 若桥接已就绪（设备已接入），主动触发一次连接
        if self.usb_manager.has_ready_bridge():
            await self._on_usb_bridge_ready(self.usb_manager._current_udid or "")
        else:
            self._emit_state("info", "等待 iPhone 通过 USB 连接（插上后自动建桥）...")
        self._emit_devices()

    @asyncSlot()
    async def enable_lan_mode(self):
        """用户切换回 LAN 模式：恢复 0.0.0.0 监听 + 关闭 USB 桥接。"""
        if self._mode == MODE_LAN:
            return
        self._mode = MODE_LAN
        self._emit_state("info", "切换到局域网模式")
        if self.usb_manager:
            await self.usb_manager.stop()
        await self._restart_receivers(listen_host="0.0.0.0")
        self._emit_devices()

    @asyncSlot()
    async def enable_srt_mode(self):
        """用户切换到 SRT 模式：用 SRTStreamReceiver 替代 TCP 接收器。

        SRT 模式下桌面端为 listener，iOS 端为 caller 主动连接。
        音频仍走 UDP（与 LAN 一致），绑定到 0.0.0.0。
        """
        if self._mode == MODE_SRT:
            return
        if SRTStreamReceiver is None:
            self.status_changed.emit(
                "SRT 模式不可用：未安装 libsrt 运行时库（设置 PHONECAM_LIBSRT_PATH 环境变量）"
            )
            # 回退 combo 到当前实际模式
            self._emit_devices()
            return
        # 预检 libsrt 运行时库：缺失时给出明确安装指引，避免切到坏状态
        try:
            from raw_stream import libsrt as _srt
            _srt.load_libsrt()
        except Exception as e:
            self.status_changed.emit(
                f"SRT 模式不可用：{e}\n"
                f"请安装 libsrt 共享库（Windows: 下载 srt.dll/libsrt.dll），"
                f"或将 dll 所在目录加入 PATH，或设置环境变量 "
                f"PHONECAM_LIBSRT_PATH=C:\\path\\to\\libsrt.dll"
            )
            self._emit_devices()
            return
        self._mode = MODE_SRT
        self._emit_state("info", "切换到 SRT 推流模式")
        if self.usb_manager:
            await self.usb_manager.stop()
        await self._restart_receivers(listen_host="0.0.0.0", use_srt=True)
        self._emit_devices()

    @asyncSlot()
    async def enable_web_mode(self):
        """用户切换到网页模式：启动 HTTPS 站点，等待浏览器推流。

        任意设备（iPhone Safari / Android Chrome / 另一台电脑）用浏览器打开
        https://<本机IP>:8443 即可采集摄像头与麦克风推过来，无需安装 App。
        """
        if self._mode == MODE_WEB:
            return
        if not is_web_available():
            self.status_changed.emit(
                f"网页模式不可用：{web_import_error()}；请执行 pip install aiohttp")
            self._emit_devices()
            return
        self._mode = MODE_WEB
        self._emit_state("info", "切换到网页模式 (HTTPS)")
        if self.usb_manager:
            await self.usb_manager.stop()
        await self._restart_receivers(listen_host="0.0.0.0", use_web=True)
        self._emit_devices()

    def _on_web_client(self):
        """网页客户端接入：重置 H.264 解码器，等下一个 IDR 重新同步。"""
        if self._h264_decoder is not None:
            try:
                self._h264_decoder.reset()
            except Exception as e:
                logger.warning("H264 decoder reset on web client failed: %s", e)

    def _on_web_disconnect(self):
        """网页客户端断开：站点继续监听，等待下一个客户端。"""
        if self._mode != MODE_WEB:
            return
        self._emit_state("warn", "网页客户端断开，等待重新连接…")
        if self._h264_decoder is not None:
            try:
                self._h264_decoder.reset()
            except Exception as e:
                logger.warning("H264 decoder reset on web disconnect failed: %s", e)

    async def _restart_receivers(self, listen_host: str,
                                 use_tcp_client: bool = False,
                                 use_srt: bool = False,
                                 use_web: bool = False,
                                 usb_pending_bridge: bool = False):
        """重启接收器，绑定到 listen_host。

        :param use_tcp_client: True=USB 模式且桥接已就绪，raw_receiver 立即作为 TCP 客户端
            连接 127.0.0.1:RAW_STREAM_PORT（forwarder 监听端口）；
            False=LAN 模式，raw_receiver 作为 TCP 服务器监听。
        :param use_srt: True=SRT 模式，使用 SRTStreamReceiver 替代 RawStreamReceiver；
            iOS 端为 caller 主动连接桌面 listener。
        :param use_web: True=网页模式，使用 WebStreamReceiver 起 HTTPS 站点；
            视频与音频都走 WebSocket，不启动独立 UDP 音频接收器。
        :param usb_pending_bridge: True=USB 模式但桥接尚未就绪（设备未接入），
            TCP 视频与音频通道都等 on_bridge_ready 回调触发后再连接。
            注意：USB 模式下音频走 TCP 5002（usbmuxd 不转发 UDP），
            因此该场景不启动 UDP 音频监听，避免收到无关数据包。
        """
        try:
            await self.raw_receiver.stop()
        except Exception:
            pass
        try:
            await self.audio_receiver.stop()
        except Exception:
            pass
        if use_web:
            # 网页模式：HTTPS 站点承载页面与两条 WebSocket 通道。
            # 视频与音频都走 WebSocket，不再占用 TCP 5000 / UDP 5001。
            try:
                self.raw_receiver = WebStreamReceiver(
                    host=listen_host, port=WEB_HTTPS_PORT,
                    on_frame=self._on_raw_frame,
                    on_audio_packet=self._on_audio_packet,
                    on_client=self._on_web_client,
                    on_disconnect=self._on_web_disconnect,
                    on_state=lambda lvl, msg: self._emit_state(lvl, msg),
                )
            except Exception as e:
                self._emit_state("error", f"网页接收器初始化失败: {e}")
                return
            self.audio_receiver = None
            try:
                await self.raw_receiver.start()
            except Exception as e:
                self._emit_state("error", f"网页服务启动失败: {e}")
                self._mode = MODE_LAN
                return
            # 把访问地址与证书指纹回显到界面，方便用手机扫码/手输
            if hasattr(self.window, "set_web_address"):
                ips = [ip for ip in getattr(self.raw_receiver, "_local_ips", [])
                       if ip != "127.0.0.1"]
                self.window.set_web_address(
                    ips, WEB_HTTPS_PORT,
                    getattr(self.raw_receiver, "fingerprint", None))
        elif use_srt:
            # SRT 模式：listener 等待 iOS caller
            try:
                self.raw_receiver = SRTStreamReceiver(
                    host=listen_host, port=RAW_STREAM_PORT,
                    on_frame=self._on_raw_frame,
                    on_disconnect=self._on_srt_disconnect,
                )
            except Exception as e:
                self._emit_state("error", f"SRT 接收器初始化失败: {e}")
                return
            self.audio_receiver = AudioStreamReceiver(
                host=listen_host, port=AUDIO_STREAM_PORT, on_packet=self._on_audio_packet
            )
            try:
                await self.raw_receiver.start()
                self._emit_state("info",
                                 f"SRT listener 已启动 :{RAW_STREAM_PORT}，等待 iPhone 推流")
            except Exception as e:
                # SRT 启动失败（常见：libsrt 缺失）时回退到 TCP 监听，
                # 保持应用可用，避免半初始化状态 + 端口冲突
                self._emit_state("error", f"SRT 接收器启动失败: {e}")
                self.raw_receiver = RawStreamReceiver(
                    host=listen_host, port=RAW_STREAM_PORT, on_frame=self._on_raw_frame,
                )
                try:
                    await self.raw_receiver.start()
                    self._emit_state("warn",
                                     "SRT 不可用，已回退到 TCP 监听 :5000（LAN 模式可用）")
                except Exception as e2:
                    self._emit_state("error", f"回退 TCP 监听失败: {e2}")
                    return
        elif usb_pending_bridge:
            # USB 模式但桥接未就绪：仅启动 UDP 音频监听（绑到 127.0.0.1，USB tethering 不可用就走 LAN），
            # TCP 视频通道由 _on_usb_bridge_ready 触发 connect_client
            self.raw_receiver = RawStreamReceiver(
                host=listen_host, port=RAW_STREAM_PORT, on_frame=self._on_raw_frame,
                on_disconnect=self._on_usb_disconnect,
            )
            self.audio_receiver = AudioStreamReceiver(
                host=listen_host, port=AUDIO_STREAM_PORT, on_packet=self._on_audio_packet
            )
            # 不调用 raw_receiver.start()，仅作为占位符等待 _on_usb_bridge_ready 重建
        else:
            self.raw_receiver = RawStreamReceiver(
                host=listen_host, port=RAW_STREAM_PORT, on_frame=self._on_raw_frame,
                on_disconnect=(self._on_usb_disconnect if use_tcp_client else None),
            )
            self.audio_receiver = AudioStreamReceiver(
                host=listen_host, port=AUDIO_STREAM_PORT, on_packet=self._on_audio_packet
            )
            if use_tcp_client:
                # USB 模式：作为客户端连接 forwarder（forwarder 已由 UsbBridgeManager 启动）。
                # forwarder 启动是异步的，需重试等待端口就绪。
                connected = False
                for attempt in range(15):
                    try:
                        await self.raw_receiver.connect_client("127.0.0.1", RAW_STREAM_PORT)
                        connected = True
                        self._emit_state("info", "USB 视频通道已连接")
                        break
                    except (ConnectionError, OSError) as e:
                        if attempt == 0:
                            self._emit_state("info",
                                             f"等待 USB 桥接就绪… ({attempt + 1}/15)")
                        elif attempt % 5 == 4:
                            self._emit_state("info",
                                             f"仍在等待 USB 桥接… ({attempt + 1}/15)")
                        await asyncio.sleep(0.5)
                if not connected:
                    self._emit_state("error", "USB 桥接连接失败：forwarder 未就绪，请确认 iOS 端已启动 USB 模式")
            else:
                try:
                    await self.raw_receiver.start()
                except Exception as e:
                    self._emit_state("error", f"重启 TCP 接收器失败: {e}")
        if use_web:
            # 音频已在 WebSocket 通道里，无需独立接收器
            pass
        elif use_tcp_client:
            # USB 模式且桥接已就绪：立即连音频 TCP 5002
            await self._connect_usb_audio(max_attempts=15, retry_delay=0.5)
        elif usb_pending_bridge:
            # 桥接尚未就绪。USB 模式下音频必须走 TCP（usbmuxd 不转发 UDP），
            # 所以此处不能启动 UDP 监听，等 _on_usb_bridge_ready 回调再连接。
            self._emit_state("info", "USB 音频通道等待桥接就绪…")
        else:
            try:
                await self.audio_receiver.start()
            except Exception as e:
                self._emit_state("error", f"重启 UDP 接收器失败: {e}")
        self._listen_host = listen_host

    async def _connect_usb_audio(self, max_attempts: int = 15,
                                 retry_delay: float = 0.5) -> bool:
        """连接 USB 音频通道（TCP 5002）。

        usbmuxd 只转发 TCP，所以 USB 模式下音频不能用 LAN 的 UDP 5001，
        必须经桥接连到 iOS 端监听的 TCP 5002，否则麦克风数据永远到不了桌面端。
        视频不受影响：失败时只提示音频不可用。
        """
        if self.audio_receiver is None:
            return False
        for attempt in range(max_attempts):
            try:
                await self.audio_receiver.connect_tcp_client(
                    "127.0.0.1", USB_DEFAULT_AUDIO_TCP_PORT)
                self._emit_state("info", "USB 音频通道已连接")
                return True
            except (ConnectionError, OSError):
                if attempt == 0:
                    self._emit_state("info", "等待 USB 音频桥接…")
                await asyncio.sleep(retry_delay)
            except Exception as e:
                self._emit_state("warn", f"USB 音频连接异常: {e}")
                break
        self._emit_state("warn", "USB 音频桥接未就绪（视频不受影响）")
        return False

    async def _on_usb_bridge_ready(self, udid: str):
        """USB 桥接就绪回调：设备已接入且 forwarder 已监听 PC 端口。

        此函数由 UsbBridgeManager 在两种场景下调用：
        1. 用户点击 USB 模式时设备已接入 → 立即建桥后触发
        2. 用户先点 USB 模式（无设备）→ 插上 iPhone 后 monitor 建桥完成时触发

        在此重建 RawStreamReceiver 为 TCP 客户端模式，连接 127.0.0.1:5000。
        """
        if self._mode != MODE_USB:
            return
        self._emit_state("info", f"USB 桥接就绪 (udid={udid[:8] if udid else '?'}...)，连接视频通道…")
        # 重建 receiver 为客户端模式
        try:
            await self.raw_receiver.stop()
        except Exception:
            pass
        self.raw_receiver = RawStreamReceiver(
            host="127.0.0.1", port=RAW_STREAM_PORT,
            on_frame=self._on_raw_frame,
            on_disconnect=self._on_usb_disconnect,
        )
        # 短重试：forwarder 刚 set listening_event，端口一定就绪，最多 3 次
        for attempt in range(3):
            try:
                await self.raw_receiver.connect_client("127.0.0.1", RAW_STREAM_PORT)
                self._emit_state("info", "USB 视频通道已连接")
                # 重置 H.264 解码器：清空参考帧，等下一帧 IDR 重新同步
                if self._h264_decoder is not None:
                    try:
                        self._h264_decoder.reset()
                    except Exception as e:
                        logger.warning("H264 decoder reset on bridge ready failed: %s", e)
                # 视频通了再连音频（TCP 5002）；音频失败不影响视频
                await self._connect_usb_audio(max_attempts=5, retry_delay=0.3)
                return
            except (ConnectionError, OSError) as e:
                self._emit_state("info", f"连接 forwarder 重试 ({attempt + 1}/3): {e}")
                await asyncio.sleep(0.3)
        self._emit_state("error", "USB 视频通道连接失败：forwarder 端口不可达")

    def _on_srt_disconnect(self):
        """SRT 客户端断开回调。与 USB 不同，SRT listener 保持开启，仅清理解码器缓存。"""
        if self._mode != MODE_SRT:
            return
        self._emit_state("warn", "SRT 推流端断开，等待重连…")
        # 重置 H.264 解码器：清空参考帧，等下一帧 IDR 重新同步
        if self._h264_decoder is not None:
            try:
                self._h264_decoder.reset()
            except Exception as e:
                logger.warning("H264 decoder reset on SRT disconnect failed: %s", e)

    async def _on_usb_devices_changed(self, devices: list):
        self.usb_devices = devices
        self._emit_devices()
        for d in devices:
            if d.get("bridges") and "tcp" in d["bridges"]:
                self._emit_state("info", f"设备就绪: {d['udid'][:8]}...")
            else:
                self._emit_state("info", f"设备断开: {d['udid'][:8]}...")

    def _on_usb_disconnect(self):
        """USB 模式下视频通道断开时触发自动重连（异步）。

        断开原因：iOS 端未启动 / app 进入后台 / USB 线缆松动 / forwarder 重启。
        重连策略：
        - 若桥接仍在（iOS app 后台/重启）→ 后台任务每 1.5s 重试 connect_client，最多 40 次（60s）；
        - 若桥接已断（USB 线缆拔出）→ 不重试，等 monitor 检测到设备重新接入时
          通过 on_bridge_ready 回调自动重建 receiver。
        """
        if self._mode != MODE_USB:
            return
        # 桥接已断（设备拔出）：不启动重连循环，等 on_bridge_ready 触发
        if self.usb_manager is None or not self.usb_manager.has_ready_bridge():
            self._emit_state("warn", "USB 视频通道断开（设备已拔出），等设备重新接入…")
            return
        self._emit_state("warn", "USB 视频通道断开，尝试重连…")
        if getattr(self, "_usb_reconnect_task", None) and not self._usb_reconnect_task.done():
            return  # 已有重连任务在跑
        self._usb_reconnect_task = asyncio.create_task(
            self._usb_reconnect_loop(), name="usb-reconnect"
        )

    async def _usb_reconnect_loop(self):
        """USB 视频通道自动重连循环（仅当桥接仍存活时）。"""
        for attempt in range(40):
            if self._mode != MODE_USB:
                return  # 已切换到 LAN 模式
            # 桥接中途断了：退出循环，等 on_bridge_ready 触发
            if self.usb_manager is None or not self.usb_manager.has_ready_bridge():
                self._emit_state("info", "USB 桥接已断开，等设备重新接入…")
                return
            try:
                # 每次重连前重建 receiver（旧的 reader/writer 已关闭）
                try:
                    await self.raw_receiver.stop()
                except Exception:
                    pass
                self.raw_receiver = RawStreamReceiver(
                    host="127.0.0.1", port=RAW_STREAM_PORT,
                    on_frame=self._on_raw_frame,
                    on_disconnect=self._on_usb_disconnect,
                )
                await self.raw_receiver.connect_client("127.0.0.1", RAW_STREAM_PORT)
                self._emit_state("info", "USB 视频通道已重连")
                # 重置 H.264 解码器：清空参考帧与参数集缓存，
                # 等待 iOS 端的下一帧 IDR（onClientConnected 已触发）重新同步，避免花屏
                if self._h264_decoder is not None:
                    try:
                        self._h264_decoder.reset()
                    except Exception as e:
                        logger.warning("H264 decoder reset on reconnect failed: %s", e)
                return
            except (ConnectionError, OSError):
                if attempt % 10 == 0:
                    self._emit_state("info", f"USB 重连中… ({attempt + 1}/40)")
                await asyncio.sleep(1.5)
        self._emit_state("error", "USB 视频通道重连失败（已尝试 60s）")

    def show(self):
        self.window.show()

    @asyncSlot(bool)
    async def _on_virtual_camera_toggled(self, enabled: bool):
        if enabled:
            self.virtual_camera.enable()
            self.status_changed.emit("虚拟摄像头已启动")
        else:
            self.virtual_camera.disable()
            self.status_changed.emit("虚拟摄像头已停止")

    @asyncSlot(bool)
    async def _on_virtual_audio_toggled(self, enabled: bool):
        if enabled:
            self.audio_player.start()
            self.status_changed.emit("虚拟麦克风已启动")
        else:
            self.audio_player.stop()
            self.status_changed.emit("虚拟麦克风已停止")

    @asyncSlot(bool, bool)
    async def _on_flip_changed(self, flip_h: bool, flip_v: bool):
        self.virtual_camera.update_flip(flip_h, flip_v)

    @asyncSlot(float)
    async def _on_volume_changed(self, volume: float):
        self.audio_player.set_volume(volume)

    async def shutdown(self):
        """应用退出时清理所有资源。"""
        if self.usb_manager:
            try:
                await self.usb_manager.stop()
            except Exception as e:
                logger.warning("USB manager stop error: %s", e)
        try:
            await self.raw_receiver.stop()
        except Exception as e:
            logger.warning("Raw receiver stop error: %s", e)
        try:
            await self.audio_receiver.stop()
        except Exception as e:
            logger.warning("Audio receiver stop error: %s", e)
        if self.discovery:
            try:
                await self.discovery.stop()
            except Exception as e:
                logger.warning("Discovery stop error: %s", e)
        try:
            self.audio_player.stop()
        except Exception as e:
            logger.warning("Audio player stop error: %s", e)
        try:
            self.virtual_camera.disable()
        except Exception as e:
            logger.warning("Virtual camera disable error: %s", e)
