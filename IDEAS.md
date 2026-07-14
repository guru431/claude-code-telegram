# Ideas — claude-code-telegram

Предложенные фичи. Анти-шум: только с конкретной болью, максимум ценности. Статус: `proposed` → `accepted` → `done` / `wontfix`.

## I6 · Capability profiles с проверяемой политикой [proposed]

**Боль:** Сейчас `allowed_tools` выглядит как allowlist, но означает auto-approval; interactive, webhook и scheduler используют несовместимые ожидания безопасности.

**Идея:** Завести именованные профили `interactive`, `webhook-readonly`, `scheduled`, `project-readonly`: каждый задаёт реальный tool surface, permission mode, sandbox/network/project-settings policy и cost limits. Добавить admin-команду `/policy [profile]`, показывающую эффективные разрешения и объясняющую, почему конкретный tool call будет разрешён/запрещён.

**Ценность:** Один аудируемый security contract вместо рассыпанных флагов; безопасные внешние автоматизации становятся реально пригодны.

**Критерий принятия:** E2E suite на установленном SDK/CLI доказывает deny опасных tools для каждого профиля, а `/policy` совпадает с фактическим результатом исполнения.

## I7 · Durable Event Inbox и ручной replay [proposed]

**Боль:** Webhook/scheduler результат может потеряться между agent run и Telegram, а `/events` показывает лишь часть состояния без безопасного управления.

**Идея:** Сделать inbox/outbox со стадиями `received`, `running`, `agent_done`, `delivered`, `retrying`, `dead`; в Telegram дать фильтры, redacted error, retry/cancel и просмотр delivery history. Replay должен сохранять idempotency и исходный project/topic context.

**Ценность:** Внешние события становятся наблюдаемым workflow, а не best-effort фоновой задачей; инциденты можно восстановить без SQL/рестарта.

**Критерий принятия:** Crash на каждой границе восстанавливается без потери/двойной мутации, dead letter можно повторить из Telegram, delivery подтверждается отдельно от agent completion.

## I8 · Project-aware Scheduler 2.0 [proposed]

**Боль:** Задача теряет автора, текущий проект и forum topic; отсутствуют удобные edit/pause/run-now/history.

**Идея:** При создании сохранять creator, project slug/cwd, chat/thread, timezone и policy profile; добавить `/schedule edit|pause|resume|run|history` и inline UI с next-run preview. Для risky profiles — опциональное подтверждение первого запуска.

**Ценность:** Расписания становятся предсказуемой автоматизацией для нескольких проектов/пользователей, а результаты возвращаются в правильный контекст.

**Критерий принятия:** После restart задача сохраняет identity/context, выполняется ровно в заданной timezone и доставляет результат/ошибку в исходный topic с полной history.

## I9 · Admin `/doctor` и operational dashboard [proposed]

**Боль:** Большая часть текущих отказов видна только по логам: no-op env, неактивный hook, очередь событий, stuck tasks, migration/version mismatch, exhausted budget.

**Идея:** Добавить redacted `/doctor` и расширенный `/health`: effective config source, unknown/deprecated variables, SDK/CLI versions, DB schema/pool, queue depth, active runs, dead letters, last delivery, topic sync, budgets и hook status. Для каждой проблемы — безопасная remediation подсказка, без вывода значений секретов.

**Ценность:** Сокращает диагностику установки и эксплуатации с ручного чтения кода/логов до одной команды.

**Критерий принятия:** Набор fault-injection tests даёт ожидаемый health status; output не содержит secret values и различает warning/degraded/unhealthy.

## I10 · User-owned Session Library [proposed]

**Боль:** Глобальный local-session список смешивает пользователей и проекты; полезных операций поиска, названия, pin/fork и безопасной передачи контекста нет.

**Идея:** Хранить owner/project lineage и дать `/sessions` search/filter/rename/pin/fork/archive с preview стоимости и context age. Импорт CLI/VS Code sessions сделать явным admin-approved действием, создающим новую owned fork вместо прямого resume чужого ID.

**Ценность:** Безопасная и понятная долговременная работа с десятками сессий без утечки preview/контекста между пользователями.

**Критерий принятия:** Ни list, ни callback resume не пересекают owner/project boundary; импорт и fork оставляют audit lineage и работают после restart.

## I11 · One-time pairing вместо номинального token auth [proposed]

**Боль:** Статический whitelist неудобен для подключения нового пользователя, а существующий TokenAuthProvider не имеет login flow и теряет state при restart.

**Идея:** Admin создаёт одноразовый короткоживущий pairing code/deep link; пользователь подтверждает Telegram account, после чего в SQLite хранится только hash/revocable grant с ролью и project scopes. Добавить list/revoke/expiry и уведомление admin о новом pairing.

**Ценность:** Управляемый multi-user onboarding без пересылки постоянных секретов и ручного редактирования `.env`.

**Критерий принятия:** Code одноразовый, истекает, устойчив к restart/replay, grant можно отозвать немедленно; audit не хранит исходный credential.

## I12 · Per-project policy, budget и notification routes [proposed]

**Боль:** Один глобальный tool/cost/notification профиль слишком широк для смеси production, docs и экспериментальных repositories.

**Идея:** Расширить project registry полями policy profile, per-run/daily budget, allowed event types, default model, notification chats/topics и read-only windows. Telegram UI должен показывать effective policy и разрешать admin override с expiry.

**Ценность:** Least privilege и контролируемая стоимость на уровне проекта; scheduler/webhooks можно включать там, где они безопасны, не ослабляя весь bot.

**Критерий принятия:** Все entrypoints получают policy только из resolved project context; override истекает автоматически и отражается в audit/dashboard.

## I13 · Safe ingestion workspace с preview [proposed]

**Боль:** File/archive/media обработка дублируется, доверяет metadata и либо блокирует поддерживаемые форматы, либо способна построить чрезмерный prompt.

**Идея:** Потоково принимать файл в quarantine workspace с hard byte/compression/file-count limits, MIME sniffing и bounded tree preview; перед отправкой Claude показывать пользователю manifest и выбор файлов/действия. Один ingestion API обслуживает agentic/classic/document/archive/MCP.

**Ценность:** Безопаснее большие code bundles и заметно меньше лишних tokens; пользователь понимает, что именно увидит Claude.

**Критерий принятия:** Фактические bytes никогда не превышают policy, archive bomb останавливается до extraction/prompt, manifest воспроизводим, temp workspace гарантированно очищается.

## I14 · Единый capability/config registry [proposed]

**Боль:** Settings, `.env.example`, docs, command menus, file extensions, feature registry и quick actions расходятся вручную.

**Идея:** Описывать capability один раз типизированной schema и из неё генерировать env reference, docs tables, Telegram menu и callback registry; CI проверяет unknown/deprecated vars и каждый публичный callback/command. Deprecated keys получают явное предупреждение и срок удаления.

**Ценность:** Устраняет no-op настройки и устаревшие обещания, делает добавление функции дешевле и безопаснее.

**Критерий принятия:** Изменение schema автоматически обновляет артефакты; committed docs/config проходят drift test, неизвестная переменная не игнорируется молча.

## История

_Батч 2026-07-09: I1/I3/I4/I5 реализованы, I2 (разбиение orchestrator на модули) отклонён как слишком инвазивный — закрыто 2026-07-10._
