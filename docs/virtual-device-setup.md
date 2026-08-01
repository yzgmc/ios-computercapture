# 虚拟设备配置指南

## Windows

### 虚拟摄像头

推荐使用 **OBS Studio** 自带的 Virtual Camera：

1. 下载并安装 OBS Studio：https://obsproject.com/
2. 启动 OBS，点击「启动虚拟摄像头」
3. PhoneCam 会自动检测到 OBS Virtual Camera 并输出视频

或使用 **UnityCapture** 作为更轻量的方案。

### 虚拟麦克风

推荐使用 **VB-Cable**：

1. 下载：https://vb-audio.com/Cable/
2. 安装后，系统会新增「CABLE Input」和「CABLE Output」设备
3. 在 PhoneCam 中点击「启动虚拟麦克风」，音频将输出到 CABLE Input
4. 在其他应用中选择「CABLE Output (VB-Audio Virtual Cable)」作为麦克风

## macOS

### 虚拟摄像头

- **OBS Virtual Camera**（macOS 10.15+）
- 或使用 **Camo Studio** 等第三方方案

### 虚拟麦克风

推荐使用 **BlackHole**：

1. 下载：https://existential.audio/blackhole/
2. 安装后，系统会新增「BlackHole 2ch」音频设备
3. 在 PhoneCam 中点击「启动虚拟麦克风」
4. 在其他应用中选择「BlackHole 2ch」作为麦克风输入

## 注意事项

- 虚拟摄像头和麦克风需要管理员权限安装驱动
- macOS 可能需要额外授权「屏幕录制」和「麦克风」权限
- 如果找不到虚拟设备，请检查驱动是否正确安装
