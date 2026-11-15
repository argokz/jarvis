# Реестр интеграций

| Интеграция | Статус | Комментарий |
|---|---|---|
| Vault (файлы: Read/Glob/Grep/Write/Edit) | включено | только внутри vault |
| Реестр проектов (`project_registry`) | включено | frontmatter из `10_Projects/` |
| Быстрые заметки (`capture_note`) | включено | пишет в `00_Inbox/` |
| GitHub MCP | если задан GITHUB_TOKEN | только чтение (этап 1) |
| Telegram | если задан TELEGRAM_BOT_TOKEN | текст + голосовые |
