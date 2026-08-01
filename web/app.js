const RESOLUTIONS = {
    1080: { width: 1920, height: 1080 },
    720: { width: 1280, height: 720 },
    480: { width: 640, height: 480 },
    240: { width: 320, height: 240 },
};

class PhoneCamWeb {
    constructor() {
        this.ws = null;
        this.pc = null;
        this.localStream = null;
        this.roomId = "room1";
        this.serverUrl = "";
        this.connected = false;

        this.els = {
            localVideo: document.getElementById("localVideo"),
            placeholder: document.getElementById("placeholder"),
            status: document.getElementById("status"),
            serverUrl: document.getElementById("serverUrl"),
            roomId: document.getElementById("roomId"),
            resolution: document.getElementById("resolution"),
            fps: document.getElementById("fps"),
            fpsValue: document.getElementById("fpsValue"),
            volume: document.getElementById("volume"),
            volumeValue: document.getElementById("volumeValue"),
            connectBtn: document.getElementById("connectBtn"),
            disconnectBtn: document.getElementById("disconnectBtn"),
            log: document.getElementById("log"),
        };

        this.init();
    }

    log(message) {
        const line = `[${new Date().toLocaleTimeString()}] ${message}`;
        console.log(line);
        if (this.els.log) {
            this.els.log.textContent += line + "\n";
            this.els.log.scrollTop = this.els.log.scrollHeight;
        }
    }

    init() {
        // 默认使用当前页面的 host 作为信令服务器
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const defaultUrl = `${protocol}//${window.location.host}`;
        this.els.serverUrl.value = defaultUrl;

        this.checkSecureContext();

        this.els.connectBtn.addEventListener("click", () => this.connect());
        this.els.disconnectBtn.addEventListener("click", () => this.disconnect());

        this.els.fps.addEventListener("input", (e) => {
            this.els.fpsValue.textContent = e.target.value;
        });

        this.els.volume.addEventListener("input", (e) => {
            this.els.volumeValue.textContent = e.target.value;
            this.updateVolume();
        });

        this.els.resolution.addEventListener("change", () => {
            if (this.connected) {
                this.restartMedia();
            }
        });

        this.els.fps.addEventListener("change", () => {
            if (this.connected) {
                this.restartMedia();
            }
        });
    }

