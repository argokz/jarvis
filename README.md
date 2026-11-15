# Jarvis — ядро личного ассистента

Один «мозг» на Claude Agent SDK, память в Obsidian-vault, клиенты — Telegram, HTTP API и голос на ПК.

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
- Файлы на ПК — только найти и открыть, и только в `JARVIS_OPEN_ROOTS`. Windows-MCP (PowerShell,
  управление окнами) намеренно не подключён: ему нужен поток подтверждений, это этап 5.
- PreToolUse-хук (`jarvis/agent.py: check_path`) отклоняет любой путь вне vault и запись в
  `90_Agent/policies.md`, `.obsidian/`, `.git/`.
- GitHub MCP подключается только при заданном `GITHUB_TOKEN`, в режиме read-only.
- HTTP API слушает localhost; наружу его не выставлять (дальше — Tailscale).

## Голос на ПК (этап 2)

```
микрофон → wake word «Hey Jarvis» + Silero VAD (на ПК, openWakeWord)
         → WebSocket /voice/ws → SpeechKit STT → Haiku 5.5 (быстрый ответ или delegate)
                                                    └→ агент на Sonnet 5.5 (vault, файлы на ПК)
         ← озвучка SpeechKit по фразам: первая звучит, пока генерируется вторая
```

Клиент тонкий: ключи Anthropic и SpeechKit есть только у ядра. История разговора общая
со всеми каналами — можно начать в Telegram и продолжить голосом.

Запуск клиента (ядро уже запущено, в `.env` тот же `JARVIS_API_TOKEN`):

```bash
uv sync --extra voice
uv run --extra voice python -m jarvis.voice_client
```

- «Hey Jarvis» или Enter — начать слушать; 5 секунд после ответа слушает продолжение без wake word.
- «Hey Jarvis» во время ответа — перебить. С гарнитурой можно `--barge-in vad`: перебивает любая
  речь (с колонками так нельзя — ассистент будет перебивать сам себя).
- Строка текста + Enter — вопрос текстом, ответ голосом.
- `--list-devices` и `--device N` — выбор микрофона; `--threshold` — чувствительность wake word.
- Долгие задачи (дольше `JARVIS_VOICE_AGENT_TIMEOUT`) голос не держат: «пришлю в Telegram».
- Файлы на ПК: задайте `JARVIS_OPEN_ROOTS`, и «открой таблицу бюджета» найдёт и откроет файл.
  Поиск и открытие — только внутри этих папок. Работает, если ядро запущено на том же ПК.

Wake word пока английский — встроенная модель openWakeWord `hey_jarvis`. Своё слово
(например, «Джарвис») — это отдельно обученная .onnx-модель, её путь передаётся в `--wake-model`.

## HTTP API

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/health` | без авторизации |
| POST | `/chat` `{"text": "...", "channel": "pc"}` | ход агента |
| POST | `/session/new` | новый разговор |
| GET | `/projects` | реестр проектов (JSON) |
| GET | `/journal?limit=50` | последние записи журнала |
| WS | `/voice/ws` | голосовой протокол (описан в `jarvis/voice.py`) |

Все, кроме `/health`, требуют `Authorization: Bearer $JARVIS_API_TOKEN` (WebSocket — также `?token=`).

## Тесты

```bash
uv run pytest -q
```
