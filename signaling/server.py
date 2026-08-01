import asyncio
import json
import logging
from collections import defaultdict

from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# room_id -> set of WebSocketResponse
rooms = defaultdict(set)


async def websocket_handler(request):
    room_id = request.match_info["room_id"]
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    logger.info("Client joined room: %s", room_id)
    rooms[room_id].add(ws)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    logger.debug("Room %s received: %s", room_id, data.get("type"))
                    # 广播给房间内其他客户端
                    for peer in rooms[room_id]:
                        if peer is not ws and not peer.closed:
                            await peer.send_str(msg.data)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON in room %s: %s", room_id, msg.data)
            elif msg.type == web.WSMsgType.ERROR:
                logger.error("WebSocket error in room %s: %s", room_id, ws.exception())
    finally:
        rooms[room_id].discard(ws)
        logger.info("Client left room: %s", room_id)

    return ws


async def index(request):
    return web.Response(text="PhoneCam Signaling Server\n", content_type="text/plain")


def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws/{room_id}", websocket_handler)

    host = "0.0.0.0"
    port = 8080
    logger.info("Signaling server starting on %s:%d", host, port)
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
