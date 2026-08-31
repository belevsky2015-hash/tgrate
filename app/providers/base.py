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


def vwap(levels: list[tuple[float, float]], need: float) -> float | None:
    """
    Средневзвешенная цена на объём `need`.
    levels — [(цена, доступный объём в базовой валюте), ...] уже отсортированы.

    Это главный источник точности: брать лучшую цену из стакана — значит
    систематически завышать курс на крупных суммах.
    """
    if not levels:
        return None
    left, cost, got = need, 0.0, 0.0
    for price, vol in levels:
        take = min(left, vol)
        cost += take * price
        got += take
        left -= take
        if left <= 1e-12:
            break
    if got <= 0:
        return None
    if left > 1e-9:
        # объёма не хватило — считаем по тому, что есть, но с наценкой
        return cost / got * 1.01
    return cost / got
