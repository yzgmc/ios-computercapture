import aiohttp
import asyncio
import ssl


async def test():
    ssl_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    async with aiohttp.ClientSession() as session:
        async with session.get("https://localhost:8080/index.html", ssl=ssl_ctx) as resp:
            text = await resp.text()
            print("status:", resp.status)
            print("body[:100]:", text[:100])


if __name__ == "__main__":
    asyncio.run(test())
