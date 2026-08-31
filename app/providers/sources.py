from __future__ import annotations

import httpx

from .base import Provider, vwap

SPOT_SYMBOLS = ["TONUSDT", "BTCUSDT", "ETHUSDT", "EURUSDT", "USDCUSDT"]


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
    P2P-стакан Binance. Служит и самостоятельным маршрутом, и опорой,
    когда стакан Wallet недоступен.
    """

    name = "binance_p2p"
    ttl = 60
    PAIRS = [("RUB", ["TinkoffNew", "RosBankNew", "RaiffeisenBank"]),
             ("KZT", []), ("UAH", []), ("TRY", [])]
    URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

    def __init__(self, depth_usdt: float = 3000):
        super().__init__()
        self.depth_usdt = depth_usdt

    async def _side(self, client, fiat, trade_type, pay_types) -> list[tuple[float, float]]:
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
        levels = []
        for a in r.json().get("data", []):
            adv = a["adv"]
            price = float(adv["price"])
            vol = float(adv["surplusAmount"])
            if float(adv.get("minSingleTransAmount", 0)) > self.depth_usdt * price:
                continue
            levels.append((price, vol))
        return levels

    async def fetch(self, client: httpx.AsyncClient) -> dict[str, float]:
        out: dict[str, float] = {}
        for fiat, pay in self.PAIRS:
            try:
                # BUY-объявления = мы покупаем USDT = ask (фиат за 1 USDT)
                asks = await self._side(client, fiat, "BUY", pay)
                bids = await self._side(client, fiat, "SELL", pay)
            except Exception:
                continue
            a = vwap(asks, self.depth_usdt)
            b = vwap(bids, self.depth_usdt)
            if a:
                out[f"binance_p2p:USDT/{fiat}:ask"] = 1 / a   # фиат -> USDT
            if b:
                out[f"binance_p2p:USDT/{fiat}:bid"] = b       # USDT -> фиат
        return out


class FiatRef(Provider):
    """Официальный курс ЦБ — только как ориентир, не для расчёта маршрута."""

    name = "fiat"
    ttl = 3600

    async def fetch(self, client: httpx.AsyncClient) -> dict[str, float]:
        r = await client.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=8)
        r.raise_for_status()
        v = r.json()["Valute"]
        out = {}
        for code in ("USD", "EUR", "KZT", "UAH", "TRY"):
            if code in v:
                rate = v[code]["Value"] / v[code]["Nominal"]
                out[f"fiat:{code}/RUB:mid"] = rate
                out[f"fiat:{code}/RUB:ask"] = 1 / (rate * 1.02)
                out[f"fiat:{code}/RUB:bid"] = rate * 0.98
        return out
