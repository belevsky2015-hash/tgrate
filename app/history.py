from __future__ import annotations

"""
Снимки котировок для графика.

Пишем не курс конкретной пары, а весь срез сырых котировок — два десятка
чисел. Курс любого направления восстанавливается из него тем же поиском
маршрута, что и в /api/quote, поэтому история сразу есть для всех пар,
включая те, которые ни разу не открывали.

Цены в срезе — VWAP по стакану на 1000 USDT, как их считает фоновый цикл.
На другой сумме сегодняшняя цифра будет чуть другой: комиссии график
пересчитает под запрошенный объём, а глубину стакана взять задним числом
неоткуда.
"""

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_snapshots (
    ts    REAL PRIMARY KEY,
    rates TEXT NOT NULL
);
"""

KEEP_DAYS = 30
MIN_GAP_SEC = 60      # цикл обновления 30 сек, для графика хватит минуты


class History:
    def __init__(self, path: str):
        self.path = path
        with self._db() as c:
            c.executescript(SCHEMA)
        self._last_ts = 0.0

    def _db(self):
        return sqlite3.connect(self.path)

    def record(self, rates: dict[str, float]) -> bool:
        now = time.time()
        if not rates or now - self._last_ts < MIN_GAP_SEC:
            return False
        self._last_ts = now
        with self._db() as c:
            c.execute("INSERT OR REPLACE INTO rate_snapshots(ts, rates) VALUES(?, ?)",
                      (now, json.dumps(rates)))
            c.execute("DELETE FROM rate_snapshots WHERE ts < ?",
                      (now - KEEP_DAYS * 86400,))
        return True

    def snapshots(self, hours: float, limit: int = 400) -> list[tuple[float, dict]]:
        since = time.time() - hours * 3600
        with self._db() as c:
            rows = c.execute(
                "SELECT ts, rates FROM rate_snapshots WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        # Прореживаем равномерно: гнать на телефон тысячи точек незачем,
        # экран всё равно 414 пикселей шириной.
        if len(rows) > limit:
            step = len(rows) / limit
            rows = [rows[int(i * step)] for i in range(limit)]
        return [(ts, json.loads(r)) for ts, r in rows]
