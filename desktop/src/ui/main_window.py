import asyncio
import logging

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox,
    QSlider, QGroupBox, QStatusBar
)

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    connect_requested = pyqtSignal(str, str)
    disconnect_requested = pyqtSignal()
    settings_changed = pyqtSignal(dict)
    virtual_camera_toggled = pyqtSignal(bool)
    virtual_audio_toggled = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PhoneCam - iPhone 摄像头共享")
        self.setMinimumSize(960, 640)

        self._setup_ui()
        self._setup_signals()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        # 左侧：视频预览
        self.video_label = QLabel("等待连接...")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet("background-color: #1a1a1a; color: #ffffff;")
        self.video_label.setMinimumSize(640, 480)
        main_layout.addWidget(self.video_label, stretch=3)

        # 右侧：控制面板
        control_panel = QWidget()
        control_layout = QVBoxLayout(control_panel)

        # 连接设置
        connection_group = QGroupBox("连接设置")
        connection_layout = QVBoxLayout(connection_group)

        connection_layout.addWidget(QLabel("信令服务器地址"))
        self.server_input = QLineEdit("ws://localhost:8080")
        connection_layout.addWidget(self.server_input)

        connection_layout.addWidget(QLabel("房间 ID"))
        self.room_input = QLineEdit("room1")
        connection_layout.addWidget(self.room_input)

        self.connect_button = QPushButton("连接")
        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.setEnabled(False)
        connection_layout.addWidget(self.connect_button)
        connection_layout.addWidget(self.disconnect_button)

        control_layout.addWidget(connection_group)

        # 视频参数
        video_group = QGroupBox("视频参数")
        video_layout = QVBoxLayout(video_group)

        video_layout.addWidget(QLabel("分辨率"))
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems(["1920x1080", "1280x720", "640x480", "320x240"])
        video_layout.addWidget(self.resolution_combo)

        video_layout.addWidget(QLabel("帧率"))
        self.fps_slider = QSlider(Qt.Orientation.Horizontal)
        self.fps_slider.setRange(15, 60)
        self.fps_slider.setValue(30)
        self.fps_label = QLabel("30 fps")
        video_layout.addWidget(self.fps_slider)
        video_layout.addWidget(self.fps_label)

        control_layout.addWidget(video_group)

        # 音频参数
        audio_group = QGroupBox("音频参数")
        audio_layout = QVBoxLayout(audio_group)

        audio_layout.addWidget(QLabel("音量"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_label = QLabel("80%")
        audio_layout.addWidget(self.volume_slider)
        audio_layout.addWidget(self.volume_label)

        control_layout.addWidget(audio_group)

        # 虚拟设备
        device_group = QGroupBox("虚拟设备")
        device_layout = QVBoxLayout(device_group)

        self.virtual_camera_button = QPushButton("启动虚拟摄像头")
        self.virtual_camera_button.setCheckable(True)
        device_layout.addWidget(self.virtual_camera_button)

        self.virtual_audio_button = QPushButton("启动虚拟麦克风")
        self.virtual_audio_button.setCheckable(True)
        device_layout.addWidget(self.virtual_audio_button)

        control_layout.addWidget(device_group)

        control_layout.addStretch()
        main_layout.addWidget(control_panel, stretch=1)

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")

    def _setup_signals(self):
        self.connect_button.clicked.connect(self._on_connect)
        self.disconnect_button.clicked.connect(self._on_disconnect)

        self.resolution_combo.currentTextChanged.connect(self._on_settings_changed)
        self.fps_slider.valueChanged.connect(self._on_fps_changed)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)

        self.virtual_camera_button.toggled.connect(self.virtual_camera_toggled.emit)
        self.virtual_audio_button.toggled.connect(self.virtual_audio_toggled.emit)

    def _on_connect(self):
        url = self.server_input.text().strip()
        room = self.room_input.text().strip()
        if url and room:
            self.connect_requested.emit(url, room)
            self.connect_button.setEnabled(False)
            self.disconnect_button.setEnabled(True)

    def _on_disconnect(self):
        self.disconnect_requested.emit()
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)

    def _on_fps_changed(self, value: int):
        self.fps_label.setText(f"{value} fps")
        self._on_settings_changed()

    def _on_volume_changed(self, value: int):
        self.volume_label.setText(f"{value}%")
        self._on_settings_changed()

    def _on_settings_changed(self):
        width, height = map(int, self.resolution_combo.currentText().split("x"))
        settings = {
            "width": width,
            "height": height,
            "fps": self.fps_slider.value(),
            "volume": self.volume_slider.value() / 100.0,
        }
        self.settings_changed.emit(settings)

    def update_video_frame(self, frame):
        """frame 为 aiortc 的 VideoFrame"""
        try:
            img = frame.to_ndarray(format="rgb24")
            h, w, ch = img.shape
            bytes_per_line = ch * w
            qt_image = QImage(img.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(qt_image)
            scaled = pixmap.scaled(
                self.video_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self.video_label.setPixmap(scaled)
        except Exception as e:
            logger.error("Update video frame error: %s", e)

    def set_status(self, message: str):
        self.status_bar.showMessage(message)
