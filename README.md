# Jarvis — ядро личного ассистента

Один «мозг» на Claude Agent SDK, память в Obsidian-vault, клиенты — Telegram, голос на ПК и приложение
(Tauri 2 + Vue 3: Windows, macOS, Android; то же приложение открывается в браузере).

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

### Речь без облака

Вместо SpeechKit можно использовать свои серверы — ядро выбирает их, если задан `JARVIS_STT_URL`
или `JARVIS_TTS_URL` (`jarvis/speech.py: make_speech`):

- STT — любой OpenAI-совместимый `/v1/audio/transcriptions`: faster-whisper-server, speaches.
  На CPU модель `Systran/faster-whisper-small` распознаёт фразу за ~2–3 с, medium — за ~7 с.
- TTS — HTTP-сервер Piper. Русские голоса: `ru_RU-irina-medium`, `-denis-`, `-dmitri-`, `-ruslan-`.

```bash
uv tool install --python 3.13 "piper-tts[http]"
python -m piper.download_voices --download-dir ~/piper-voices ru_RU-irina-medium
python -m piper.http_server --host 127.0.0.1 --port 5005 -m ru_RU-irina-medium --data-dir ~/piper-voices
```

```
JARVIS_STT_URL=http://127.0.0.1:8000/v1/audio/transcriptions
JARVIS_STT_MODEL=Systran/faster-whisper-small
JARVIS_TTS_URL=http://127.0.0.1:5005
JARVIS_TTS_RATE=22050          # должна совпадать с частотой голоса Piper
```

Голос включается, только когда заданы оба URL. `onnxruntime` (Piper, openWakeWord) пока
собран до Python 3.14 — на 3.15 ядро работает, а Piper и голосовой клиент ставьте в окружение 3.13.

Wake word пока английский — встроенная модель openWakeWord `hey_jarvis`. Своё слово
(например, «Джарвис») — это отдельно обученная .onnx-модель, её путь передаётся в `--wake-model`.

## Приложение (этап 3)

Один Vue-клиент в `app/` для всего: окно Tauri на Windows/macOS, APK для Android и обычная
веб-страница, которую ядро раздаёт по адресу `/app/`.

- **Чат** — общий разговор из всех каналов в реальном времени: написали в Telegram — сообщение
  сразу видно в приложении. Текст уходит агенту (Sonnet), кнопка 🎙 — голосом через быстрый
  контур (Haiku) с озвучкой. Удерживать — говорить; короткое касание — «нажал, сказал, нажал».
  Нажатие во время ответа перебивает его.
- **Задачи** — запросы подтверждения с кнопками и лента действий агента (инструменты, отказы, решения).
- **Проекты** — реестр из frontmatter, обновляется сам, когда агент правит заметку.
- **Настройки** — адрес ядра и токен, новый разговор.

### Подтверждения
Перезапись целиком существующей заметки в `10_Projects/`, `20_Areas/`, `30_Decisions/` агент
делает только после вашего «да». Запрос приходит сразу в приложение, в Telegram (с кнопками) и
push-напоминанием в ntfy; ответить можно где удобнее. Нет ответа 5 минут — значит «нет».
Это тот же механизм, через который на этапе 5 пойдут деплой в прод и merge.

### Запуск

```bash
cd app && npm install
npm run build              # веб-версия → app/dist, ядро раздаёт её на /app/
npm run tauri dev          # окно приложения с горячей перезагрузкой
npm run tauri build        # установщик: .msi/.exe на Windows, .dmg на macOS
```

Для `tauri build` нужен Rust (rustup), на Windows — ещё Microsoft C++ Build Tools и WebView2
(в Windows 10/11 уже есть).

### Android (в том числе Huawei без Google-сервисов)

1. Android Studio с SDK и NDK; переменные `JAVA_HOME`, `ANDROID_HOME`, `NDK_HOME`;
   `rustup target add aarch64-linux-android armv7-linux-androideabi i686-linux-android x86_64-linux-android`.
2. `npm run tauri android init` — создаст `app/src-tauri/gen/android`.
3. В `gen/android/app/src/main/AndroidManifest.xml` добавить
   `<uses-permission android:name="android.permission.RECORD_AUDIO"/>` и
   `<uses-permission android:name="android.permission.MODIFY_AUDIO_SETTINGS"/>`.
4. `npm run tauri android build -- --apk --debug` и поставить APK на телефон.

Микрофон на Android при первом нажатии 🎙 спросит разрешение. Распознавание всё равно идёт на
сервере (SpeechKit), поэтому отсутствие Google-сервисов на Huawei не мешает.

### Доступ с телефона: Tailscale

Ядро наружу не открываем. На ПК с ядром и на телефоне — Tailscale (на Huawei — APK с
tailscale.com или F-Droid). На ПК:

```bash
tailscale serve --bg 8000
```

Получится адрес `https://<имя-пк>.<tailnet>.ts.net` — его и вписываем в настройках приложения.
HTTPS обязателен: без него браузер не даст микрофон, а Android-сборка не пустит `ws://`.
Тот же адрес с `/app/` в браузере телефона — полноценная веб-версия (можно «добавить на главный экран»).

### Push: ntfy
Поставьте приложение ntfy (APK или F-Droid — работает без GMS), подпишитесь на тему из
`NTFY_TOPIC` и отключите для ntfy оптимизацию батареи. Push приходит, когда нужно
подтверждение и когда голосовая задача доделалась в фоне. В push нет ссылок и кодов, которыми
можно что-то подтвердить: тема на ntfy.sh публичная для всех, кто знает её имя.

## HTTP API

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/health` | без авторизации |
| POST | `/chat` `{"text": "...", "channel": "pc"}` | ход агента |
| POST | `/session/new` | новый разговор |
| GET | `/projects` | реестр проектов (JSON) |
| GET | `/messages?limit=100` | общий разговор из всех каналов |
| GET | `/journal?limit=50` | последние записи журнала |
| GET | `/confirmations` | ожидающие подтверждения |
| POST | `/confirmations/{id}` `{"approve": true}` | ответить на подтверждение |
| WS | `/events/ws` | живая лента: реплики, инструменты, подтверждения |
| WS | `/voice/ws` | голосовой протокол (описан в `jarvis/voice.py`) |

Все, кроме `/health`, требуют `Authorization: Bearer $JARVIS_API_TOKEN` (WebSocket — также `?token=`).

## Тесты

```bash
uv run --extra voice pytest -q
cd app && npm test
```
