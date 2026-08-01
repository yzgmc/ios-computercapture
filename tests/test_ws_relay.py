import asyncio
import aiohttp


async def test():
    session = aiohttp.ClientSession()
    try:
        ws1 = await session.ws_connect("http://localhost:8080/ws/testroom")
        print("ws1 connected")
        ws2 = await session.ws_connect("http://localhost:8080/ws/testroom")
        print("ws2 connected")
        await ws1.send_str('{"type":"offer","sdp":"test"}')
        print("ws1 sent")
        msg = await asyncio.wait_for(ws2.receive(), timeout=5)
        print("ws2 received:", msg.data)
        await ws1.close()
        await ws2.close()
    except Exception as e:
        print("Error:", e)
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(test())
