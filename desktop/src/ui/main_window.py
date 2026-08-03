import logging

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox,
    QSlider, QGroupBox, QStatusBar, QCheckBox
)

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """桌面端主窗口。

    桌面端是被动接收方：监听 TCP 5000（视频） + UDP 5001（音频）等待 iPhone 连接。
    用户可控制：水平/垂直翻转、音量、启动/停止虚拟摄像头与虚拟麦克风。
    """

    virtual_camera_toggled = pyqtSignal(bool)
    virtual_audio_toggled = pyqtSignal(bool)
    flip_changed = pyqtSignal(bool, bool)
    volume_changed = pyqtSignal(float)
    usb_mode_requested = pyqtSignal()  # 请求切换到 USB 模式
    lan_mode_requested = pyqtSignal()  # 请求切换到 LAN 模式

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PhoneCam - iPhone 摄像头共享")
        self.setMinimumSize(960, 640)

        self._setup_ui()
        self._setup_signals()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # 顶部：传输模式 + 监听端口信息
        info_group = QGroupBox("连接方式")
        info_layout = QHBoxLayout(info_group)
        info_layout.addWidget(QLabel("模式："))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["局域网 (LAN)", "USB 直连 (USB)"])
        info_layout.addWidget(self.mode_combo)
        self.usb_status_label = QLabel("未连接")
        self.usb_status_label.setStyleSheet("color: #888;")
        info_layout.addWidget(self.usb_status_label, stretch=1)
        info_layout.addWidget(QLabel("监听："))
        self.server_input = QLineEdit("TCP :5000 / UDP :5001")
        self.server_input.setReadOnly(True)
        info_layout.addWidget(self.server_input, stretch=1)
        main_layout.addWidget(info_group)

        # 中部：视频预览 + 右侧控制
        content_widget = QWidget()
        content_layout = QHBoxLayout(content_widget)

        # 左侧：视频预览
        self.video_label = QLabel("等待 iPhone 连接...")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet("background-color: #1a1a1a; color: #ffffff;")
        self.video_label.setMinimumSize(640, 480)
        content_layout.addWidget(self.video_label, stretch=3)

        # 右侧：控制面板
        control_panel = QWidget()
        control_layout = QVBoxLayout(control_panel)

        # 视频参数（分辨率由实际视频流自动同步，只读显示）
        video_group = QGroupBox("视频参数")
        video_layout = QVBoxLayout(video_group)

        video_layout.addWidget(QLabel("分辨率"))
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems(["3840x2160", "2560x1440", "1920x1080", "1280x720", "640x480", "320x240"])
        self.resolution_combo.setEnabled(False)
        video_layout.addWidget(self.resolution_combo)

        # 翻转（用户可编辑，本地控制）
        self.flip_horizontal_checkbox = QCheckBox("水平翻转")
        self.flip_vertical_checkbox = QCheckBox("垂直翻转")
        video_layout.addWidget(self.flip_horizontal_checkbox)
        video_layout.addWidget(self.flip_vertical_checkbox)

        control_layout.addWidget(video_group)

        # 音频参数（用户可编辑音量）
        audio_group = QGroupBox("音频参数")
        audio_layout = QVBoxLayout(audio_group)

        audio_layout.addWidget(QLabel("音量"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_label = QLabel("100%")
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
        content_layout.addWidget(control_panel, stretch=1)
        main_layout.addWidget(content_widget, stretch=1)

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")

    def _setup_signals(self):
        self.virtual_camera_button.toggled.connect(self.virtual_camera_toggled.emit)
        self.virtual_audio_button.toggled.connect(self.virtual_audio_toggled.emit)

        self.flip_horizontal_checkbox.toggled.connect(self._emit_flip_changed)
        self.flip_vertical_checkbox.toggled.connect(self._emit_flip_changed)
        self.volume_slider.valueChanged.connect(self._on_volume_slider_changed)

    def _emit_flip_changed(self, _checked: bool = False):
        self.flip_changed.emit(
            self.flip_horizontal_checkbox.isChecked(),
            self.flip_vertical_checkbox.isChecked(),
        )

    def _on_volume_slider_changed(self, value: int):
        self.volume_label.setText(f"{value}%")
        self.volume_changed.emit(value / 100.0)

    def set_status(self, message: str):
        self.status_bar.showMessage(message)

    def set_server_address(self, address: str):
        """显示当前桌面端监听的端口信息。"""
        self.server_input.setText(address)

    def set_actual_resolution(self, width: int, height: int):
        """根据实际收到的视频帧尺寸更新分辨率下拉框（只读）。"""
        text = f"{int(width)}x{int(height)}"
        idx = self.resolution_combo.findText(text)
        if idx >= 0:
            self.resolution_combo.setCurrentIndex(idx)
        else:
            # 不在预设列表里，临时插入
            self.resolution_combo.insertItem(0, text)
            self.resolution_combo.setCurrentIndex(0)

    def _on_mode_combo_changed(self, idx: int):
        if idx == 1:
            self.usb_mode_requested.emit()
        else:
            self.lan_mode_requested.emit()

    def set_usb_devices(self, devices: list, current_mode: str):
        """更新 USB 设备状态显示。"""
        if current_mode != "usb":
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(0)
            self.mode_combo.blockSignals(False)
            self.usb_status_label.setText("未启用")
            self.usb_status_label.setStyleSheet("color: #888;")
            return
        # USB 模式
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(1)
        self.mode_combo.blockSignals(False)
        if not devices:
            self.usb_status_label.setText("⚠ 等待 iPhone USB 连接...")
            self.usb_status_label.setStyleSheet("color: #ff8800;")
        else:
            names = [f"{d['udid'][:8]}..." for d in devices]
            bridge_ok = any("tcp" in d.get("bridges", []) for d in devices)
            if bridge_ok:
                self.usb_status_label.setText(
                    f"✓ 已连接: {' / '.join(names)}"
                )
                self.usb_status_label.setStyleSheet("color: #00cc66;")
            else:
                self.usb_status_label.setText(
                    f"⏳ 桥接中: {' / '.join(names)}"
                )
                self.usb_status_label.setStyleSheet("color: #ffaa00;")
