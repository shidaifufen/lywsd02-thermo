"""LYWSD02 温湿度计 —— 最小 Web 服务。

只有三件事：定时读温湿度、每天校时、把历史写进 SQLite 并画成曲线。
BLE 逻辑都在 lywsd02.py，这里只做 HTTP 壳。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiohttp import web

import lywsd02

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
PORT = int(__import__("os").environ.get("PORT", "8199"))

_LOGGER = logging.getLogger("lywsd02")


async def index(request):
    return web.FileResponse(WEB_DIR / "index.html")


async def favicon(request):
    icon = WEB_DIR / "favicon.ico"
    if icon.exists():
        return web.FileResponse(icon)
    return web.Response(status=204)


def build_app(svc) -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/favicon.ico", favicon)
    for method, path, handler in lywsd02.make_routes():
        app.router.add_route(method, path, handler)
    app["svc"] = svc
    return app


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    DATA_DIR.mkdir(exist_ok=True)
    svc = lywsd02.init_service(DATA_DIR)
    svc.start()
    app = build_app(svc)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    _LOGGER.info("温湿度计服务已启动： http://127.0.0.1:%s/", PORT)
    try:
        await asyncio.Event().wait()
    finally:
        await svc.stop()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
