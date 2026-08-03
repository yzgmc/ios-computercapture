"""H.264 解码器：基于 PyAV（ffmpeg）将 Annex-B H.264 Access Unit 解码为 RGB ndarray。

每帧接收一个 Access Unit（关键帧含 SPS/PPS+IDR，P 帧仅含 P-slice），
decode() 返回 RGB24 ndarray；解码器内部缓冲，可能某些调用返回 None
（解码器需要积累足够输入才输出帧），调用方需容忍 None。
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
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._codec = None
        self._frame_count = 0
        self._init_codec()

    def _init_codec(self):
        try:
            import av
            self._codec = av.CodecContext.create("h264", "r")
            # 低延迟解码配置（属性在不同 PyAV 版本可用性不同，逐项尝试）
            # thread_type="NONE" 单线程降低延迟；low_delay 在部分版本不存在
            for prop, val in [("thread_type", "NONE"), ("low_delay", True)]:
                try:
                    setattr(self._codec, prop, val)
                except (AttributeError, TypeError):
                    pass  # 该版本不支持此属性，跳过
            logger.info("H264Decoder: PyAV h264 decoder initialized (av %s)",
                        getattr(av, "__version__", "unknown"))
        except Exception as e:
            logger.error("H264Decoder: failed to init PyAV h264 decoder: %s", e)
            self._codec = None

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
                if self._frame_count == 0:
                    logger.debug("H264Decoder: first packet decode error (may need SPS/PPS): %s", e)
                else:
                    logger.warning("H264Decoder: decode error: %s", e)
                return None

            if not frames:
                # 解码器缓冲，需要更多输入
                return None

            # 一般一个 packet 对应一帧；取最后一帧（最新的）
            frame = frames[-1]
            self._frame_count += 1
            if self._frame_count == 1:
                logger.info("H264Decoder: first frame decoded %dx%d",
                            frame.width, frame.height)
            try:
                arr = frame.to_ndarray(format="rgb24")
                return arr
            except Exception as e:
                logger.error("H264Decoder: frame to_ndarray error: %s", e)
                return None

    def reset(self):
        """重置解码器（断线重连后调用，清空内部状态）。"""
        with self._lock:
            # 重新创建 codec context 以清空解码器状态
            self._init_codec()
            self._frame_count = 0

    def close(self):
        with self._lock:
            if self._codec is not None:
                try:
                    self._codec.close()
                except Exception:
                    pass
                self._codec = None
