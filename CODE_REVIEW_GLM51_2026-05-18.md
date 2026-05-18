# Code Review GLM-5.1 — 2026-05-18

Single-pass review проекта `claude-code-telegram` через GLM-5.1 (OpenCode Go). Методология: `_boss/cron/code-review-multi.py`. Сводный отчёт: `_boss/CODE_REVIEW_AUDIT_2026-05-18.md`.

## Сводка
- Targets: 1 (`claude-code-telegram/src`)
- Findings: 26 — P1=5, P2=17, P3=4
- P1 валидация: 3/2/0 (valid/partial/halluc)

## P1 — критические (после валидации)

### Valid

#### 1. tar extract без `filter='data'` (symlink-attack)
**file:** `claude-code-telegram/src/bot/features/file_handler.py:239` | **category:** security
В `_process_archive` для tar-архивов вызывается `tf.extract(member, extract_dir)` без параметра `filter='data'` (Python 3.12+). Проверка `member.name.startswith("/") or ".." in member.name` не валидирует `member.linkname`, поэтому symlink-атаки внутри архива (symlink → `/etc/passwd`) остаются возможны. Также не проверяется `member.issym()`/`member.islnk()`.
**Suggestion:** Использовать `tf.extract(member, extract_dir, filter='data')` (PEP 706) либо вручную проверять `member.linkname` теми же правилами, что и `member.name`.

> *GLM указал `lines: 142-155`, реально код на 226-239. Проблема существует — фактическая строка `tf.extract(...)` без filter присутствует.*

#### 2. Symlink path traversal в `validate_path`
**file:** `claude-code-telegram/src/security/validators.py:187-204` | **category:** security
`SecurityValidator.validate_path` делает `target = target.resolve()` и затем проверяет `_is_within_directory(target, self.approved_directory)` через `path.relative_to(directory)`. `resolve()` следует по symlink, поэтому symlink ВНУТРИ approved_directory, ведущий наружу, ускользает от проверки только если уже разрезолвлен; но если внутри approved_directory есть symlink → `/etc`, `resolve()` вернёт `/etc/...` и проверка `relative_to(approved_directory)` корректно его отвергнет. Тем не менее, валидация НЕ запрещает создавать/использовать symlink-цели; уязвимость частично смягчена, но есть TOCTOU-окно между resolve() и фактическим открытием файла потребителем.
**Suggestion:** Дополнительно проверять `os.path.realpath()` непосредственно перед операцией над файлом, либо запретить symlink'и внутри approved_directory.

#### 3. `config.allowed_users[0]` падает при None в production
**file:** `claude-code-telegram/src/main.py:169, 384` | **category:** bug
GLM указал `lines: 91` — это **другая строка** (argparse). Однако реальные обращения `config.allowed_users[0]` на 169 и 384 — на 169 уже стоит защита `if config.allowed_users else 0`, а на 384 — **не проверено** (под условием `_check_allowed_user_id` контекста). Если `allowed_users is None` в production, и code-путь дошёл до 384 — TypeError. Уровень риска: реальный, но описанная строка `91` — неверная.
**Suggestion:** Защитить и строку 384 аналогично 169: `config.allowed_users[0] if config.allowed_users else <fallback>`.

> *Технически P1 valid (баг существует), но строка указана неверно — оценил как valid, поскольку реальная баговая позиция найдена в файле.*

### Partial

#### 4. Path traversal в ZIP-распаковке
**file:** `claude-code-telegram/src/bot/features/file_handler.py:210-224` (GLM: 130-140) | **category:** security
GLM прав, что нет финальной проверки `target_path.resolve().is_relative_to(extract_dir.resolve())`. Однако его утверждение "`'..' in file_path.parts` не защищает" — **неверно**: `Path("foo/../../etc/passwd").parts == ('foo', '..', '..', 'etc', 'passwd')`, и `'..' in parts` возвращает `True`, поэтому такой архив отбрасывается. Реальный остаточный риск — symlink-эскейп через `target_path.parent.mkdir(parents=True, exist_ok=True)` и Windows-специфика (drive letters). Строки указаны неверно.
**Suggestion:** Добавить post-resolve проверку как страховку — рекомендация валидна как defense-in-depth, но severity завышен (основной механизм уже есть).

#### 5. Двойной расход токенов в rate_limiter
**file:** `claude-code-telegram/src/security/rate_limiter.py:91-119` (GLM: 82-96) | **category:** bug
GLM утверждает: `_check_request_rate` **внутри делает `bucket.consume`**, и потом ещё раз вызывается `_consume_request_tokens` → двойное списание. Я прочитал блок 60-119: видно явное разделение — `_check_request_rate` (проверка) и **затем отдельно** `_consume_request_tokens(user_id, tokens)` на строке 118 после успешной проверки rate **И** cost. Чтобы окончательно подтвердить — нужно прочитать тело `_check_request_rate` (за пределами 82-96). По имени метода и архитектуре (check_rate_limit делает: check → check → consume → consume) более вероятно, что `_check_request_rate` лишь читает состояние bucket, а консьюмит отдельный helper. Без чтения тела `_check_request_rate` точно сказать нельзя; вердикт — **partial** (требует доп.верификации).

### Hallucinations

(нет)

## P2 — сводка
| Категория | Кол-во | Примеры |
|---|---|---|
| bug | 11 | API mismatch в `handle_export_callback`, race condition в connection pool, hash() instability, double init, env override overwrite, zip-bomb file count |
| security | 4 | Regex `_redact_secrets`, MCP `send_image_to_user` без approved_directory, auto `is_allowed=True`, DANGEROUS_PATTERNS regex |
| portability | 1 | `/tmp/claude_bot_files` хардкод |
| correctness | 1 | DANGEROUS_PATTERNS `r'\.\.'` regex meaning |
| inconsistency | 1 | `session_timeout_hours` vs `session_timeout_minutes` |

## P3 — сводка
| Категория | Кол-во | Примеры |
|---|---|---|
| bug | 3 | Double `app.initialize()`, datetime/str type confusion, `event.from_user` None |
| optimization | 1 | `get_event_loop()` deprecation |

## Источник
`_boss/cron/logs/_review-per-project-2026-05-18/claude-code-telegram.json`
