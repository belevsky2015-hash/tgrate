#!/usr/bin/env python3
"""
Проверка перед запуском.

Главное здесь — автоматически определить, чью сторону описывает поле `side`
в объявлении Wallet. Документация об этом молчит, а ошибка переворачивает
спред и делает все расчёты стабильно неверными.

Логика простая: продавцы просят больше, покупатели предлагают меньше.
Значит у стороны, которая соответствует продаже крипты, средняя цена обязана
быть выше. Сравниваем и говорим, что поставить в .env.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.config import settings
from app.graph import bind_rates, k_best_routes, load_graph
from app.wallet import WalletP2P, exchange_fee, p2p_fee

OK, BAD, WARN = "  ok  ", " ОШИБКА ", " ! "


def line(mark: str, text: str):
    print(f"[{mark}] {text}")


async def check_wallet() -> bool:
    if not settings.wallet_api_key:
        line(BAD, "WALLET_API_KEY пуст. Ключ берётся в Кошельке: "
                  "Deposit → P2P Market → My Profile → API Keys")
        return False

    w = WalletP2P(settings.wallet_api_key, min_execute_rate=0.0)
    async with httpx.AsyncClient() as c:
        try:
            sells = await w._request(c, "USDT", "RUB", "SELL")
            buys = await w._request(c, "USDT", "RUB", "BUY")
        except Exception as exc:
            line(BAD, f"P2P API не ответил: {exc}")
            return False

    if not sells or not buys:
        line(BAD, f"Пустой стакан: side=SELL — {len(sells)} объявлений, "
                  f"side=BUY — {len(buys)}")
        return False

    line(OK, f"P2P API работает. Объявлений: SELL {len(sells)}, BUY {len(buys)}")

    top_sell = sum(a.price for a in sells[:5]) / min(5, len(sells))
    top_buy = sum(a.price for a in buys[:5]) / min(5, len(buys))
    gap = abs(top_sell / top_buy - 1) * 100

    print(f"      средняя цена по пяти лучшим: SELL {top_sell:.4f}, "
          f"BUY {top_buy:.4f}, разница {gap:.2f}%")

    if gap < 0.1:
        line(WARN, "Стороны почти неразличимы. Стакан пустой или сломан — "
                   "не угадывайте, перепроверьте позже")
        return False

    should_be = "SELL" if top_sell > top_buy else "BUY"
    if settings.wallet_side_buy == should_be:
        line(OK, f"WALLET_SIDE_WHEN_BUYING={should_be} — верно")
    else:
        line(BAD, f"WALLET_SIDE_WHEN_BUYING сейчас "
                  f"{settings.wallet_side_buy}, а должно быть {should_be}. "
                  f"Исправьте в .env, иначе спред перевёрнут")
        return False

    q = await _quote_sample(w)
    if q:
        line(OK, f"VWAP на 1000 USDT: {q['price']:.4f} RUB "
                 f"(лучшая строка {q['best_price']:.4f}, "
                 f"проскальзывание {q['slippage_pct']:.2f}%, "
                 f"объявлений задействовано {q['ads_used']})")
    return True


async def _quote_sample(w: WalletP2P):
    async with httpx.AsyncClient() as c:
        return await w.quote(c, "USDT", "RUB", "buy", 1000)


def check_graph() -> bool:
    variables = {
        "exchange_fee": exchange_fee(settings.wallet_tier),
        "p2p_fee": p2p_fee(settings.wallet_role),
    }
    nodes, edges = load_graph(settings.fees_path, variables)
    line(OK, f"Граф загружен: {len(nodes)} узлов, {len(edges)} операций")
    print(f"      тариф {settings.wallet_tier}: обмен {variables['exchange_fee']}%, "
          f"роль {settings.wallet_role}: P2P {variables['p2p_fee']}%")

    unknown = {e.src for e in edges} | {e.dst for e in edges}
    orphan = unknown - set(nodes)
    if orphan:
        line(BAD, f"Рёбра ссылаются на несуществующие узлы: {sorted(orphan)}")
        return False

    fake = {
        "wallet:USDT/RUB:ask": 1 / 78.5, "wallet:USDT/RUB:bid": 77.2,
        "cex:TONUSDT:bid": 2.1, "cex:TONUSDT:ask_inv": 1 / 2.12,
        "cex:BTCUSDT:bid": 95000, "cex:BTCUSDT:ask_inv": 1 / 95100,
        "fiat:USD/RUB:bid": 79.0, "fiat:USD/RUB:ask": 1 / 81.0,
    }
    bind_rates(edges, fake)
    r = k_best_routes(edges, "RUB.BANK", "USDT.TRC20", 10000, k=1)
    if not r:
        line(BAD, "Поиск маршрута не работает на тестовых данных")
        return False
    line(OK, f"Поиск маршрутов работает: 10 000 RUB → "
             f"{r[0].amount_out:.2f} USDT в TRON ({r[0].key})")
    return True


def check_env() -> bool:
    ok = True
    if not settings.panel_token:
        line(WARN, "PANEL_TOKEN пуст — панель без авторизации. "
                   "Допустимо только на 127.0.0.1")
    if settings.session:
        if not (settings.api_id and settings.api_hash):
            line(BAD, "Есть TG_SESSION, но нет TG_API_ID/TG_API_HASH")
            ok = False
        else:
            line(OK, "Данные юзербота на месте")
    else:
        line(WARN, "TG_SESSION пуст — юзербот не запустится, панель будет работать")
    if settings.safety_pct:
        line(WARN, f"SAFETY_PCT={settings.safety_pct}% — итог занижается намеренно")
    return ok


async def main():
    print("Проверка проекта\n")
    results = [check_env(), check_graph(), await check_wallet()]
    print()
    if all(results):
        print("Всё в порядке, можно запускать: make run")
        return 0
    print("Есть проблемы выше. Запускать рано.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
