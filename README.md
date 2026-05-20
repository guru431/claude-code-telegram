# Claude Code Telegram

Telegram-бот для удалённого доступа к [Claude Code](https://claude.ai/code). Позволяет общаться с Claude о проектах из любого устройства через Telegram -- без терминала.

## Стек

- **Python 3.11+**, Poetry
- **python-telegram-bot** v22 -- Telegram API
- **claude-agent-sdk** + **anthropic** -- интеграция с Claude Code (SDK-режим, стриминг)
- **FastAPI / Uvicorn** -- webhook-сервер для внешних событий
- **APScheduler** -- планировщик cron-задач
- **Pydantic Settings v2** -- конфигурация через .env
- **aiosqlite** -- SQLite-хранилище (сессии, аудит, стоимость)
- **structlog** -- логирование (JSON в проде, консоль в деве)
- Опционально: **mistralai** / **openai** -- транскрипция голосовых сообщений

## Структура проекта

```
src/
  main.py                  -- точка входа
  bot/
    core.py                -- инициализация бота
    orchestrator.py        -- маршрутизация сообщений (agentic / classic)
    handlers/              -- command, message, callback хендлеры
    middleware/             -- auth, rate_limit, security
    features/              -- git, file upload, quick actions, voice, session export
    utils/                 -- DraftStreamer (стриминг), HTML-форматирование
  claude/
    facade.py              -- ClaudeIntegration (фасад)
    sdk_integration.py     -- ClaudeSDKManager (async streaming)
    session.py             -- управление сессиями
    local_sessions.py      -- обнаружение сессий из VS Code / CLI
    monitor.py             -- валидация tool calls
  config/                  -- Pydantic Settings, feature flags, YAML-загрузчик
  storage/                 -- SQLite: repository pattern, миграции
  security/                -- auth (whitelist + token), валидация, rate limiter, аудит
  api/                     -- FastAPI webhook-сервер (GitHub HMAC-SHA256, Bearer)
  events/                  -- EventBus (async pub/sub)
  scheduler/               -- APScheduler cron-задачи
  notifications/           -- rate-limited доставка в Telegram
  mcp/                     -- FastMCP stdio-сервер (send_image_to_user)
  projects/                -- мульти-проектный режим: registry, thread_manager, discovery
tests/                     -- pytest + pytest-asyncio
config/                    -- projects.example.yaml
docs/                      -- документация (setup, configuration, tools)
```

## Ключевые возможности

- **Два режима работы**: agentic (по умолчанию, естественный язык) и classic (13 команд, inline-клавиатуры)
- **Стриминг ответов** через DraftStreamer с индикатором набора
- **Автоматическое сохранение сессий** per user+directory в SQLite
- **Загрузка файлов и изображений** с анализом, распаковка архивов
- **Голосовые сообщения** -- транскрипция через Mistral Voxtral / OpenAI Whisper
- **Git-интеграция** -- безопасные операции с репозиториями
- **Webhook API** -- приём событий GitHub (push, PR, issues) с HMAC-SHA256 верификацией
- **Планировщик** -- cron-задачи с персистентным хранилищем
- **Уведомления** -- проактивная доставка в Telegram с rate limiting
- **Мульти-проектные топики** -- маршрутизация по проектам через Telegram topics
- **MCP-сервер** -- отправка изображений пользователю из Claude
- **5-уровневая безопасность**: auth -> directory isolation -> input validation -> rate limiting -> audit

## Конфигурация

Минимум в `.env`:

```bash
TELEGRAM_BOT_TOKEN=...        # от @BotFather
TELEGRAM_BOT_USERNAME=...     # имя бота
APPROVED_DIRECTORY=...        # базовая директория проектов
ALLOWED_USERS=123456789       # Telegram user ID (через запятую)
```

Основные опции: `ANTHROPIC_API_KEY`, `AGENTIC_MODE` (default true), `VERBOSE_LEVEL` (0-2), `ENABLE_API_SERVER`, `ENABLE_SCHEDULER`, `ENABLE_PROJECT_THREADS`, `ENABLE_VOICE_MESSAGES`.

Полный список -- см. `.env.example` и `docs/configuration.md`.

## Запуск

```bash
make dev           # установить зависимости (включая dev)
make run           # запуск бота
make run-debug     # запуск с debug-логированием
make test          # тесты с coverage
make lint          # black + isort + flake8 + mypy
make format        # автоформатирование
```

Версия: **1.5.0** | Лицензия: MIT

## Credits

Изначально форк [RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram). С момента форка значительно переработано: agentic-режим как default, webhook-сервер (FastAPI) для внешних триггеров, multi-project topics в Telegram, транскрипция голосовых сообщений (Mistral/OpenAI), local session discovery (`~/.claude/projects/`), MCP-сервер для отправки изображений, расширенный набор middleware (security/auth/rate-limit) и итеративные code reviews.
