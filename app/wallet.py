"""
Telegram Wallet: официальный P2P API + модель комиссий.

Тарифы сверены с help.ru.wallet.tg (страница обновлена 19.08.2026) и
docs.wallet.tg/p2p. Кошелёк их меняет, поэтому все числа вынесены сюда
константами, а не размазаны по коду.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("wallet")

P2P_URL = "https://p2p.walletbot.me/p2p/integration-api/v1/item/online"

# --- Комиссии --------------------------------------------------------------

# P2P Маркет: 0,9% и только с мейкера, только с успешной сделки.
# Тейкер (тот, кто откликается на чужое объявление) не платит ничего.
# Это самый крупный рычаг во всей схеме: 0,9% против 0.
P2P_MAKER_FEE_PCT = 0.9
P2P_TAKER_FEE_PCT = 0.0

# Внутренний обмен («Торговля»/«Обменять») — зависит от тарифа.
EXCHANGE_FEE_PCT = {"basic": 0.9, "trader": 0.09}
TRADER_TIER_MONTHLY_VOLUME_USD = 50_000

# Лимиты обмена, в долларовом эквиваленте.
EXCHANGE_LIMITS_USD = {
    "default": (1.4, 25_000),
    "btc": (3.1, 25_000),
    "usdt_gram": (1.4, 100_000),
}

# Вывод на внешний адрес. Фиксировано, от суммы не зависит —
# поэтому на мелких суммах TRC-20 убивает всю экономику.
WITHDRAW_FEE = {
    "USDT.TRC20": ("USDT", 3.5),
    "USDT.TON": ("USDT", 1.0),
    "USDT.SOL": ("USDT", 1.5),
    "USDT.ETH": ("USDT", 4.0),
    "USDC.SOL": ("USDC", 1.5),
    "USDC.ETH": ("USDC", 4.0),
    "BTC": ("BTC", 0.00008),      # плавающая, смотреть перед отправкой
    "GRAM": ("GRAM", 0.05),       # GRAM — бывший TON
}

WITHDRAW_MIN = {"USDT": 1.0, "BTC": 0.0001, "GRAM": 0.1}
DEPOSIT_MIN = {"USDT.TRC20": 0.003, "USDT.TON": 0.01, "GRAM": 0.01}

# Перевод контакту в Telegram — бесплатно. Минимумы:
CONTACT_TRANSFER_MIN = {"USDT": 0.01, "GRAM": 0.1}

# Внебиржевой ввод/вывод через платёжного партнёра.
CARD_SELL_FEE_PCT = 3.5                       # продажа крипты на карту
CARD_BUY_FREE_LIMIT_EUR = {"basic": 300, "trader": 10_000}


def exchange_fee(tier: str = "basic") -> float:
    return EXCHANGE_FEE_PCT.get(tier, EXCHANGE_FEE_PCT["basic"])


def p2p_fee(role: str) -> float:
    """role: 'maker' — размещаем объявление, 'taker' — откликаемся на чужое."""
    return P2P_MAKER_FEE_PCT if role == "maker" else P2P_TAKER_FEE_PCT


# --- Стакан P2P ------------------------------------------------------------


@dataclass
class Ad:
    price: float          # фиата за 1 единицу крипты
    volume: float         # доступно крипты
    min_fiat: float
    max_fiat: float
    execute_rate: float   # доля успешных сделок мейкера
    merchant_level: str
    is_online: bool
    auto_accept: bool
    payments: list[str]
    nickname: str

    def fits(self, fiat_amount: float) -> bool:
        return self.min_fiat <= fiat_amount <= self.max_fiat


class WalletP2P:
    """
    Единственный официальный способ получить настоящие цены Wallet.
    Ключ: Кошелёк → Deposit → P2P Market → My Profile → API Keys.
    Только чтение цен, торговать через API нельзя. Данные обновляются
    раз в 30 секунд — чаще опрашивать бессмысленно.
    """

    def __init__(
        self,
        api_key: str,
        *,
        # Сторона объявления, у которого мы ПОКУПАЕМ крипту.
        # По документации ad.side описывает действие мейкера, значит нам
        # нужны его продажи. Проверяется за минуту сравнением с приложением.
        side_when_we_buy: str = "SELL",
        min_execute_rate: float = 0.90,
        merchants_only: bool = False,
        payments: list[str] | None = None,
        ttl: int = 30,
    ):
        self.api_key = api_key
        self.side_buy = side_when_we_buy
        self.side_sell = "BUY" if side_when_we_buy == "SELL" else "SELL"
        self.min_execute_rate = min_execute_rate
        self.merchants_only = merchants_only
        # Методы оплаты: либо плоский список на все валюты (как раньше),
        # либо словарь {"EUR": [...], "RUB": [...]}. Ключ "*" — умолчание.
        # Одним списком на всё пользоваться нельзя: SEPA есть только у евро,
        # СБП только у рублей, и общий фильтр убивает одну из ног маршрута.
        if isinstance(payments, dict):
            self.payments = {k.upper(): [p.lower() for p in v] for k, v in payments.items()}
        else:
            self.payments = {"*": [p.lower() for p in (payments or [])]}
        self.ttl = ttl
        self._cache: dict[tuple, tuple[float, list[Ad]]] = {}
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    async def _request(
        self, client: httpx.AsyncClient, crypto: str, fiat: str, side: str
    ) -> list[Ad]:
        ads: list[Ad] = []
        for page in (1, 2):
            r = await client.post(
                P2P_URL,
                json={
                    "cryptoCurrency": crypto,
                    "fiatCurrency": fiat,
                    "side": side,
                    "page": page,
                    "pageSize": 50,
                },
                headers={
                    "X-API-Key": self.api_key,
                    "accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=12,
            )
            if r.status_code == 401:
                raise RuntimeError("Неверный API-ключ Wallet")
            if r.status_code == 429:
                raise RuntimeError("Лимит запросов Wallet исчерпан")
            r.raise_for_status()
            rows = r.json().get("data") or []
            for it in rows:
                ads.append(
                    Ad(
                        price=float(it["price"]),
                        volume=float(it.get("lastQuantity") or 0),
                        min_fiat=float(it.get("minAmount") or 0),
                        max_fiat=float(it.get("maxAmount") or 1e18),
                        execute_rate=float(it.get("executeRate") or 0),
                        merchant_level=it.get("merchantLevel") or "",
                        is_online=bool(it.get("isOnline")),
                        auto_accept=bool(it.get("isAutoAccept")),
                        payments=[p.lower() for p in (it.get("payments") or [])],
                        nickname=it.get("nickname") or "",
                    )
                )
            if len(rows) < 50:
                break
        return ads

    async def ads(
        self, client: httpx.AsyncClient, crypto: str, fiat: str, side: str
    ) -> list[Ad]:
        key = (crypto, fiat, side)
        async with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
            try:
                data = await self._request(client, crypto, fiat, side)
                self._cache[key] = (time.time(), data)
                self.last_error = None
                return data
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Wallet P2P %s/%s %s: %s", crypto, fiat, side, exc)
                return hit[1] if hit else []

    def _payments_for(self, fiat: str | None) -> list[str]:
        if fiat and fiat.upper() in self.payments:
            return self.payments[fiat.upper()]
        return self.payments.get("*", [])

    def _usable(self, ads: list[Ad], fiat_amount: float,
                fiat: str | None = None) -> list[Ad]:
        allowed = self._payments_for(fiat)
        out = []
        for a in ads:
            if a.execute_rate < self.min_execute_rate:
                continue
            if not a.is_online:
                continue
            if self.merchants_only and a.merchant_level != "MERCHANT":
                continue
            if allowed and not set(a.payments) & set(allowed):
                continue
            if fiat_amount and not a.fits(fiat_amount):
                # объявление не покрывает нашу сумму целиком, но может
                # покрыть часть — если его максимум ниже нашей суммы
                if a.max_fiat < fiat_amount and a.min_fiat <= a.max_fiat:
                    out.append(a)
                continue
            out.append(a)
        return out

    async def quote(
        self,
        client: httpx.AsyncClient,
        crypto: str,
        fiat: str,
        direction: str,        # "buy" — покупаем крипту, "sell" — продаём
        crypto_amount: float,
    ) -> dict | None:
        """
        Средневзвешенная цена на нужный объём, а не лучшая строка стакана.
        Брать верх стакана — это и есть та ошибка, из-за которой прогноз
        расходится с реальностью на несколько процентов при крупной сумме.
        """
        side = self.side_buy if direction == "buy" else self.side_sell
        raw = await self.ads(client, crypto, fiat, side)
        if not raw:
            return None

        # цену сортируем в нашу пользу: покупаем дешевле, продаём дороже
        pool = self._usable(raw, 0, fiat)
        pool.sort(key=lambda a: a.price, reverse=(direction == "sell"))

        left, cost, got, used = crypto_amount, 0.0, 0.0, 0
        for a in pool:
            cap_by_fiat = a.max_fiat / a.price if a.price else 0
            avail = min(a.volume, cap_by_fiat)
            if avail <= 0:
                continue
            take = min(left, avail)
            cost += take * a.price
            got += take
            used += 1
            left -= take
            if left <= 1e-9:
                break

        if got <= 0:
            return None
        price = cost / got
        shortfall = left / crypto_amount if crypto_amount else 0
        if shortfall > 0.01:
            # объёма не хватило — честно помечаем и добавляем наценку
            price *= 1.01 if direction == "buy" else 0.99

        best = pool[0].price if pool else price
        return {
            "price": price,
            "best_price": best,
            "slippage_pct": abs(price / best - 1) * 100 if best else 0.0,
            "ads_used": used,
            "ads_total": len(pool),
            "filled_pct": round((1 - shortfall) * 100, 1),
            "depth_crypto": crypto_amount,
        }

    async def quotes_for(
        self, client: httpx.AsyncClient, fiats: list[str], crypto_amount: float
    ) -> dict[str, float]:
        """Ключи для графа маршрутов."""
        out: dict[str, float] = {}
        self.detail: dict[str, dict] = {}
        for fiat in fiats:
            for direction in ("buy", "sell"):
                q = await self.quote(client, "USDT", fiat, direction, crypto_amount)
                if not q:
                    continue
                if direction == "buy":
                    # фиат -> USDT: сколько USDT за 1 фиат
                    out[f"wallet:USDT/{fiat}:ask"] = 1 / q["price"]
                else:
                    out[f"wallet:USDT/{fiat}:bid"] = q["price"]
                self.detail[f"{fiat}:{direction}"] = q
        return out
