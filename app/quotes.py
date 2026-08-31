from __future__ import annotations

import asyncio
import time

import httpx

from .providers.sources import BinanceP2P, CexSpot, FiatRef
from .wallet import WalletP2P

FIATS = ["RUB", "KZT", "UAH", "USD", "EUR", "TRY"]


class QuoteBook:
    """
    Источник истины — P2P API Кошелька. Спот нужен для курса внутреннего
    обмена, Binance P2P — только чтобы показать, насколько Wallet
    отличается от рынка. В расчёт маршрута он не входит.
    """

    def __init__(self, wallet_api_key: str, **wallet_kwargs):
        self.wallet = WalletP2P(wallet_api_key, **wallet_kwargs) if wallet_api_key else None
        self.spot = CexSpot()
        self.bench = BinanceP2P()
        self.fiat = FiatRef()
        self.last: dict[str, float] = {}
        self.updated_at = 0.0
        self.wallet_detail: dict[str, dict] = {}

    async def refresh(self, depth_usdt: float = 1000.0) -> dict[str, float]:
        q: dict[str, float] = {}
        async with httpx.AsyncClient(follow_redirects=True) as c:
            spot, bench, fiat = await asyncio.gather(
                self.spot.quotes(c), self.bench.quotes(c), self.fiat.quotes(c),
                return_exceptions=True,
            )
            for part in (spot, bench, fiat):
                if isinstance(part, dict):
                    q.update(part)

            if self.wallet:
                w = await self.wallet.quotes_for(c, FIATS, depth_usdt)
                q.update(w)
                self.wallet_detail = getattr(self.wallet, "detail", {})

        self.last = q
        self.updated_at = time.time()
        return q

    def wallet_vs_market(self) -> list[dict]:
        """Насколько Wallet отличается от Binance P2P по каждой валюте."""
        rows = []
        for f in FIATS:
            wb = self.last.get(f"wallet:USDT/{f}:bid")
            bb = self.last.get(f"binance_p2p:USDT/{f}:bid")
            if wb and bb:
                rows.append({
                    "fiat": f,
                    "wallet": round(wb, 4),
                    "binance": round(bb, 4),
                    "diff_pct": round((wb / bb - 1) * 100, 2),
                })
        return rows

    def status(self) -> dict:
        return {
            "wallet_api": "не настроен" if not self.wallet
                          else (self.wallet.last_error or "работает"),
            "wallet_keys": len([k for k in self.last if k.startswith("wallet:")]),
            "spot_keys": len(self.spot._cache),
            "bench_keys": len(self.bench._cache),
            "updated_at": self.updated_at,
            "age_sec": round(time.time() - self.updated_at, 1) if self.updated_at else None,
        }
