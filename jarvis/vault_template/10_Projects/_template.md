---
id: my-project
status: active            # active | paused | archived
stack: [vue3, vite, fastapi, postgres]
repo: git@github.com:me/my-project.git
local_path: D:/projects/my-project
default_branch: main
test_cmd: "pnpm test && pytest -q"
deploy:
  staging: "gh workflow run deploy.yml -f env=staging"
  prod: "gh workflow run deploy.yml -f env=prod"
deploy_policy: staging_auto_prod_confirm
sensitive: false          # true → реальные данные (БД, пациенты) в LLM не отправлять
last_touched: 2026-11-15
---
## Контекст

## Открытые задачи
- [ ] ...

## Журнал
