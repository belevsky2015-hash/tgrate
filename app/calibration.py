from __future__ import annotations

import math
import sqlite3
import time
from statistics import median

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    route_key TEXT NOT NULL,
    pair TEXT NOT NULL,
    amount_in REAL NOT NULL,
    predicted_out REAL NOT NULL,
    actual_out REAL NOT NULL,
    comment TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS deals_route ON deals(route_key);
CREATE INDEX IF NOT EXISTS deals_pair ON deals(pair);
"""

HALF_LIFE_DAYS = 21.0     # старые сделки теряют вес
MIN_DEALS = 3
MAX_CORRECTION = 0.08     # не даём поправке уехать больше чем на 8%


class Calibrator:
    """
    Модель комиссий всегда врёт: где-то мейкер поставил другой курс, где-то
    банк округлил, где-то стакан уехал за время сделки. Поэтому после каждой
    реальной операции сюда пишется, сколько предсказали и сколько получили.

    Дальше на маршрут и на пару считается взвешенный по свежести коэффициент
    actual/predicted, и следующий расчёт идёт уже с ним. predicted — прогноз
    МОДЕЛИ, без поправки: поправка применяется к модели, и мерить её надо от
    модели же. Иначе, как только поправка заработает, новые сделки тянут её
    обратно к 1 и она раскачивается. Через десяток сделок
    на направление ошибка садится в те самые несколько процентов.
    """

    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def add_deal(
        self,
        route_key: str,
        pair: str,
        amount_in: float,
        predicted_out: float,
        actual_out: float,
        comment: str = "",
    ) -> int:
        # Повторное нажатие «Сохранить» — та же сделка, а не вторая: дубль
        # засчитал бы одну сделку дважды и включил поправку раньше времени.
        same = self.db.execute(
            "SELECT id FROM deals WHERE route_key=? AND pair=? AND amount_in=? "
            "AND actual_out=? AND ts > ? ORDER BY ts DESC LIMIT 1",
            (route_key, pair, amount_in, actual_out, time.time() - 120),
        ).fetchone()
        if same:
            return same["id"]
        cur = self.db.execute(
            "INSERT INTO deals(ts, route_key, pair, amount_in, predicted_out,"
            " actual_out, comment) VALUES (?,?,?,?,?,?,?)",
            (time.time(), route_key, pair, amount_in, predicted_out,
             actual_out, comment),
        )
        self.db.commit()
        return cur.lastrowid

    def _weighted(self, rows) -> float | None:
        num = den = 0.0
        now = time.time()
        ratios = []
        for r in rows:
            if r["predicted_out"] <= 0:
                continue
            ratio = r["actual_out"] / r["predicted_out"]
            if not (0.5 < ratio < 2.0):     # явный выброс или опечатка
                continue
            age_days = (now - r["ts"]) / 86400.0
            w = 0.5 ** (age_days / HALF_LIFE_DAYS)
            num += ratio * w
            den += w
            ratios.append(ratio)
        if den == 0 or len(ratios) < MIN_DEALS:
            return None
        avg = num / den
        # робастность: не даём одному кривому вводу утащить коэффициент
        med = median(ratios)
        avg = 0.5 * avg + 0.5 * med
        return max(1 - MAX_CORRECTION, min(1 + MAX_CORRECTION, avg))

    def factor(self, route_key: str, pair: str) -> tuple[float, str, int]:
        """Возвращает (коэффициент, на чём основан, сколько сделок)."""
        rows = self.db.execute(
            "SELECT * FROM deals WHERE route_key=? ORDER BY ts DESC LIMIT 60",
            (route_key,),
        ).fetchall()
        f = self._weighted(rows)
        if f is not None:
            return f, "маршрут", len(rows)

        rows = self.db.execute(
            "SELECT * FROM deals WHERE pair=? ORDER BY ts DESC LIMIT 60", (pair,)
        ).fetchall()
        f = self._weighted(rows)
        if f is not None:
            return f, "направление", len(rows)

        return 1.0, "нет данных", 0

    def accuracy(self, pair: str | None = None) -> dict:
        """Насколько модель промахивалась в последних сделках."""
        q = "SELECT * FROM deals"
        args: tuple = ()
        if pair:
            q += " WHERE pair=?"
            args = (pair,)
        q += " ORDER BY ts DESC LIMIT 200"
        rows = self.db.execute(q, args).fetchall()

        errs = [
            abs(r["actual_out"] / r["predicted_out"] - 1) * 100
            for r in rows
            if r["predicted_out"] > 0
        ]
        if not errs:
            return {"deals": 0, "mae_pct": None, "p90_pct": None}
        errs_sorted = sorted(errs)
        p90 = errs_sorted[min(len(errs_sorted) - 1, math.floor(len(errs) * 0.9))]
        return {
            "deals": len(errs),
            "mae_pct": round(sum(errs) / len(errs), 2),
            "p90_pct": round(p90, 2),
        }

    def recent(self, limit: int = 30) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM deals ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
