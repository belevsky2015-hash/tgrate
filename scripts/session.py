#!/usr/bin/env python3
"""
Получить StringSession для .env.

Если у вас уже есть файл сессии (.session), скрипт возьмёт его и просто
напечатает строку — повторная авторизация не нужна. Если файла нет,
проведёт вход обычным способом.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings


async def main():
    if not (settings.api_id and settings.api_hash):
        print("Заполните TG_API_ID и TG_API_HASH в .env (my.telegram.org)")
        return 1

    existing = sorted(Path(".").glob("*.session"))
    if existing:
        name = str(existing[0])[:-8]
        print(f"Нашёл готовую сессию: {existing[0]}")
        client = TelegramClient(name, settings.api_id, settings.api_hash)
    else:
        print("Файла сессии нет, потребуется вход")
        client = TelegramClient(StringSession(), settings.api_id, settings.api_hash)

    await client.start()
    me = await client.get_me()
    print(f"\nВошли как {me.first_name} (@{me.username or me.id})")
    print("\nСтрока для TG_SESSION в .env:\n")
    print(StringSession.save(client.session))
    print("\nЭто полный доступ к аккаунту. Не пересылайте её никому.")
    await client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
