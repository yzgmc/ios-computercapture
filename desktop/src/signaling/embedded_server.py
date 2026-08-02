"""嵌入桌面客户端的信令服务器。

启动后在本地监听 WebSocket，供 iOS 端直连，无需独立部署信令服务。
同一房间内的客户端互相转发 offer/answer/ice 消息。
"""
import asyncio
import json
import logging
from collections import defaultdict
from typing import Optional, Set

from aiohttp import web, WSMsgType

logger = logging.getLogger(__name__)


class EmbeddedSignalingServer:
    """轻量信令服务器，随桌面客户端一起启动。"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        # room_id -> set of WebSocketResponse
        self._rooms: dict[str, Set[web.WebSocketResponse]] = defaultdict(set)
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._loop_task: Optional[asyncio.Task] = None

    async def start(self):
        app = web.Application()
        app.router.add_get("/ws/{room_id}", self._websocket_handler)
        app.router.add_get("/health", self._health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info("Embedded signaling server listening on %s:%d", self.host, self.port)

    async def stop(self):
        # 关闭所有现有连接
        for room in list(self._rooms.values()):
            for ws in list(room):
                try:
                    await ws.close()
                except Exception:
                    pass
            room.clear()
        self._rooms.clear()
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("Embedded signaling server stopped")

    async def _health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        room_id = request.match_info["room_id"]
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        logger.info("Client joined room: %s", room_id)
        self._rooms[room_id].add(ws)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        logger.debug("Room %s received: %s", room_id, data.get("type"))
                        # 转发给房间内其他客户端
                        for peer in list(self._rooms[room_id]):
                            if peer is not ws and not peer.closed:
                                try:
                                    await peer.send_str(msg.data)
                                except Exception as e:
                                    logger.warning("Failed to forward to peer: %s", e)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON in room %s: %s", room_id, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    logger.error("WebSocket error in room %s: %s", room_id, ws.exception())
        finally:
            self._rooms[room_id].discard(ws)
            if not self._rooms[room_id]:
                del self._rooms[room_id]
            logger.info("Client left room: %s", room_id)

        return ws
