# Ideas — claude-code-telegram

Предложенные фичи. Анти-шум: только с конкретной болью, максимум ценности. Статус: `proposed` → `accepted` → `done` / `wontfix`.

> Батч 2026-07-09: адверсариальная оркестровка (9 критиков, двойная refute-by-default верификация). Отметка `[split 1C/1R]` = один верификатор подтвердил, один отклонил.

## 2026-07-09 · Дедуплицировать run+deliver пайплайн agentic_text и _handle_agentic_media_message [P2]
**Боль:** Текстовый (`agentic_text`) и медиа-путь (`_handle_agentic_media_message`) вручную дублируют один и тот же ~75-строчный блок доставки ответа (combine text+images в caption, цикл formatted_messages с HTML→plain→error fallback, отдельная отправка картинок) + setup жизненного цикла Claude-run'а (interrupt_event, Stop-keyboard, per-user lock, ActiveRequest, typing-heartbeat). Копии уже разошлись (is_error, redact_secrets ретрофитились в оба, удаление progress_msg отличается) — security-релевантно: правка редакции секретов в одном пути может оставить утечку в другом. Главный драйвер 2286-строчного orchestrator.
**Что/предложение:** Вынести двух коллабораторов: (1) `ClaudeRunSession` (context manager: interrupt_event/stop_kb/request_lock/ActiveRequest/heartbeat setup+teardown); (2) `ResponseDelivery.send(update, formatted_messages, images)` (combine caption + send-with-fallback + image-логика). Оба хендлера зовут их → одна копия каждого.
**Статус:** proposed

## 2026-07-09 · Разбить orchestrator (2286 строк) по швам ответственности [P3]
**Боль:** `MessageOrchestrator` совмещает 9 несвязанных ролей: регистрация хендлеров обоих режимов, strict project-thread routing + persistence, verbose-progress форматирование, tool-input summarization, доставка album/document/animation (`_send_images` ~140 строк), полные text/document/photo/voice-пайплайны, admin `/schedule` и `/events` с собственным SQL, `/sessions`-листинг, `/repo`+cd-навигация. Каждое изменение фичи трогает этот файл (макс merge-conflict surface), unit-тесты требуют тяжёлого мокинга Update/context, а inline `/events`-SQL (:2034) минует repository-слой — смена схемы webhook_events молча ломает `/events`.
**Что/предложение:** Разделить: `ThreadRoutingMiddleware` (load/persist thread-context), `MediaDelivery` (`_send_images` + formatted send), `AdminCommands` (schedule/events, запрос webhook_events → в `WebhookEventRepository`), `SessionNavigation` (/repo, /sessions, cd/resume). Orchestrator оставляет только регистрацию + роутинг. (Синергия с идеей дедупликации run+deliver выше.)
**Статус:** proposed

## 2026-07-09 · Correlation-id, связывающий Telegram-сообщение через Claude-run, storage-запись и ответ [P3]
**Боль:** Agentic-путь логирует разрозненные события (`Agentic text message`, `Claude command completed`, `Failed to log interaction`) с ключом только по user_id, без общего request/trace-id от входящего сообщения через `run_command`/`save_claude_interaction` до исходящей отправки. Когда пользователь говорит «упало в 14:32», оператор не может сшить message → claude run → cost charge → delivery из логов; двух конкурентных пользователей в один момент не различить (только по user_id), диагностика одной неудачной интеракции — угадывание.
**Что/предложение:** Генерировать `request_id` (uuid4) на входе хендлера, биндить через `structlog.contextvars.bind_contextvars` (все downstream-логи несут его), прокидывать в `run_command`/`save_claude_interaction`, включать в хвост user-facing ошибки для grep'а end-to-end. Опционально — counter исходов run'а (success/error/interrupted).
**Статус:** proposed

## 2026-07-09 · Ограничить рост MessageOrchestrator._request_locks (нет eviction) [P3]
**Боль:** `_get_request_lock()` лениво создаёт `asyncio.Lock` на user_id и никогда не удаляет записи. `StopAwareUpdateProcessor` намеренно бьёт свой аналогичный `_user_locks` по `_MAX_USER_LOCKS=10_000` через `_evict_idle_locks()`, а карта orchestrator'а — без cap/eviction. Сейчас практически ограничена числом allowed-пользователей, но при `ALLOW_ALL_USERS=true` (разрешено security-моделью) карта растёт неограниченно — латентный footgun, и две карты локов управляются несогласованно.
**Что/предложение:** Переиспользовать подход update-processor'а: evict idle (`not locked()`) при превышении cap; проще — после `request_lock.release()` в finally: `if not lock.locked(): self._request_locks.pop(user_id, None)`.
**Статус:** proposed

## 2026-07-09 · Убрать/починить мёртвый admin_required/require_auth middleware [P3] [split 1C/1R]
**Боль:** `admin_required` гейтит на `"admin" not in permissions`, но ни один провайдер не выдаёт `admin` (Whitelist → `["basic"]`, Token → `["basic","advanced"]`), т.е. при использовании отклонял бы 100% пользователей. Оба (`admin_required`, `require_auth`) — незарегистрированный мёртвый код (реальный admin-гейтинг идёт через `settings.is_admin`). Footgun: будущий разработчик, защитивший привилегированную команду `admin_required` по докстрингу, получит self-DoS (отказ всем) либо «починит» ослаблением проверки и откроет дыру; дублирует/конкурирует с корректным `Settings.is_admin`, размывая где живёт авторизация. Split-верификация: один рецензент счёл это intentional placeholder (докстринг явно помечает «placeholder»).
**Что/предложение:** Удалить `admin_required`+`require_auth`, либо переписать `admin_required` как делегат к `settings.is_admin(user_id)` (единый источник истины, уже используемый /restart, /schedule, orchestrator:2011) — тогда любое будущее применение корректно by construction.
**Статус:** proposed
