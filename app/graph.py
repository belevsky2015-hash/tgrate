from __future__ import annotations

import heapq
import itertools
from collections import defaultdict
from typing import Iterable

import yaml

from .models import Edge, Route, StepResult, Unreachable

MAX_HOPS = 6


def _num(value, variables: dict[str, float], default: float = 0.0) -> float:
    """Разрешает '${p2p_fee}' в число, обычные значения пропускает."""
    if value is None:
        return default
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return float(variables.get(value[2:-1], default))
    return float(value)


def load_graph(path: str, variables: dict[str, float] | None = None) -> tuple[dict, list[Edge]]:
    variables = variables or {}
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    nodes = raw.get("nodes", {})
    edges: list[Edge] = []
    for e in raw.get("edges", []):
        rate = e.get("rate", 1.0)
        edges.append(
            Edge(
                id=e["id"],
                src=e["src"],
                dst=e["dst"],
                kind=e.get("kind", "transfer"),
                rate_ref=rate if isinstance(rate, str) else None,
                rate_const=float(rate) if not isinstance(rate, str) else None,
                fee_pct=_num(e.get("fee_pct"), variables),
                fee_src=_num(e.get("fee_src"), variables),
                fee_dst=_num(e.get("fee_dst"), variables),
                spread_pct=_num(e.get("spread_pct"), variables),
                min_amount=float(e.get("min", 0.0)),
                max_amount=float(e.get("max", float("inf"))),
                eta_min=int(e.get("eta_min", 0)),
                note=e.get("note", ""),
            )
        )
    return nodes, edges


def bind_rates(edges: Iterable[Edge], quotes: dict[str, float]) -> list[str]:
    """Подставляет живые котировки. Возвращает список неразрешённых ссылок."""
    missing = []
    for e in edges:
        if e.rate_ref is None:
            e.rate = e.rate_const or 1.0
            e.rate_origin = "const"
            continue
        val = quotes.get(e.rate_ref)
        if val is None:
            missing.append(e.rate_ref)
            e.rate = 0.0
            e.rate_origin = "missing"
        else:
            e.rate = val
            e.rate_origin = e.rate_ref
    return missing


def best_route(edges: list[Edge], src: str, dst: str, amount: float) -> Route:
    """
    Дейкстра на максимум выхода. Корректна, потому что каждая функция ребра
    монотонно не убывает по входной сумме: больше зашло — не меньше вышло.
    """
    routes = k_best_routes(edges, src, dst, amount, k=1)
    if not routes:
        raise Unreachable(f"Нет маршрута {src} -> {dst} на сумму {amount}")
    return routes[0]


def k_best_routes(
    edges: list[Edge], src: str, dst: str, amount: float, k: int = 5
) -> list[Route]:
    """K лучших маршрутов. Поиск в ширину с отсечением по лучшему результату."""
    out_edges: dict[str, list[Edge]] = defaultdict(list)
    for e in edges:
        if e.rate > 0:
            out_edges[e.src].append(e)

    counter = itertools.count()
    # (-сумма, tie, node, steps, visited)
    heap = [(-amount, next(counter), src, [], frozenset([src]))]
    found: list[Route] = []
    best_at: dict[str, float] = {}

    while heap and len(found) < k * 4:
        neg, _, node, steps, visited = heapq.heappop(heap)
        cur = -neg

        if node == dst and steps:
            found.append(Route(steps=list(steps)))
            continue
        if len(steps) >= MAX_HOPS:
            continue

        for e in out_edges.get(node, []):
            if e.dst in visited:
                continue
            if not e.usable(cur):
                continue
            try:
                step = e.apply(cur)
            except Unreachable:
                continue

            seen = best_at.get(e.dst)
            # пускаем чуть худшие ветки, иначе не наберём альтернатив
            if seen is not None and step.amount_out < seen * 0.90:
                continue
            if seen is None or step.amount_out > seen:
                best_at[e.dst] = step.amount_out

            heapq.heappush(
                heap,
                (
                    -step.amount_out,
                    next(counter),
                    e.dst,
                    steps + [step],
                    visited | {e.dst},
                ),
            )

    found.sort(key=lambda r: -r.amount_out)
    unique, seen_keys = [], set()
    for r in found:
        if r.key in seen_keys:
            continue
        seen_keys.add(r.key)
        unique.append(r)
    return unique[:k]


def reachable_nodes(edges: list[Edge]) -> set[str]:
    s = set()
    for e in edges:
        s.add(e.src)
        s.add(e.dst)
    return s
