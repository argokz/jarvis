"""Настройки ядра. Всё читается из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(";", ",").split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    vault: Path = field(default_factory=lambda: Path(os.getenv("JARVIS_VAULT", "./vault")).expanduser().resolve())
    db: Path = field(default_factory=lambda: Path(os.getenv("JARVIS_DB", "./data/jarvis.db")).expanduser().resolve())
    # Идентификаторы моделей — только в конфиге, не в коде.
    model: str = field(default_factory=lambda: os.getenv("JARVIS_MODEL", "claude-sonnet-5-5"))
    effort: str = field(default_factory=lambda: os.getenv("JARVIS_EFFORT", "medium"))
    max_turns: int = field(default_factory=lambda: int(os.getenv("JARVIS_MAX_TURNS", "30")))
    max_budget_usd: float = field(default_factory=lambda: float(os.getenv("JARVIS_MAX_BUDGET_USD", "1.0")))
    # Клиенты
    api_token: str = field(default_factory=lambda: os.getenv("JARVIS_API_TOKEN", ""))
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_allowed: frozenset[int] = field(default_factory=lambda: _ids(os.getenv("TELEGRAM_ALLOWED_IDS", "")))
    # Интеграции
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
    speechkit_key: str = field(default_factory=lambda: os.getenv("YANDEX_SPEECHKIT_API_KEY", ""))
    speechkit_lang: str = field(default_factory=lambda: os.getenv("YANDEX_SPEECHKIT_LANG", "ru-RU"))
