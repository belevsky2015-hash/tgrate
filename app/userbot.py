"""
Юзербот на уже существующей сессии.

Делает две вещи:
  1. Считает курс прямо в Telegram — команда в «Избранном» или в любом чате.
  2. Слушает уведомления от @wallet и подхватывает завершённые сделки,
     чтобы калибровка набиралась сама, без ручного ввода.

Второе — главное. Модель комиссий без обратной связи всегда врёт на пару
процентов; с обратной связью она сходится за десяток сделок.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from .config import settings

log = logging.getLogger("userbot")
API = settings.api_base
HEADERS = {"X-Token": settings.panel_token} if settings.panel_token else {}

WALLET_BOTS = {"wallet", "walletbot"}

# Формулировки уведомлений Wallet меняются — правьте под то, что реально
# приходит вам. Проверить можно командой .raw в ответ на уведомление.
DEAL_PATTERNS = [
    re.compile(
        r"(?P<crypto>[\d\s.,]+)\s*(?P<casset>USDT|GRAM|TON|BTC).{0,60}?"
        r"(?P<fiat>[\d\s.,]+)\s*(?P<fasset>RUB|₽|KZT|UAH|USD|EUR|TRY)",
        re.I | re.S,
    ),
]

HELP = """Команды:
.к 50000 RUB.BANK USDT.WALLET — посчитать
.к 200 USDT.WALLET RUB.BANK
.узлы — список направлений
.статус — источники и точность
.факт <ключ маршрута> <вошло> <прогноз> <получено> — записать сделку
"""


def _num(s: str) -> float:
    return float(s.replace(" ", "").replace("\u00a0", "").replace(",", "."))


async def api_post(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{API}{path}", json=payload, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.json()


async def api_get(path: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{API}{path}", headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.json()


def fmt_quote(data: dict) -> str:
    b = data["best"]
    req = data["request"]
    lines = [
        f"{req['amount']:,.2f} {req['src']} → {b['amount_out']:,.2f} {req['dst']}",
        f"Курс {b['effective_rate']:.6f} · примерно {b['eta_min']} мин",
    ]
    if b["total_loss_pct"] is not None:
        lines.append(f"Схема съедает {b['total_loss_pct']:.2f}% от прямого курса")
    c = b["calibration"]
    lines.append(
        f"Поправка {c['factor']:.3f} ({c['basis']}, сделок: {c['deals']})"
        if c["deals"] else "Поправки нет — реальных сделок по маршруту ещё не было"
    )
    lines.append("")
    for i, s in enumerate(b["steps"], 1):
        lines.append(
            f"{i}. {s['from']} → {s['to']}: "
            f"{s['in']:,.4f} → {s['out']:,.4f} (−{s['loss_pct']:.2f}%)"
        )
    if data.get("alternatives"):
        alt = data["alternatives"][0]
        delta = (alt["amount_out"] / b["amount_out"] - 1) * 100
        lines.append(f"\nСледующий вариант хуже на {abs(delta):.2f}%")
    if data.get("missing_quotes"):
        lines.append(f"\nНет котировок: {', '.join(data['missing_quotes'][:4])}")
    lines.append(f"\nКлюч маршрута: {b['key']}")
    return "\n".join(lines)


def build() -> TelegramClient:
    if not settings.session:
        raise SystemExit("TG_SESSION пуст — положите строку сессии в .env")
    client = TelegramClient(
        StringSession(settings.session), settings.api_id, settings.api_hash
    )

    def mine(e) -> bool:
        return e.out or (settings.allowed_users and e.sender_id in settings.allowed_users)

    @client.on(events.NewMessage(pattern=r"^\.(к|k|rate)\s+(.+)"))
    async def on_rate(e):
        if not mine(e):
            return
        parts = e.pattern_match.group(2).split()
        if len(parts) < 3:
            await e.reply("Формат: .к 50000 RUB.BANK USDT.WALLET")
            return
        try:
            data = await api_post("/api/quote", {
                "amount": _num(parts[0]), "src": parts[1].upper(),
                "dst": parts[2].upper(), "routes": 3,
            })
            await e.reply(fmt_quote(data))
        except httpx.HTTPStatusError as exc:
            await e.reply(f"Не посчиталось: {exc.response.text[:200]}")

    @client.on(events.NewMessage(pattern=r"^\.(узлы|nodes)$"))
    async def on_nodes(e):
        if not mine(e):
            return
        rows = await api_get("/api/nodes")
        await e.reply("\n".join(f"{n['id']} — {n['title']}" for n in rows))

    @client.on(events.NewMessage(pattern=r"^\.(статус|status)$"))
    async def on_status(e):
        if not mine(e):
            return
        s = await api_get("/api/status")
        acc = s["accuracy"]
        vs = "\n".join(
            f"  {r['fiat']}: Wallet {r['wallet']} против Binance {r['binance']}"
            f" ({r['diff_pct']:+.2f}%)" for r in s["wallet_vs_market"]
        )
        await e.reply(
            f"P2P API Wallet: {s['quotes']['wallet_api']}\n"
            f"Котировок: {s['quotes']['wallet_keys']}, возраст {s['quotes']['age_sec']} с\n"
            f"Тариф {s['tier']}: обмен {s['exchange_fee_pct']}%, "
            f"P2P как {s['role']} — {s['p2p_fee_pct']}%\n"
            f"Средняя ошибка прогноза: "
            f"{acc['mae_pct'] if acc['deals'] else '—'}% на {acc['deals']} сделках\n"
            + (f"\nWallet против рынка:\n{vs}" if vs else "")
        )

    @client.on(events.NewMessage(pattern=r"^\.(факт|deal)\s+(.+)"))
    async def on_deal(e):
        if not mine(e):
            return
        p = e.pattern_match.group(2).split()
        if len(p) < 4:
            await e.reply("Формат: .факт <ключ> <вошло> <прогноз> <получено>")
            return
        res = await api_post("/api/deals", {
            "route_key": p[0], "pair": p[0].split(">")[0] if ">" in p[0] else "manual",
            "amount_in": _num(p[1]), "predicted_out": _num(p[2]),
            "actual_out": _num(p[3]),
        })
        await e.reply(
            f"Записал. Прогноз разошёлся на {res['error_pct']:+.2f}%. "
            f"Средняя ошибка теперь {res['accuracy']['mae_pct']}%"
        )

    @client.on(events.NewMessage(pattern=r"^\.(помощь|help)$"))
    async def on_help(e):
        if mine(e):
            await e.reply(HELP)

    @client.on(events.NewMessage(pattern=r"^\.raw$"))
    async def on_raw(e):
        """Показать сырой текст уведомления — чтобы настроить парсер."""
        if not mine(e):
            return
        src = await e.get_reply_message()
        if src:
            await e.reply(f"<pre>{src.raw_text[:1500]}</pre>", parse_mode="html")

    @client.on(events.NewMessage(incoming=True))
    async def on_wallet_notice(e):
        """Ловим завершённые сделки из уведомлений @wallet."""
        try:
            sender = await e.get_sender()
            uname = (getattr(sender, "username", "") or "").lower()
        except Exception:
            return
        if uname not in WALLET_BOTS:
            return
        text = e.raw_text or ""
        if not re.search(r"заверш|получ|complet|успешн", text, re.I):
            return
        for pat in DEAL_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            log.info("Сделка Wallet: %s", text[:200].replace("\n", " "))
            # Автозапись выключена намеренно: сначала посмотрите, что
            # ловит парсер, потом раскомментируйте вызов api_post.
            break

    return client


async def main():
    logging.basicConfig(level=logging.INFO)
    client = build()
    await client.start()
    me = await client.get_me()
    log.info("Юзербот запущен под %s", me.username or me.id)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
