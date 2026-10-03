"""H.264 解码器：基于 PyAV（ffmpeg）将 Annex-B H.264 Access Unit 解码为 RGB ndarray。

每帧接收一个 Access Unit（关键帧含 SPS/PPS+IDR，P 帧仅含 P-slice），
decode() 返回 RGB24 ndarray；解码器内部缓冲，可能某些调用返回 None
（解码器需要积累足够输入才输出帧），调用方需容忍 None。

v3 优化：
- 硬件解码：优先使用 h264_cuvid（NVIDIA NVDEC），失败回退到 h264 软解；
  4K60 软解吃力，NVDEC 可稳定 4K60 + 显著降低 CPU 占用；
- 低延迟解码：thread_type=NONE（单线程）+ low_delay=True + flags=low_delay；
- 容错解码：strict_std_compliance=-1 + err_recognition=0，避免次要错误导致丢帧；
- 断线重连后 reset()：清空解码器内部缓冲与参考帧，避免花屏。
"""
import logging
import threading
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class H264Decoder:
    """H.264 Annex-B 解码器（线程安全）。

    PyAV 的 CodecContext 不是线程安全的，所有访问加锁。
    解码器在首个关键帧（含 SPS/PPS）到达后会自动初始化参数集。

    解码器选择顺序：
    1. h264_cuvid（NVIDIA NVDEC 硬件解码，4K60 稳定，CPU 占用极低）
    2. h264（FFmpeg 软件解码，兼容性好但 4K60 吃力）
    """

    # 候选解码器：按优先级排序，第一个成功初始化的胜出
    _CODEC_CANDIDATES = ("h264_cuvid", "h264")

    def __init__(self, prefer_hardware: bool = True):
        """初始化 H.264 解码器。

        :param prefer_hardware: True=优先使用 NVDEC 硬件解码（默认）；
            False=强制使用软件解码（调试/兼容用）
        """
        self._lock = threading.Lock()
        self._codec = None
        self._codec_name: Optional[str] = None
        self._is_hardware: bool = False
        self._frame_count = 0
        self._prefer_hardware = prefer_hardware
        self._init_codec()

    def _init_codec(self):
        """初始化解码器：优先硬件，失败回退软件。"""
        candidates = self._CODEC_CANDIDATES if self._prefer_hardware else ("h264",)
        last_error: Optional[Exception] = None

        for codec_name in candidates:
            try:
                import av
                # 检查编解码器是否可用（PyAV 17+ 通过 codecs_available 列表）
                available = getattr(av, "codecs_available", None)
                if available is not None and codec_name not in available:
                    continue

                codec = av.CodecContext.create(codec_name, "r")
                # 低延迟解码配置（属性在不同 PyAV 版本可用性不同，逐项尝试）
                # thread_type="NONE" 单线程降低延迟；low_delay 在部分版本不存在
                # 注意：h264_cuvid 不支持 thread_type=FRAME（硬件解码自带并行），
                #       仅 NONE/FUTURE 可用，所以 NONE 是安全选择
                for prop, val in [
                    ("thread_type", "NONE"),
                    ("low_delay", True),
                    # flags 字符串：low_delay 减少缓冲；unaligned 允许非对齐分辨率
                    ("flags", "low_delay+unaligned"),
                    # flags2: faststart 加快首帧输出
                    ("flags2", "+faststart"),
                    # 宽松标准合规：允许非标准流（如 iOS 硬编的某些边角参数）
                    ("strict_std_compliance", -1),
                    # 关闭错误识别：避免次要错误（如 SPS 变化）导致丢帧
                    ("err_recognition", 0),
                ]:
                    try:
                        setattr(codec, prop, val)
                    except (AttributeError, TypeError, ValueError):
                        pass  # 该版本/该解码器不支持此属性，跳过

                self._codec = codec
                self._codec_name = codec_name
                self._is_hardware = codec_name.endswith("_cuvid")
                hw_tag = " [NVDEC hardware]" if self._is_hardware else " [software]"
                logger.info("H264Decoder: PyAV %s decoder initialized%s (av %s, ffmpeg %s)",
                            codec_name, hw_tag,
                            getattr(av, "__version__", "unknown"),
                            getattr(av, "ffmpeg_version_info", "unknown"))
                return
            except Exception as e:
                last_error = e
                logger.debug("H264Decoder: codec %s init failed: %s", codec_name, e)
                continue

        logger.error("H264Decoder: failed to init any decoder (last error: %s)", last_error)
        self._codec = None
        self._codec_name = None
        self._is_hardware = False

    @property
    def is_hardware(self) -> bool:
        """当前是否使用硬件解码（NVDEC）。"""
        return self._is_hardware

    @property
    def codec_name(self) -> Optional[str]:
        """当前使用的解码器名称（h264_cuvid / h264）。"""
        return self._codec_name

    def decode(self, payload: bytes) -> Optional[np.ndarray]:
        """解码一个 Access Unit，返回 RGB24 ndarray 或 None。

        :param payload: Annex-B 格式 H.264 字节流（含起始码 00 00 00 01）
        :return: shape=(H, W, 3) dtype=uint8 的 RGB ndarray；若解码器未输出帧则返回 None
        """
        if self._codec is None:
            return None
        try:
            import av
        except Exception as e:
            logger.error("H264Decoder: PyAV not available: %s", e)
            return None

        with self._lock:
            try:
                packet = av.Packet(payload)
                frames = self._codec.decode(packet)
            except Exception as e:
                # 解码错误通常是参数集变化或丢包导致，记录但不中断流
                # 频繁错误会降低日志级别，避免日志爆炸
                if self._frame_count == 0:
                    logger.debug("H264Decoder: first packet decode error (may need SPS/PPS): %s", e)
                else:
                    logger.debug("H264Decoder: decode error (will recover on next keyframe): %s", e)
                return None

            if not frames:
                # 解码器缓冲，需要更多输入
                return None

            # 一般一个 packet 对应一帧；取最后一帧（最新的）
            frame = frames[-1]
            self._frame_count += 1
            if self._frame_count == 1:
                logger.info("H264Decoder: first frame decoded %dx%d via %s",
                            frame.width, frame.height, self._codec_name)
            try:
                arr = frame.to_ndarray(format="rgb24")
                return arr
            except Exception as e:
                logger.error("H264Decoder: frame to_ndarray error: %s", e)
                return None

    def reset(self):
        """重置解码器（断线重连后调用，清空内部状态）。

        重建 codec context 以清空：
        - 内部参考帧缓冲
        - 参数集缓存（SPS/PPS）
        - 解码器流水线中的待输出帧
        这样下一个 IDR 关键帧到达时可立即重新同步，避免花屏。
        """
        with self._lock:
            self._init_codec()
            self._frame_count = 0
            logger.info("H264Decoder: decoder reset (cleared reference frames, codec=%s)",
                        self._codec_name)

    def close(self):
        with self._lock:
            if self._codec is not None:
                try:
                    self._codec.close()
                except Exception:
                    pass
                self._codec = None
                self._codec_name = None
                self._is_hardware = False
