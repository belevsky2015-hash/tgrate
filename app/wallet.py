"""
Telegram Wallet: официальный P2P API + модель комиссий.

Тарифы сверены с help.ru.wallet.tg 27.09.2026 (статьи 73 «Комиссии, лимиты
и курсы» от 23.09, 82 «Комиссия за сделки» — тариф с 10.08, 893 «Тарифы»)
и docs.wallet.tg/p2p. Кошелёк их меняет, поэтому все числа вынесены сюда
константами, а не размазаны по коду.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass

import httpx

log = logging.getLogger("wallet")

P2P_URL = "https://p2p.walletbot.me/p2p/integration-api/v1/item/online"

# --- Комиссии --------------------------------------------------------------

# P2P Маркет: платит только мейкер и только с успешной сделки. Ставка
# зависит от фиата. Тейкер (откликается на чужое объявление) не платит
# ничего — объём в объявлении на продажу уже за вычетом комиссии мейкера.
# Статус «Надёжный продавец» снижает ставку; считаем по обычной, пока
# владелец не получил статус.
_P2P_MAKER_FEE = {"RUB": (1.6, 2.0)}          # (надёжный продавец, остальные)
for _f in ("AMD AZN BRL BYN EUR GEL HKD INR KGS KZT LKR "
           "TRY UAH USD UYU ZMW").split():
    _P2P_MAKER_FEE[_f] = (0.9, 1.2)
P2P_TAKER_FEE_PCT = 0.0

# Порог статуса «Надёжный продавец» (help.ru.wallet.tg, статья 129):
# не меньше 250 успешных сделок и не ниже 95% успешно завершённых.
RELIABLE_EXECUTE_RATE = 0.95
RELIABLE_ORDERS = 250

# Внутренний обмен («Торговля»/«Обменять») — зависит от тарифа.
EXCHANGE_FEE_PCT = {"basic": 0.9, "trader": 0.09}
TRADER_TIER_MONTHLY_VOLUME_USD = 50_000

# Лимиты обмена, в долларовом эквиваленте.
EXCHANGE_LIMITS_USD = {
    "default": (1.4, 25_000),        # USDT–GRAM больше не исключение
    "btc": (3.1, 25_000),
    "usdt_btc": (3.1, 100_000),
    "usdt_sol": (1.4, 50_000),
    "usdt_eth": (1.4, 100_000),
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
CARD_SELL_FEE_PCT = 3.5    # продажа крипты на карту; в графе нет — курс не виден
CARD_BUY_FREE_LIMIT_EUR = {"basic": 300, "trader": 10_000}


def exchange_fee(tier: str = "basic") -> float:
    return EXCHANGE_FEE_PCT.get(tier, EXCHANGE_FEE_PCT["basic"])


def p2p_fee(role: str, fiat: str) -> float:
    """role: 'maker' — размещаем объявление, 'taker' — откликаемся на чужое."""
    if role != "maker":
        return P2P_TAKER_FEE_PCT
    return _P2P_MAKER_FEE.get(fiat.upper(), (0.0, 0.0))[1]


def fee_vars(tier: str, role: str) -> dict[str, float]:
    """Переменные для fees.yaml: ${exchange_fee}, ${p2p_fee_rub} и т.д."""
    out = {"exchange_fee": exchange_fee(tier)}
    for fiat in ("RUB", "KZT", "UAH", "USD", "EUR", "TRY"):
        out[f"p2p_fee_{fiat.lower()}"] = p2p_fee(role, fiat)
    return out


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
    order_num: int = 0          # успешных сделок у мейкера
    payment_period: int = 0     # минут на оплату
    user_id: str = ""           # у одного мейкера бывает несколько объявлений

    @property
    def reliable(self) -> bool:
        return self.merchant_level == "TRUSTED_MERCHANT" or (
            self.execute_rate >= RELIABLE_EXECUTE_RATE
            and self.order_num >= RELIABLE_ORDERS
        )

    def capacity(self) -> float:
        """Сколько крипты можно взять одной сделкой: объём и максимум."""
        return min(self.volume, self.max_fiat / self.price) if self.price > 0 else 0.0

    def row(self) -> dict:
        return {
            "nickname": self.nickname, "price": self.price,
            "min_fiat": self.min_fiat, "max_fiat": self.max_fiat,
            "volume": self.volume, "execute_rate": self.execute_rate,
            "orders": self.order_num, "level": self.merchant_level,
            "auto_accept": self.auto_accept,
            "payment_min": self.payment_period, "payments": self.payments,
        }


# Подписи способов оплаты. Ключ — код из P2P API. Сам список способов берётся
# из живого стакана (WalletP2P.methods), отсюда только названия; кода, которого
# здесь нет, панель покажет как есть.
PAYMENT_NAMES = {
    "sbp": "СБП", "sberbankru": "Сбербанк", "tinkoff": "Т-Банк",
    "alfabank": "Альфа-Банк", "vtbbankru": "ВТБ", "ozon": "Озон Банк",
    "yandex": "Яндекс Банк", "raiffeisenru": "Райффайзен (РФ)",
    "gazprombank": "Газпромбанк", "psbank": "ПСБ", "sovcombank": "Совкомбанк",
    "rosselhoz": "Россельхозбанк", "akbarsbank": "Ак Барс", "otpbank": "ОТП Банк",
    "yoomoney": "ЮMoney", "international_transfer": "Международный перевод",
    "dushanbecitybank": "Душанбе Сити", "sepainstant": "SEPA Instant",
    "sepaeu": "SEPA", "wise": "Wise", "revolut": "Revolut", "n26": "N26",
    "zen": "ZEN", "paysera": "Paysera", "skrill": "Skrill", "neteller": "Neteller",
    "advcash": "AdvCash", "payoneer": "Payoneer", "paypal": "PayPal",
    "visadirect": "Visa Direct", "mastercardsend": "Mastercard Send",
    "westernunion": "Western Union", "moneygram": "MoneyGram",
    "bankofgeorgia": "Bank of Georgia", "tbcbank": "TBC Bank",
    "raiffeisen": "Raiffeisen", "privatbank": "ПриватБанк", "monobank": "monobank",
    "pumb": "ПУМБ", "abank": "А-Банк", "sensebank": "Sense Bank",
    "oschadbank": "Ощадбанк", "ukrsibbank": "УкрСиббанк", "sportbank": "Sportbank",
    "kaspibank": "Kaspi", "halykbank": "Halyk", "ffinbank": "Freedom Bank",
    "fortebank": "ForteBank", "jysanbank": "Jusan", "centercreditbank": "БЦК",
    "eurasianbank": "Евразийский банк", "homecreditbankkz": "Home Credit (KZ)",
    "altynbank": "Altyn Bank", "sberbank": "Сбербанк (KZ)", "visaalias": "Visa Alias",
    "smp": "СМП", "simply": "Simply", "ziraat": "Ziraat", "vakifbank": "VakıfBank",
    "garanti": "Garanti BBVA", "isbank": "İş Bankası", "akbank": "Akbank",
    "denizbank": "DenizBank", "halkbank": "Halkbank", "kuveytturk": "Kuveyt Türk",
    "albaraka": "Albaraka", "onb": "QNB", "ozan": "Ozan",
}


def fill(pool: list[Ad], crypto_amount: float) -> tuple[float, float, int]:
    """
    Набирает объём по объявлениям в порядке pool. Каждый кусок — отдельная
    сделка, и она обязана влезть в лимиты объявления: меньше minAmount
    открыть нельзя, больше maxAmount и доступного объёма — тоже. Без этой
    проверки объявление «ровно 100 000 ₽» по лучшей цене попадало в расчёт
    на 1000 USDT и давало курс, которого на такой сумме не существует.
    Возвращает (фиата всего, крипты набрано, сделок).
    """
    # ponytail: жадно по цене. Если крупный кусок оставит остаток меньше
    # минимума всех прочих, будет недобор, хотя другой раскрой прошёл бы.
    left, cost, got, used = crypto_amount, 0.0, 0.0, 0
    for a in pool:
        take = min(left, a.capacity())
        if take <= 0 or take * a.price < a.min_fiat:
            continue
        cost += take * a.price
        got += take
        used += 1
        left -= take
        if left <= 1e-9:
            break
    return cost, got, used


def top_offers(pool: list[Ad], crypto_amount: float, n: int = 3) -> list[dict]:
    """
    Надёжные мейкеры, которые закроют всю сумму ОДНОЙ сделкой — то, что
    реально можно открыть прямо сейчас. pool уже отсортирован в нашу пользу.
    """
    out, seen = [], set()
    for a in pool:
        if not a.reliable or a.capacity() < crypto_amount:
            continue
        if crypto_amount * a.price < a.min_fiat:
            continue
        who = a.user_id or a.nickname
        if who in seen:
            continue
        seen.add(who)
        out.append(a.row())
        if len(out) == n:
            break
    return out


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
                        order_num=int(it.get("orderNum") or 0),
                        payment_period=int(it.get("paymentPeriod") or 0),
                        user_id=str(it.get("userId") or ""),
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

    def _usable(self, ads: list[Ad], fiat: str | None = None,
                payments: list[str] | None = None) -> list[Ad]:
        """Лимиты по сумме здесь не проверяются — это делает fill().
        payments — способы, выбранные в запросе; None — из настроек."""
        allowed = (self._payments_for(fiat) if payments is None
                   else [p.lower() for p in payments])
        out = []
        for a in ads:
            if a.execute_rate < self.min_execute_rate:
                continue
            if not a.is_online:
                continue
            if self.merchants_only and a.merchant_level not in (
                    "MERCHANT", "TRUSTED_MERCHANT"):
                continue
            if allowed and not set(a.payments) & set(allowed):
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
        payments: list[str] | None = None,
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
        pool = self._usable(raw, fiat, payments)
        pool.sort(key=lambda a: a.price, reverse=(direction == "sell"))

        # Прогноз строится из тех же продавцов, что стоят в топе. Замер
        # 27.09.2026: VWAP по всему стакану дробил даже 50 USDT на 3–5 сделок
        # у непроверенных мейкеров и расходился с реальным предложением
        # надёжного продавца на 4–8%.
        top = top_offers(pool, crypto_amount)
        if top:
            # 1) один надёжный продавец берёт всю сумму — ровно его цена
            price, got, used = top[0]["price"], crypto_amount, 1
            basis = "одна сделка у надёжного"
        else:
            # 2) сумма крупнее любого объявления — VWAP по надёжным
            cost, got, used = fill([a for a in pool if a.reliable], crypto_amount)
            basis = "несколько сделок у надёжных"
            if got < crypto_amount * 0.99:
                # 3) надёжных не хватает — весь стакан, с пометкой
                cost, got, used = fill(pool, crypto_amount)
                basis = "не хватает надёжных — весь стакан"
            # Стакан не закрывает сумму — котировки нет, маршрут выпадет.
            # Процент допуска — на неточность оценки глубины в _usd_depth.
            if got < crypto_amount * 0.99:
                return None
            price = cost / got
        shortfall = 1 - got / crypto_amount if crypto_amount else 0

        best = pool[0].price if pool else price
        return {
            "price": price,
            "basis": basis,
            "best_price": best,
            "slippage_pct": abs(price / best - 1) * 100 if best else 0.0,
            "ads_used": used,
            "ads_total": len(pool),
            "filled_pct": round((1 - shortfall) * 100, 1),
            "depth_crypto": crypto_amount,
            "top": top,
        }

    async def methods(self, client: httpx.AsyncClient, fiat: str,
                      direction: str) -> list[dict]:
        """Способы оплаты, которые сейчас есть в стакане у живых мейкеров,
        по числу объявлений. Список конечный и настоящий, а не справочник."""
        side = self.side_buy if direction == "buy" else self.side_sell
        ads = [a for a in await self.ads(client, "USDT", fiat, side)
               if a.is_online and a.execute_rate >= self.min_execute_rate]
        cnt = Counter(p for a in ads for p in set(a.payments))
        return [{"code": p, "name": PAYMENT_NAMES.get(p, p), "ads": n}
                for p, n in cnt.most_common()]

    async def quotes_for(
        self, client: httpx.AsyncClient, fiats: list[str], crypto_amount: float,
        payments: dict[str, list[str]] | None = None,
    ) -> tuple[dict[str, float], dict[str, dict]]:
        """Ключи для графа маршрутов и разбор стакана по каждой ноге."""
        out: dict[str, float] = {}
        detail: dict[str, dict] = {}
        for fiat in fiats:
            for direction in ("buy", "sell"):
                q = await self.quote(client, "USDT", fiat, direction, crypto_amount,
                                     (payments or {}).get(fiat))
                if not q:
                    continue
                if direction == "buy":
                    # фиат -> USDT: сколько USDT за 1 фиат
                    out[f"wallet:USDT/{fiat}:ask"] = 1 / q["price"]
                else:
                    out[f"wallet:USDT/{fiat}:bid"] = q["price"]
                detail[f"{fiat}:{direction}"] = q
        return out, detail
