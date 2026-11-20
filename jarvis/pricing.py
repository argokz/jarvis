"""Цена запроса к модели по счётчикам токенов из ответа API.

Цены — доллары за миллион токенов (ввод, вывод), сверены 06.10.2026 с https://platform.claude.com/docs/en/pricing.
Чтение из кэша стоит долю цены ввода, запись в кэш на пять минут — в 1,25 раза дороже ввода.
Модели, которой нет в таблице, цену не выдумываем: вернётся None, а токены всё равно попадут в журнал.
"""
from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
}
CACHE_READ = {"claude-opus-5-5": 0.05}  # у остальных — 0.1 цены ввода
CACHE_WRITE = 1.25
FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def tokens(usage) -> dict[str, int]:
    """Счётчики из ответа API; у подменного клиента в тестах их может не быть вовсе."""
    return {name: int(getattr(usage, name, 0) or 0) for name in FIELDS}


def add(total: dict[str, int], usage) -> dict[str, int]:
    for name, value in tokens(usage).items():
        total[name] = total.get(name, 0) + value
    return total


def cost(model: str, used: dict[str, int]) -> float | None:
    if model not in PRICES or not any(used.values()):
        return None
    price_in, price_out = PRICES[model]
    spent = (used.get("input_tokens", 0) * price_in
             + used.get("cache_read_input_tokens", 0) * price_in * CACHE_READ.get(model, 0.1)
             + used.get("cache_creation_input_tokens", 0) * price_in * CACHE_WRITE
             + used.get("output_tokens", 0) * price_out)
    return round(spent / 1_000_000, 6)
