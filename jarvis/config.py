"""Настройки ядра. Всё читается из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _paths(raw: str) -> tuple[Path, ...]:
    """Список папок через «;» (в путях Windows есть «:», поэтому не он)."""
    return tuple(Path(x.strip()).expanduser().resolve() for x in raw.replace("\n", ";").split(";") if x.strip())


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
    # Пауза в разговоре, после которой агент начинает новую сессию (минуты; 0 — не начинать). Кэш запроса живёт
    # пять минут: после паузы вся накопленная переписка оплачивается заново, и чем длиннее сессия, тем дороже ход.
    session_idle: float = field(default_factory=lambda: float(os.getenv("JARVIS_SESSION_IDLE_MIN", "30")) * 60)
    # Клиенты
    api_token: str = field(default_factory=lambda: os.getenv("JARVIS_API_TOKEN", ""))
    # Токен «простого» профиля (гости, дети, планшет на стене): видит и переключает только устройства,
    # открытые ассистенту; чата, настроек и журнала у него нет. Пусто — профиля нет.
    simple_token: str = field(default_factory=lambda: os.getenv("JARVIS_SIMPLE_TOKEN", "").strip())
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_allowed: frozenset[int] = field(default_factory=lambda: _ids(os.getenv("TELEGRAM_ALLOWED_IDS", "")))
    # Интеграции
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
    # Умный дом: MCP-сервер Home Assistant; видит только сущности, открытые для Assist
    hass_url: str = field(default_factory=lambda: os.getenv("HASS_URL", "").rstrip("/"))
    hass_token: str = field(default_factory=lambda: os.getenv("HASS_TOKEN", ""))
    # /api/mcp/assist — только Assist (открытые сущности); /api/mcp — все API, включая вызов любых служб
    hass_mcp_path: str = field(default_factory=lambda: os.getenv("HASS_MCP_PATH", "/api/mcp/assist"))
    # Notion: токен внутренней интеграции и страница, под которой создавать новые
    notion_token: str = field(default_factory=lambda: os.getenv("NOTION_TOKEN", ""))
    notion_parent: str = field(default_factory=lambda: os.getenv("NOTION_PARENT_PAGE", ""))
    # Речь: свои серверы (если заданы URL) или Yandex SpeechKit — см. speech.make_speech
    stt_url: str = field(default_factory=lambda: os.getenv("JARVIS_STT_URL", ""))
    stt_model: str = field(default_factory=lambda: os.getenv("JARVIS_STT_MODEL", ""))
    tts_url: str = field(default_factory=lambda: os.getenv("JARVIS_TTS_URL", ""))
    tts_voice: str = field(default_factory=lambda: os.getenv("JARVIS_TTS_VOICE", ""))
    tts_model: str = field(default_factory=lambda: os.getenv("JARVIS_TTS_MODEL", ""))
    tts_rate: int = field(default_factory=lambda: int(os.getenv("JARVIS_TTS_RATE", "22050")))
    # Слова активации по распознанному тексту (через запятую); работают только со своим STT
    wake_words: str = field(default_factory=lambda: os.getenv("JARVIS_WAKE_WORDS", "джарвис"))

    @property
    def assistant_name(self) -> str:
        """Как ассистента зовут: первое слово активации."""
        first = self.wake_words.replace(";", ",").split(",")[0].strip()
        return first[:1].upper() + first[1:] if first else "Джарвис"
    speech_lang: str = field(default_factory=lambda: os.getenv(
        "JARVIS_SPEECH_LANG", os.getenv("YANDEX_SPEECHKIT_LANG", "ru-RU")))
    speechkit_key: str = field(default_factory=lambda: os.getenv("YANDEX_SPEECHKIT_API_KEY", ""))
    speechkit_voice: str = field(default_factory=lambda: os.getenv("YANDEX_SPEECHKIT_VOICE", "alena"))
    speechkit_tts_rate: int = field(default_factory=lambda: int(os.getenv("YANDEX_SPEECHKIT_TTS_RATE", "48000")))
    speechkit_folder: str = field(default_factory=lambda: os.getenv("YANDEX_FOLDER_ID", ""))
    # Push через ntfy (self-hosted или ntfy.sh); приложение ntfy работает на Android без GMS
    ntfy_url: str = field(default_factory=lambda: os.getenv("NTFY_URL", ""))
    ntfy_topic: str = field(default_factory=lambda: os.getenv("NTFY_TOPIC", ""))
    ntfy_token: str = field(default_factory=lambda: os.getenv("NTFY_TOKEN", ""))
    # Откуда браузерным клиентам можно ходить в API (Tauri и dev-сервер Vite — по умолчанию)
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(x.strip() for x in os.getenv(
        "JARVIS_CORS_ORIGINS",
        "http://localhost:5173,tauri://localhost,http://tauri.localhost,https://tauri.localhost",
    ).split(",") if x.strip()))
    # Голос: быстрый контур
    router_model: str = field(default_factory=lambda: os.getenv("JARVIS_ROUTER_MODEL", "claude-haiku-5-5"))
    router_effort: str = field(default_factory=lambda: os.getenv("JARVIS_ROUTER_EFFORT", "low"))
    voice_agent_timeout: float = field(default_factory=lambda: float(os.getenv("JARVIS_VOICE_AGENT_TIMEOUT", "25")))
    # Громкость и плееры на ПК, где запущено ядро (wpctl, MPRIS); по умолчанию выключено
    pc_control: bool = field(default_factory=lambda: os.getenv("JARVIS_PC_CONTROL", "").strip().lower()
                             in ("1", "true", "yes", "on"))
    # Снимок экрана для агента («что у меня на экране?»); по умолчанию выключено
    screen: bool = field(default_factory=lambda: os.getenv("JARVIS_SCREEN", "").strip().lower()
                         in ("1", "true", "yes", "on"))
    # Браузер для агента (Playwright MCP, ставится отдельно в browser_dir); по умолчанию выключено
    browser: bool = field(default_factory=lambda: os.getenv("JARVIS_BROWSER", "").strip().lower()
                          in ("1", "true", "yes", "on"))
    browser_dir: Path = field(default_factory=lambda: Path(
        os.getenv("JARVIS_BROWSER_DIR", "").strip() or "~/.local/share/jarvis/browser").expanduser())
    # Модель помощника, который ходит по сайтам; пусто — быстрая модель роутера
    browser_model: str = field(default_factory=lambda: os.getenv("JARVIS_BROWSER_MODEL", "").strip())
    browser_headless: bool = field(default_factory=lambda: os.getenv("JARVIS_BROWSER_HEADLESS", "").strip().lower()
                                   in ("1", "true", "yes", "on"))
    # Адрес поиска, к которому помощник дописывает запрос; пусто — Bing
    browser_search: str = field(default_factory=lambda: os.getenv("JARVIS_BROWSER_SEARCH", "").strip())
    browser_budget_usd: float = field(default_factory=lambda: float(os.getenv("JARVIS_BROWSER_BUDGET_USD", "0.3")))
    # Принтер для print_file; пусто — системный по умолчанию
    printer: str = field(default_factory=lambda: os.getenv("JARVIS_PRINTER", "").strip())
    # Музыка: на ПК ядра (нужен JARVIS_PC_CONTROL) — YouTube и, если задан токен, Яндекс Музыка;
    # на Яндекс Станции — через Home Assistant (плеер ищется сам, либо JARVIS_STATION_ENTITY)
    music_source: str = field(default_factory=lambda: os.getenv("JARVIS_MUSIC_SOURCE", "").strip().lower())
    yandex_music_token: str = field(default_factory=lambda: os.getenv("YANDEX_MUSIC_TOKEN", "").strip())
    station_entity: str = field(default_factory=lambda: os.getenv("JARVIS_STATION_ENTITY", "").strip())
    # Живые состояния на вкладке «Дом»: ядро держит подписку на изменения в Home Assistant
    home_live: bool = field(default_factory=lambda: os.getenv("JARVIS_HOME_LIVE", "1").strip().lower()
                            in ("1", "true", "yes", "on"))
    # Сторож: раз в столько минут опрашивать серверы речи и Home Assistant и сообщать о поломке (0 — не опрашивать)
    watch_min: float = field(default_factory=lambda: float(os.getenv("JARVIS_WATCH_MIN", "5") or 0))
    # Резервная копия vault и базы: папка (пусто — не делать), сколько архивов хранить, раз во сколько часов
    backup_dir: Path | None = field(default_factory=lambda: (
        Path(os.getenv("JARVIS_BACKUP_DIR", "").strip()).expanduser().resolve()
        if os.getenv("JARVIS_BACKUP_DIR", "").strip() else None))
    backup_keep: int = field(default_factory=lambda: int(os.getenv("JARVIS_BACKUP_KEEP", "14") or 14))
    backup_hours: float = field(default_factory=lambda: float(os.getenv("JARVIS_BACKUP_HOURS", "24") or 24))
    # Папки на ПК, где агенту разрешено искать и открывать файлы (пусто = выключено)
    open_roots: tuple[Path, ...] = field(default_factory=lambda: _paths(os.getenv("JARVIS_OPEN_ROOTS", "")))
