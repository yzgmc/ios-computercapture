import asyncio
import json
import logging
from typing import Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)


class SignalingClient:
    """连接到信令服务器（内嵌或远程）的 WebSocket 客户端。"""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._receive_task: Optional[asyncio.Task] = None
        self.on_message: Optional[Callable[[dict], None]] = None

    async def connect(self, url: str, room_id: str):
        self.session = aiohttp.ClientSession()
        ws_url = f"{url}/ws/{room_id}"
        self.ws = await self.session.ws_connect(ws_url)
        self._receive_task = asyncio.create_task(self._receive_loop())
        logger.info("Connected to signaling server: %s", ws_url)

    async def _receive_loop(self):
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if self.on_message:
                            self.on_message(data)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON: %s", msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", self.ws.exception())
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    logger.info("WebSocket closed")
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Signaling receive loop error: %s", e)

    async def send(self, message: dict):
        if self.ws and not self.ws.closed:
            await self.ws.send_str(json.dumps(message))
        else:
            logger.warning("Cannot send, WebSocket is closed")

    async def disconnect(self):
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        if self.ws and not self.ws.closed:
            await self.ws.close()
        self.ws = None
        if self.session:
            await self.session.close()
        self.session = None
