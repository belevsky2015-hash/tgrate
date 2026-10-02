from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger("providers")


class Provider:
    name = "base"
    ttl = 30  # секунд

    def __init__(self):
        self._cache: dict[str, Any] = {}
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def fetch(self, client: httpx.AsyncClient) -> dict[str, float]:
        raise NotImplementedError

    async def quotes(self, client: httpx.AsyncClient) -> dict[str, float]:
        async with self._lock:
            if self._cache and time.time() - self._cached_at < self.ttl:
                return self._cache
            try:
                data = await self.fetch(client)
                if data:
                    self._cache = data
                    self._cached_at = time.time()
            except Exception as exc:
                log.warning("%s недоступен: %s", self.name, exc)
            return self._cache

