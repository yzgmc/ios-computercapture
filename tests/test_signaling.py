"""
测试信令服务器的基本功能。

运行方式:
1. 先启动信令服务器: python signaling/server.py
2. 运行测试: python tests/test_signaling.py
"""
import asyncio
import json

import aiohttp


async def test_signaling():
    url = "http://localhost:8080/ws/test_room"

    async with aiohttp.ClientSession() as session:
        ws1 = await session.ws_connect(url)
        ws2 = await session.ws_connect(url)

        # ws1 发送消息，ws2 应该收到
        test_msg = json.dumps({"type": "offer", "sdp": "fake_sdp"})
        await ws1.send_str(test_msg)

        msg = await ws2.receive()
        assert msg.type == aiohttp.WSMsgType.TEXT
        data = json.loads(msg.data)
        assert data["type"] == "offer"
        assert data["sdp"] == "fake_sdp"

        print("信令服务器测试通过")

        await ws1.close()
        await ws2.close()


if __name__ == "__main__":
    asyncio.run(test_signaling())