    checkSecureContext() {
        if (!window.isSecureContext) {
            this.setStatus("需要 HTTPS", "error");
            this.log("错误：当前不是安全上下文。请通过 https:// 或 localhost 访问。");
        }
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            this.setStatus("浏览器不支持", "error");
            this.log("错误：当前浏览器不支持 getUserMedia。请使用 Safari（iOS）或 Chrome（Android/桌面）。");
        }
    }

    setStatus(text, type = "") {
        this.els.status.textContent = text;
        this.els.status.className = "status " + type;
    }

    getConstraints() {
        const res = RESOLUTIONS[this.els.resolution.value];
        const fps = parseInt(this.els.fps.value, 10);
        return {
            video: {
                width: { ideal: res.width },
                height: { ideal: res.height },
                frameRate: { ideal: fps },
            },
            audio: {
                echoCancellation: true,
                noiseSuppression: true,
            },
        };
    }

    getFallbackConstraints() {
        return {
            video: { facingMode: { ideal: "environment" } },
            audio: true,
        };
    }

    translateMediaError(err) {
        switch (err.name) {
            case "NotAllowedError":
                return "摄像头/麦克风权限被拒绝。请在 Safari 设置中允许访问。";
            case "NotFoundError":
                return "找不到摄像头或麦克风设备。";
            case "NotReadableError":
                return "摄像头/麦克风被其他应用占用或硬件异常。";
            case "OverconstrainedError":
                return "设备不支持所选分辨率/帧率，将尝试降低要求。";
            case "SecurityError":
                return "安全上下文错误，请通过 HTTPS 访问并信任证书。";
            default:
                return `获取媒体失败: ${err.name}: ${err.message}`;
        }
    }

    async startMedia() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error("浏览器不支持摄像头/麦克风访问");
        }

        const constraints = this.getConstraints();
        this.log("尝试获取媒体: " + JSON.stringify(constraints));

        try {
            this.localStream = await navigator.mediaDevices.getUserMedia(constraints);
        } catch (err) {
            this.log(`首次获取失败: ${err.name}`);
            if (err.name === "OverconstrainedError" || err.name === "NotFoundError") {
                this.log("尝试使用默认媒体约束...");
                this.localStream = await navigator.mediaDevices.getUserMedia(this.getFallbackConstraints());
            } else {
                throw err;
            }
        }

        this.log("媒体获取成功，轨道: " +
            this.localStream.getTracks().map((t) => `${t.kind}(${t.label})`).join(", "));

        this.els.localVideo.srcObject = this.localStream;
        this.els.localVideo.classList.add("active");
        this.els.placeholder.classList.add("hidden");
        this.updateVolume();
    }

    updateVolume() {
        if (!this.localStream) return;
        const volume = parseInt(this.els.volume.value, 10) / 100;
        this.localStream.getAudioTracks().forEach((track) => {
            track._volume = volume;
        });
        this.els.localVideo.volume = volume;
    }

    async restartMedia() {
        if (this.localStream) {
            this.localStream.getTracks().forEach((track) => track.stop());
        }
        await this.startMedia();
        if (this.pc) {
            const senders = this.pc.getSenders();
            this.localStream.getTracks().forEach((track) => {
                const sender = senders.find((s) => s.track && s.track.kind === track.kind);
                if (sender) {
                    sender.replaceTrack(track);
                } else {
                    this.pc.addTrack(track, this.localStream);
                }
            });
        }
    }

    async connect() {
        this.els.connectBtn.disabled = true;
        this.setStatus("连接中...", "connecting");
        this.log("开始连接...");

        try {
            this.serverUrl = this.els.serverUrl.value.trim();
            this.roomId = this.els.roomId.value.trim() || "room1";

            await this.startMedia();
            await this.connectSignaling();
            await this.createPeerConnection();

            this.connected = true;
            this.els.disconnectBtn.disabled = false;
            this.setStatus("已连接", "connected");
            this.log("连接成功");
        } catch (err) {
            const message = err.name ? this.translateMediaError(err) : (err.message || "连接失败");
            console.error(err);
            this.log("连接失败: " + message);
            this.setStatus(message, "error");
            this.els.connectBtn.disabled = false;
            await this.cleanup();
        }
    }

    connectSignaling() {
        return new Promise((resolve, reject) => {
            const wsUrl = `${this.serverUrl}/ws/${this.roomId}`;
            this.log("连接信令服务器: " + wsUrl);
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                this.log("WebSocket 已连接");
                resolve();
            };

            this.ws.onerror = (err) => {
                this.log("WebSocket 错误: " + JSON.stringify(err));
                reject(new Error("无法连接信令服务器"));
            };

            this.ws.onclose = () => {
                this.log("WebSocket 已关闭");
                if (this.connected) {
                    this.setStatus("连接已断开", "error");
                    this.cleanup();
                }
            };

            this.ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data);
                    this.log("收到信令: " + msg.type);
                    this.handleSignalingMessage(msg);
                } catch (err) {
                    console.error("Invalid signaling message:", event.data);
                }
            };
        });
    }

    async createPeerConnection() {
        this.pc = new RTCPeerConnection({
            iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
        });

        this.pc.onicecandidate = (event) => {
            if (event.candidate) {
                this.sendSignaling({
                    type: "ice",
                    candidate: event.candidate.candidate,
                    sdpMid: event.candidate.sdpMid,
                    sdpMLineIndex: event.candidate.sdpMLineIndex,
                });
            }
        };

        this.pc.onconnectionstatechange = () => {
            this.log("PeerConnection 状态: " + this.pc.connectionState);
            if (this.pc.connectionState === "connected") {
                this.setStatus("WebRTC 已连接", "connected");
            } else if (["failed", "disconnected", "closed"].includes(this.pc.connectionState)) {
                this.setStatus("WebRTC 连接断开", "error");
            }
        };

        this.localStream.getTracks().forEach((track) => {
            this.pc.addTrack(track, this.localStream);
        });

        const offer = await this.pc.createOffer();
        await this.pc.setLocalDescription(offer);
        this.sendSignaling({ type: "offer", sdp: offer.sdp });
        this.log("已发送 offer");
    }

    async handleSignalingMessage(msg) {
        console.log("Received:", msg);

        if (msg.type === "answer") {
            await this.pc.setRemoteDescription(new RTCSessionDescription({
                type: "answer",
                sdp: msg.sdp,
            }));
        } else if (msg.type === "ice") {
            await this.pc.addIceCandidate(new RTCIceCandidate({
                candidate: msg.candidate,
                sdpMid: msg.sdpMid,
                sdpMLineIndex: msg.sdpMLineIndex,
            }));
        }
    }

    sendSignaling(msg) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(msg));
        }
    }

    async disconnect() {
        this.setStatus("断开中...", "connecting");
        await this.cleanup();
        this.setStatus("未连接", "");
        this.els.connectBtn.disabled = false;
        this.els.disconnectBtn.disabled = true;
    }

    async cleanup() {
        this.connected = false;

        if (this.pc) {
            this.pc.close();
            this.pc = null;
        }

        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }

        if (this.localStream) {
            this.localStream.getTracks().forEach((track) => track.stop());
            this.localStream = null;
        }

        this.els.localVideo.srcObject = null;
        this.els.localVideo.classList.remove("active");
        this.els.placeholder.classList.remove("hidden");
    }
}

new PhoneCamWeb();
