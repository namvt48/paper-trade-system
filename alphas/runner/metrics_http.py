from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import web


@dataclass
class MetricsServer:
    metrics_snapshot: Callable[[], dict[str, Any]]
    host: str = "0.0.0.0"
    port: int = 9091
    alive: bool = True
    _runner: web.AppRunner | None = None
    _site: web.TCPSite | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/metrics", self._metrics)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

    async def stop(self) -> None:
        self.alive = False
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def _health(self, request: web.Request) -> web.Response:
        if not self.alive:
            return web.json_response({"status": "stopping"}, status=503)
        snapshot = self.metrics_snapshot()
        stale_alphas = snapshot.get("stale_alphas") or []
        if stale_alphas:
            # An alpha that hasn't processed any event in far longer than
            # its own timeframe warrants -- e.g. the 2026-07-16 incident,
            # where 5 daily alphas went silent for 12+ hours with nothing
            # in the logs to surface it short of reading raw runner logs.
            return web.json_response(
                {"status": "degraded", "stale_alphas": stale_alphas}, status=503,
            )
        return web.json_response({"status": "ok"})

    async def _metrics(self, request: web.Request) -> web.Response:
        return web.json_response(self.metrics_snapshot())
