# Ideas — claude-code-telegram

Предложенные фичи. Анти-шум: только с конкретной болью, максимум ценности. Статус: `proposed` → `accepted` → `done` / `wontfix`.

Источник: глубокий мультиагентный анализ 2026-06-16 (24 аналитика → триаж → 2-линзовая верификация → синтез). Логика/оптимизации/архитектура — в [FINDINGS.md](FINDINGS.md). Каждая фича имеет «брата»-находку в FINDINGS (та же корневая проблема как баг/долг): F1↔A1, F2↔A3, F3↔O3.

---

## 2026-06-16 · Durable SQLite-аудит безопасности + durable token storage [feature]
**Боль:** форензик-след безопасности (попытки авторизации, security-violations, `/restart`, доступ к файлам, превышения rate-limit) стирается при каждом рестарте — `src/main.py:185` использует `InMemoryAuditStorage()` (список на 10000, помечен `# TODO: Use database storage in production`). После любого перезапуска нельзя ответить «кто ломился / кто запускал `/restart`». То же блокирует продакшн-токены: `src/main.py:130` `InMemoryTokenStorage()`, таблица `user_tokens` не используется.
**Что:** SQLite-backed `AuditStorage`-адаптер (`store_event`/`get_events`/`get_security_violations`), делегирующий в уже существующий `AuditLogRepository.log_event`; опционально — `TokenStorage` поверх `user_tokens`.
**Почему ценно:** таблица `audit_log` уже создана и проиндексирована, а read-API (`get_user_audit_log`/`get_recent_audit_log` в `src/storage/facade.py`) уже читает её — половина фичи построена, но писатель (security-events) и читатель не соединены (в проде в таблицу пишутся только `claude_interaction`). Малой правкой в `main.py` получаем работающий запросопригодный аудит, переживающий рестарт. (Token-половина уже fail-closed: `main.py` отказывается включать token-auth в проде без `DEVELOPMENT_MODE`.)
**Эскиз решения:** новый класс в `src/security/audit.py` (реализует протокол `AuditStorage`), делегирует в `AuditLogRepository`; в `src/main.py` заменить `InMemoryAuditStorage()` на него (storage уже инициализирован выше). Связано с FINDINGS `[A1]`.
**Статус:** proposed

## 2026-06-16 · /schedule — команда управления планировщиком [feature]
**Боль:** включение `ENABLE_SCHEDULER` даёт планировщик, который умеет исполнять задачи, но которому невозможно их задать — нет ни команды, ни API, ни callback'а, вызывающего `JobScheduler.add_job/list_jobs/remove_job` (`src/scheduler/scheduler.py:55,115,126`; вызовов в `src/` нет — единственный `add_job` в `main.py:460` это несвязанный `discovery_scheduler`). Единственный способ — руками вставлять строки в таблицу `scheduled_jobs`. Фича заявлена (CLAUDE.md, settings, docstrings, CHANGELOG «Add, remove, and list jobs programmatically»), но для пользователя недостижима.
**Что:** admin-gated команда `/schedule` (подкоманды `add` / `list` / `remove`) в оркестраторе, вызывающая методы `JobScheduler`, с audit-логированием.
**Почему ценно:** разблокирует уже написанную персистентность + APScheduler-обвязку без новой инфраструктуры — чистый «последний провод».
**Эскиз решения:** добавить handler в `src/bot/orchestrator.py` (по инструкции CLAUDE.md «Adding a New Bot Command»: регистрация в `_register_agentic_handlers`, добавление в `get_bot_commands`, audit), пробросить ссылку на `JobScheduler` через `bot_data` (сейчас инстанс — локальная переменная в `main.py:387`, не кладётся в bot_data). Гейтить через `ADMIN_USERS`. Связано с FINDINGS `[A3]`, `[L27]`, `[L42]`.
**Статус:** proposed

## 2026-06-16 · Allowlist типов GitHub webhook-событий [feature]
**Боль:** при настроенном GitHub-webhook каждая доставка — включая `ping` при создании и высокочастотные `push`/`status`/`check_run`/`workflow_job` — тратит полный agent-вызов Claude и рассылает уведомление во все чаты (`chat_id=0` → все `notification_chat_ids`). `receive_webhook` (`src/api/server.py:68`) принимает любой `event_type` и публикует без фильтра; `handle_webhook` (`src/events/handlers.py:59-92`) безусловно запускает Claude. Дедуп ловит только повтор того же `delivery_id`, не разные «шумные» события — неограниченный усилитель стоимости/шума, управляемый внешним трафиком.
**Что:** `settings.github_webhook_events` (дефолт — небольшой набор, напр. `['issues','pull_request','release']`); в `receive_webhook` возвращать `{status: 'ignored'}` для типов вне списка и короткозамыкать `ping` до публикации.
**Почему ценно:** один параметр, ограничивающий стоимость агента событиями, которые реально важны оператору (дополняет фильтр на стороне GitHub UI).
**Эскиз решения:** добавить поле в `src/config/settings.py`, проверку по `x-github-event` в `src/api/server.py` перед `EventBus.publish`. Связано с FINDINGS `[O3]`.
**Статус:** proposed
