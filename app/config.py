from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _f(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except ValueError:
        return default


def _payments(raw: str):
    """
    WALLET_PAYMENTS в двух формах:
      "sberbank,tinkoff"                     — один список на все валюты
      "RUB:sbp,tinkoff;EUR:sepainstant"      — свой список на валюту
    Вторая нужна потому, что методы оплаты у валют не пересекаются.
    """
    raw = raw.strip()
    if not raw:
        return []
    if ":" not in raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    out: dict[str, list[str]] = {}
    for part in raw.split(";"):
        if ":" not in part:
            continue
        fiat, methods = part.split(":", 1)
        vals = [m.strip() for m in methods.split(",") if m.strip()]
        if vals:
            out[fiat.strip().upper()] = vals
    return out


class Settings:
    fees_path = os.getenv("FEES_PATH", str(ROOT / "fees.yaml"))
    db_path = os.getenv("DB_PATH", str(ROOT / "data" / "deals.sqlite"))
    static_dir = str(ROOT / "static")

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 8080))
    panel_token = os.getenv("PANEL_TOKEN", "")        # пусто = без авторизации

    # --- Telegram Wallet ---
    wallet_api_key = os.getenv("WALLET_API_KEY", "")
    wallet_tier = os.getenv("WALLET_TIER", "basic")        # basic | trader
    wallet_role = os.getenv("WALLET_P2P_ROLE", "taker")    # taker | maker
    wallet_side_buy = os.getenv("WALLET_SIDE_WHEN_BUYING", "SELL")
    wallet_min_execute_rate = _f("WALLET_MIN_EXECUTE_RATE", 0.90)
    wallet_merchants_only = os.getenv("WALLET_MERCHANTS_ONLY", "0") == "1"
    wallet_payments = _payments(os.getenv("WALLET_PAYMENTS", ""))

    safety_pct = _f("SAFETY_PCT", 0.0)                # общий запас сверху

    # Маршруты, которые фоновый цикл раз в минуту считает точно на заданную
    # сумму — для честного графика. "USDT.WALLET>RUB.BANK:1000,RUB.BANK>KZT.BANK:50000"
    track_routes = [
        (r.split(">")[0], r.split(">")[1].split(":")[0], float(r.split(":")[1]))
        for r in os.getenv("TRACK_ROUTES", "").replace(" ", "").split(",") if r
    ]

    # Telegram
    api_id = int(os.getenv("TG_API_ID", 0) or 0)
    api_hash = os.getenv("TG_API_HASH", "")
    session = os.getenv("TG_SESSION", "")             # StringSession
    bot_token = os.getenv("TG_BOT_TOKEN", "")
    allowed_users = {
        int(x) for x in os.getenv("TG_ALLOWED_USERS", "").replace(" ", "").split(",") if x
    }
    api_base = os.getenv("API_BASE", "http://127.0.0.1:8080")


settings = Settings()
Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
