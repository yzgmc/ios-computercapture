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
        };

        this.init();
    }

    init() {
        // 默认使用当前页面的 host 作为信令服务器
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const defaultUrl = `${protocol}//${window.location.host}`;
        this.els.serverUrl.value = defaultUrl;

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
                facingMode: { ideal: "environment" },
            },
            audio: {
                echoCancellation: true,
                noiseSuppression: true,
            },
        };
    }

    async startMedia() {
        try {
            const constraints = this.getConstraints();
            this.localStream = await navigator.mediaDevices.getUserMedia(constraints);
            this.els.localVideo.srcObject = this.localStream;
            this.els.localVideo.classList.add("active");
            this.els.placeholder.classList.add("hidden");
            this.updateVolume();
        } catch (err) {
            console.error("getUserMedia error:", err);
            throw new Error("无法访问摄像头或麦克风，请检查权限设置");
        }
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

        try {
            this.serverUrl = this.els.serverUrl.value.trim();
            this.roomId = this.els.roomId.value.trim() || "room1";

            await this.startMedia();
            await this.connectSignaling();
            await this.createPeerConnection();

            this.connected = true;
            this.els.disconnectBtn.disabled = false;
            this.setStatus("已连接", "connected");
        } catch (err) {
            console.error(err);
            this.setStatus(err.message || "连接失败", "error");
            this.els.connectBtn.disabled = false;
            await this.cleanup();
        }
    }

    connectSignaling() {
        return new Promise((resolve, reject) => {
            const wsUrl = `${this.serverUrl}/ws/${this.roomId}`;
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                console.log("WebSocket connected:", wsUrl);
                resolve();
            };

            this.ws.onerror = (err) => {
                console.error("WebSocket error:", err);
                reject(new Error("无法连接信令服务器"));
            };

            this.ws.onclose = () => {
                if (this.connected) {
                    this.setStatus("连接已断开", "error");
                    this.cleanup();
                }
            };

            this.ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data);
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
            console.log("PeerConnection state:", this.pc.connectionState);
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
