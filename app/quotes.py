from __future__ import annotations

import asyncio
import time

import httpx

from .providers.sources import BinanceP2P, CexSpot
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
        self.last: dict[str, float] = {}
        self.updated_at = 0.0
        self.wallet_detail: dict[str, dict] = {}

    async def refresh(self, depth_usdt: float = 1000.0) -> dict[str, float]:
        q: dict[str, float] = {}
        async with httpx.AsyncClient(follow_redirects=True) as c:
            spot, bench = await asyncio.gather(
                self.spot.quotes(c), self.bench.quotes(c),
                return_exceptions=True,
            )
            for part in (spot, bench):
                if isinstance(part, dict):
                    q.update(part)

            if self.wallet:
                w, self.wallet_detail = await self.wallet.quotes_for(c, FIATS, depth_usdt)
                q.update(w)

        self.last = q
        self.updated_at = time.time()
        return q

    async def at_depth(self, depth_usdt: float,
                       payments: dict[str, list[str]] | None = None
                       ) -> tuple[dict[str, float], dict]:
        """
        Котировки Wallet под конкретную сумму. Фоновый цикл держит стакан на
        1000 USDT, а цена на 100 и на 10 000 USDT другая: у мелкой суммы
        отпадают объявления с высоким минимумом, у крупной — кончается верх
        стакана. Объявления кешируются на 30 с, поэтому лишних запросов к
        API здесь нет. self.last не трогаем — это срез для истории.
        """
        if not self.wallet:
            return self.last, {}
        async with httpx.AsyncClient(follow_redirects=True) as c:
            w, detail = await self.wallet.quotes_for(c, FIATS, depth_usdt, payments)
        # Способы оплаты выбраны в запросе: котировки Wallet из среза цикла
        # (там способы из настроек) подмешивать нельзя — фиат без объявлений
        # по выбранному способу должен остаться без цены, а не взять чужую.
        base = {k: v for k, v in self.last.items() if not k.startswith("wallet:")}
        return {**base, **w}, detail

    def wallet_vs_market(self) -> list[dict]:
        """
        Насколько Wallet отличается от Binance P2P по каждой валюте, на обе
        стороны. Обе цены — фиат за 1 USDT на одной глубине, с одними и
        теми же лимитами и порогом исполнения. Плюс — в Wallet выгоднее.
        """
        rows = []
        for f in FIATS:
            for side, key in (("buy", "ask"), ("sell", "bid")):
                w = self.last.get(f"wallet:USDT/{f}:{key}")
                b = self.last.get(f"binance_p2p:USDT/{f}:{key}")
                if not (w and b):
                    continue
                if key == "ask":            # хранится как USDT за 1 фиат
                    w, b = 1 / w, 1 / b
                diff = (b / w - 1) if key == "ask" else (w / b - 1)
                rows.append({
                    "fiat": f, "side": side,
                    "wallet": round(w, 4),
                    "binance": round(b, 4),
                    "diff_pct": round(diff * 100, 2),
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
