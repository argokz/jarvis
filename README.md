# Jarvis — ядро личного ассистента (этап 1)

Один «мозг» на Claude Agent SDK, память в Obsidian-vault, клиенты — Telegram и HTTP API.

## Запуск

```bash
uv sync
cp .env.example .env    # заполнить ключи
uv run uvicorn jarvis.api:app --host 127.0.0.1 --port 8000
```

Минимум в `.env`: `ANTHROPIC_API_KEY`, `JARVIS_VAULT`, и либо `TELEGRAM_BOT_TOKEN` +
`TELEGRAM_ALLOWED_IDS`, либо `JARVIS_API_TOKEN`. Свой Telegram ID бот подскажет сам в ответ на
первое сообщение.

Vault создаётся при первом запуске. Чтобы добавить проект, скопируйте
`10_Projects/_template.md` в `10_Projects/<id>.md` и заполните frontmatter.

## Что умеет

- Отвечает по реестру проектов (сводка — в системном промпте, подробности — `project_registry`).
- Читает, ищет и правит заметки vault; быстрые заметки — в `00_Inbox/`.
- Память между разговорами — `90_Agent/memories/`.
- Одна сессия на пользователя, общая для всех клиентов; `/new` начинает заново.
- Telegram: текст и голосовые до 30 с (Yandex SpeechKit).
- Журнал: SQLite (`/journal`) + дневная заметка `40_Journal/ГГГГ-ММ-ДД.md`.

## Границы безопасности

- Из встроенных инструментов агенту доступны только Read/Glob/Grep/Write/Edit. Shell и веба нет.
- PreToolUse-хук (`jarvis/agent.py: check_path`) отклоняет любой путь вне vault и запись в
  `90_Agent/policies.md`, `.obsidian/`, `.git/`.
- GitHub MCP подключается только при заданном `GITHUB_TOKEN`, в режиме read-only.
- HTTP API слушает localhost; наружу его не выставлять (дальше — Tailscale).

## HTTP API

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/health` | без авторизации |
| POST | `/chat` `{"text": "...", "channel": "pc"}` | ход агента |
| POST | `/session/new` | новый разговор |
| GET | `/projects` | реестр проектов (JSON) |
| GET | `/journal?limit=50` | последние записи журнала |

Все, кроме `/health`, требуют `Authorization: Bearer $JARVIS_API_TOKEN`.

## Тесты

```bash
uv run pytest -q
```
