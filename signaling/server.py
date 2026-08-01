import argparse
import asyncio
import json
import logging
import os
import ssl
from collections import defaultdict

from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# room_id -> set of WebSocketResponse
rooms = defaultdict(set)

# web 静态文件目录
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")


def create_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


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


async def index_redirect(request):
    raise web.HTTPFound("/index.html")


def main():
    parser = argparse.ArgumentParser(description="PhoneCam Signaling Server")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument("--cert", help="HTTPS 证书路径")
    parser.add_argument("--key", help="HTTPS 私钥路径")
    parser.add_argument("--https", action="store_true", help="使用 signaling/cert.pem 和 signaling/key.pem 启用 HTTPS")
    args = parser.parse_args()

    app = web.Application()
    app.router.add_get("/ws/{room_id}", websocket_handler)

    # 提供网页端静态资源
    if os.path.isdir(WEB_DIR):
        app.router.add_get("/", index_redirect)
        app.router.add_static("/", WEB_DIR, name="web")
        logger.info("Serving web client from: %s", WEB_DIR)
    else:
        logger.warning("Web client directory not found: %s", WEB_DIR)

    ssl_context = None
    if args.https:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        cert_path = args.cert or os.path.join(base_dir, "cert.pem")
        key_path = args.key or os.path.join(base_dir, "key.pem")
        if not os.path.isfile(cert_path) or not os.path.isfile(key_path):
            logger.error("找不到证书文件，请先运行: python signaling/generate_cert.py")
            return
        ssl_context = create_ssl_context(cert_path, key_path)
        logger.info("HTTPS enabled with cert: %s", cert_path)

    logger.info("Signaling server starting on %s:%d", args.host, args.port)
    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
