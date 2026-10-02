from __future__ import annotations

import httpx

from ..wallet import RELIABLE_EXECUTE_RATE, Ad, fill
from .base import Provider

# TON переименован в GRAM и на биржах: TONUSDT у Binance отдаёт нулевой
# стакан, у Bybit его нет (проверено 27.09.2026).
SPOT_SYMBOLS = ["GRAMUSDT", "BTCUSDT", "ETHUSDT", "EURUSDT", "USDCUSDT"]


class CexSpot(Provider):
    """Спот. Binance основной, Bybit добирает то, чего у него нет."""

    name = "cex"
    ttl = 15

    async def fetch(self, client: httpx.AsyncClient) -> dict[str, float]:
        out: dict[str, float] = {}
        try:
            r = await client.get(
                "https://api.binance.com/api/v3/ticker/bookTicker", timeout=8
            )
            r.raise_for_status()
            book = {i["symbol"]: i for i in r.json()}
        except Exception:
            book = {}

        # Резерв срабатывал только когда Binance целиком отваливался. Но
        # символ может просто пропасть из листинга — тогда ответ приходит
        # успешный, а ключа в нём нет. Добираем недостающее у Bybit.
        if any(sym not in book for sym in SPOT_SYMBOLS):
            try:
                for sym, item in (await self._bybit(client)).items():
                    book.setdefault(sym, item)
            except Exception:
                pass

        for sym in SPOT_SYMBOLS:
            item = book.get(sym)
            if not item:
                continue
            bid, ask = float(item["bidPrice"]), float(item["askPrice"])
            if bid <= 0 or ask <= 0:
                continue
            out[f"cex:{sym}:bid"] = bid
            out[f"cex:{sym}:ask"] = ask
            out[f"cex:{sym}:bid_inv"] = 1 / bid
            out[f"cex:{sym}:ask_inv"] = 1 / ask
            out[f"cex:{sym}:mid"] = (bid + ask) / 2
        return out

    async def _bybit(self, client: httpx.AsyncClient) -> dict:
        r = await client.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "spot"},
            timeout=8,
        )
        r.raise_for_status()
        return {
            i["symbol"]: {"bidPrice": i["bid1Price"], "askPrice": i["ask1Price"]}
            for i in r.json()["result"]["list"]
        }


class BinanceP2P(Provider):
    """
    P2P-стакан Binance — только для сравнения, в маршрут не входит. Цена
    считается той же fill(), что и у Wallet: с лимитами объявлений и порогом
    исполнения «надёжного» (95%), иначе сравнение ловит выбросы вроде
    «49 грн с максимумом 35 000 грн» или 475 KZT у мейкера с 90% сделок.
    """

    name = "binance_p2p"
    ttl = 60
    PAIRS = [("RUB", ["TinkoffNew", "RosBankNew", "RaiffeisenBank"]),
             ("KZT", []), ("UAH", []), ("TRY", [])]
    URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

    def __init__(self, depth_usdt: float = 1000,
                 min_execute_rate: float = RELIABLE_EXECUTE_RATE):
        super().__init__()
        self.depth_usdt = depth_usdt          # та же глубина, что у Wallet в цикле
        self.min_execute_rate = min_execute_rate

    async def _side(self, client, fiat, trade_type, pay_types) -> list[Ad]:
        # tradeType — действие пользователя: BUY отдаёт объявления продавцов
        payload = {
            "asset": "USDT", "fiat": fiat, "tradeType": trade_type,
            "page": 1, "rows": 20, "payTypes": pay_types,
            "publisherType": None, "merchantCheck": False,
        }
        r = await client.post(self.URL, json=payload, timeout=10, headers={
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0",
        })
        r.raise_for_status()
        ads = []
        for a in r.json().get("data") or []:
            adv, who = a["adv"], a.get("advertiser") or {}
            ads.append(Ad(
                price=float(adv["price"]),
                volume=float(adv.get("surplusAmount") or 0),
                min_fiat=float(adv.get("minSingleTransAmount") or 0),
                max_fiat=float(adv.get("dynamicMaxSingleTransAmount")
                               or adv.get("maxSingleTransAmount") or 1e18),
                execute_rate=float(who.get("monthFinishRate") or 0),
                merchant_level=(who.get("userType") or "").upper(),
                is_online=True, auto_accept=False,
                payments=[], nickname=who.get("nickName") or "",
                order_num=int(who.get("monthOrderCount") or 0),
            ))
        return [a for a in ads if a.execute_rate >= self.min_execute_rate]

    def _price(self, ads: list[Ad], reverse: bool) -> float | None:
        ads.sort(key=lambda a: a.price, reverse=reverse)
        cost, got, _ = fill(ads, self.depth_usdt)
        # недобор больше процента — цена не на ту глубину, не сравниваем
        return cost / got if got >= self.depth_usdt * 0.99 else None

    async def fetch(self, client: httpx.AsyncClient) -> dict[str, float]:
        out: dict[str, float] = {}
        for fiat, pay in self.PAIRS:
            try:
                asks = await self._side(client, fiat, "BUY", pay)
                bids = await self._side(client, fiat, "SELL", pay)
            except Exception:
                continue
            a = self._price(asks, reverse=False)
            b = self._price(bids, reverse=True)
            if a:
                out[f"binance_p2p:USDT/{fiat}:ask"] = 1 / a   # фиат -> USDT
            if b:
                out[f"binance_p2p:USDT/{fiat}:bid"] = b       # USDT -> фиат
        return out
