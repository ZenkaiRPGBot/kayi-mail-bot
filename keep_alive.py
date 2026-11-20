import asyncio
import aiohttp
from aiohttp import web
from config import PORT, PING_URL, BRAND_NAME

async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text=f"{BRAND_NAME} Bot is active and healthy! 🚀", content_type="text/plain")

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "kayi-mail-bot"})

async def ping_self_loop():
    """Periodically sends an HTTP request to keep Render service awake (every 10 minutes)."""
    if not PING_URL:
        print("[keep_alive] No PING_URL configured. Self-ping loop disabled.")
        return
        
    print(f"[keep_alive] Self-ping active! Target: {PING_URL} (every 10 minutes)")
    await asyncio.sleep(30)
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(PING_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    print(f"[keep_alive] Self-ping status: {resp.status}")
            except Exception as e:
                print(f"[keep_alive] Self-ping failed: {e}")
            await asyncio.sleep(600)

async def start_web_server():
    """Starts the aiohttp web server and the self-ping task."""
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[keep_alive] Web server listening on port {PORT}")
    
    asyncio.create_task(ping_self_loop())
    return runner
