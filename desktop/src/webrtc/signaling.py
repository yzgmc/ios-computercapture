import asyncio
import json
import logging
from typing import Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)


class SignalingClient:
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

    async def send(self, message: dict):
        if self.ws and not self.ws.closed:
            await self.ws.send_str(json.dumps(message))

    async def disconnect(self):
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()
