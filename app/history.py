from __future__ import annotations

"""
Снимки котировок для графика.

Пишем не курс конкретной пары, а весь срез сырых котировок — два десятка
чисел. Курс любого направления восстанавливается из него тем же поиском
маршрута, что и в /api/quote, поэтому история сразу есть для всех пар,
включая те, которые ни разу не открывали.

Цены в срезе — стакан на 1000 USDT, как его считает фоновый цикл. На
другой сумме цифра будет другой: глубину стакана задним числом взять
неоткуда. Поэтому для маршрутов из TRACK_ROUTES цикл раз в минуту пишет
точный итог на заданную сумму в отдельную таблицу, и график берёт его.

Срезы помечены версией модели. До MODEL = 2 (27.09.2026) цена считалась без
лимитов объявлений и расходилась с реальными продавцами до 8% — такие срезы
в график не идут: пустой график честнее неправдивого.
"""

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_snapshots (
    ts    REAL PRIMARY KEY,
    rates TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracked (
    route      TEXT NOT NULL,         -- "USDT.WALLET>RUB.BANK:1000"
    ts         REAL NOT NULL,
    amount_out REAL NOT NULL,
    route_key  TEXT NOT NULL,         -- через какие операции
    legs       TEXT NOT NULL DEFAULT '{}',  -- цена крипты на каждом шаге
    PRIMARY KEY (route, ts)
);
"""

MODEL = 2

KEEP_DAYS = 30
MIN_GAP_SEC = 60      # цикл обновления 30 сек, для графика хватит минуты


class History:
    def __init__(self, path: str):
        self.path = path
        with self._db() as c:
            c.executescript(SCHEMA)
            # таблица tracked появилась 27.09.2026 без legs — докидываем
            cols = {r[1] for r in c.execute("PRAGMA table_info(tracked)")}
            if "legs" not in cols:
                c.execute("ALTER TABLE tracked ADD COLUMN legs TEXT NOT NULL DEFAULT '{}'")
        self._last_ts = 0.0
        self._last_tracked: dict[str, float] = {}

    def _db(self):
        return sqlite3.connect(self.path)

    def record(self, rates: dict[str, float]) -> bool:
        now = time.time()
        if not rates or now - self._last_ts < MIN_GAP_SEC:
            return False
        self._last_ts = now
        with self._db() as c:
            c.execute("INSERT OR REPLACE INTO rate_snapshots(ts, rates) VALUES(?, ?)",
                      (now, json.dumps({**rates, "_model": MODEL})))
            c.execute("DELETE FROM rate_snapshots WHERE ts < ?",
                      (now - KEEP_DAYS * 86400,))
        return True

    def record_tracked(self, route: str, amount_out: float, route_key: str,
                       legs: dict[str, float] | None = None) -> bool:
        now = time.time()
        if now - self._last_tracked.get(route, 0.0) < MIN_GAP_SEC:
            return False
        self._last_tracked[route] = now
        with self._db() as c:
            c.execute("INSERT OR REPLACE INTO tracked"
                      "(route, ts, amount_out, route_key, legs) VALUES(?, ?, ?, ?, ?)",
                      (route, now, amount_out, route_key, json.dumps(legs or {})))
            c.execute("DELETE FROM tracked WHERE ts < ?", (now - KEEP_DAYS * 86400,))
        return True

    def tracked(self, route: str, hours: float) -> list[tuple[float, float, str, dict]]:
        with self._db() as c:
            rows = c.execute(
                "SELECT ts, amount_out, route_key, legs FROM tracked "
                "WHERE route = ? AND ts >= ? ORDER BY ts",
                (route, time.time() - hours * 3600),
            ).fetchall()
        return [(ts, out, key, json.loads(legs)) for ts, out, key, legs in rows]

    def snapshots(self, hours: float, limit: int = 400) -> list[tuple[float, dict]]:
        since = time.time() - hours * 3600
        with self._db() as c:
            rows = c.execute(
                "SELECT ts, rates FROM rate_snapshots WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        rows = [(ts, json.loads(r)) for ts, r in rows]
        rows = [(ts, r) for ts, r in rows if r.get("_model") == MODEL]
        # Прореживаем равномерно: гнать на телефон тысячи точек незачем,
        # экран всё равно 414 пикселей шириной.
        if len(rows) > limit:
            step = len(rows) / limit
            rows = [rows[int(i * step)] for i in range(limit)]
        return rows
