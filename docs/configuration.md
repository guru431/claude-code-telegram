# Configuration Guide

This document provides comprehensive information about configuring the Claude Code Telegram Bot.

## Overview

The bot uses a configuration system built with Pydantic Settings v2 that provides:

- **Type Safety**: All configuration values are validated and type-checked
- **Environment Support**: Automatic environment-specific overrides
- **Feature Flags**: Dynamic enabling/disabling of functionality
- **Validation**: Cross-field validation and runtime checks

## Configuration Sources

Highest precedence first:

1. **Environment variables** — an explicitly set variable always wins. It beats
   the `.env` file (pydantic-settings ranks the process environment above the
   dotenv source) *and* the environment profile: `_apply_environment_overrides`
   skips every field present in `model_fields_set`, so `RATE_LIMIT_REQUESTS=20`
   survives `ENVIRONMENT=production`.
2. **`.env` file** (if present) — loaded into the process environment at
   startup, so values there are also "set" for the rule above.
3. **Environment-specific overrides** (development/testing/production) — applied
   only to fields nobody set explicitly.
4. **Default values** defined in the Settings class.

## Environment Variables

### Required Settings

```bash
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_BOT_USERNAME=your_bot_name

# Security
APPROVED_DIRECTORY=/path/to/your/projects
```

### Optional Settings

#### User Access Control

```bash
# Comma-separated list of allowed Telegram user IDs
ALLOWED_USERS=123456789,987654321
```

The Telegram ID whitelist is the only authentication method. There is no
token-login flow.

#### Security Relaxation (Trusted Environments Only)

```bash
# Disable dangerous pattern validation in SecurityValidator (default: false)
# WARNING: This allows characters such as pipes and redirections in validated paths.
DISABLE_SECURITY_PATTERNS=false

# Hand the SDK no tool restrictions at all (default: false)
# WARNING: passes BOTH allowed_tools=None AND disallowed_tools=None to the SDK, so
# CLAUDE_DISALLOWED_TOOLS stops being enforced too. Per-call path checks
# (can_use_tool) and Bash directory-boundary checks still apply. An explicit
# per-run tool override (e.g. the read-only webhook set) is not widened.
DISABLE_TOOL_VALIDATION=false
```

#### Claude Configuration

```bash
# Authentication
ANTHROPIC_API_KEY=sk-ant-api03-...    # Optional: API key for SDK (uses CLI auth if omitted)

# Maximum conversation turns before requiring new session
CLAUDE_MAX_TURNS=10

# Timeout for Claude operations in seconds
CLAUDE_TIMEOUT_SECONDS=300

# Maximum cost per user in USD (daily budget for the rate limiter; the counter
# resets when the UTC calendar day rolls over, matching the cost_tracking table)
CLAUDE_MAX_COST_PER_USER=10.0

# Maximum cost per individual request in USD (SDK-level hard cap)
CLAUDE_MAX_COST_PER_REQUEST=5.0

# Allowed Claude tools (comma-separated list; see docs/tools.md for descriptions)
CLAUDE_ALLOWED_TOOLS=Read,Write,Edit,Bash,Glob,Grep,LS,Task,TaskOutput,MultiEdit,NotebookRead,NotebookEdit,WebFetch,TodoRead,TodoWrite,WebSearch,Skill,AskUserQuestion,EnterPlanMode,ExitPlanMode
```

#### Rate Limiting

```bash
# Number of requests allowed per window
RATE_LIMIT_REQUESTS=10

# Rate limit window in seconds
RATE_LIMIT_WINDOW=60

# Burst capacity for rate limiting
RATE_LIMIT_BURST=20
```

#### Storage & Database

```bash
# Database URL (SQLite by default)
DATABASE_URL=sqlite:///data/bot.db

# Session management
SESSION_TIMEOUT_HOURS=24           # Session timeout in hours
MAX_SESSIONS_PER_USER=5            # Max concurrent sessions per user

# Data retention
DATA_RETENTION_DAYS=90            # Days to keep old data
AUDIT_LOG_RETENTION_DAYS=365     # Days to keep audit logs
```

#### Mode Selection

```bash
# Agentic mode (default: true)
# true  = conversational mode: /start, /new, /status, /verbose, /repo,
#         /sessions, /schedule, /events, /restart (+ /sync_threads when
#         ENABLE_PROJECT_THREADS=true)
# false = classic terminal mode with the full command set and inline keyboards
AGENTIC_MODE=true
```

#### Feature Flags

```bash
# Enable Model Context Protocol
ENABLE_MCP=false
MCP_CONFIG_PATH=/path/to/mcp/config.json

# Enable Git integration (classic mode)
ENABLE_GIT_INTEGRATION=true

# Enable file upload handling
ENABLE_FILE_UPLOADS=true

# Enable quick action buttons (classic mode)
ENABLE_QUICK_ACTIONS=true

# Enable voice message transcription
ENABLE_VOICE_MESSAGES=true
VOICE_PROVIDER=mistral              # 'mistral' (default) or 'openai'
MISTRAL_API_KEY=                     # Required when VOICE_PROVIDER=mistral
OPENAI_API_KEY=                      # Required when VOICE_PROVIDER=openai
VOICE_TRANSCRIPTION_MODEL=           # Default: voxtral-mini-latest (Mistral) or whisper-1 (OpenAI)
VOICE_MAX_FILE_SIZE_MB=20            # Max Telegram voice file size to download (1-200MB)
```

#### Agentic Platform

```bash
# Webhook API Server
ENABLE_API_SERVER=false               # Enable FastAPI webhook server
API_SERVER_PORT=8080                  # Server port (default: 8080)

# Webhook Authentication
GITHUB_WEBHOOK_SECRET=your-secret    # GitHub HMAC-SHA256 secret
WEBHOOK_API_SECRET=your-secret       # Bearer token for generic providers

# Job Scheduler
ENABLE_SCHEDULER=false                # Enable cron job scheduler

# Notifications
NOTIFICATION_CHAT_IDS=123456,789012  # Default Telegram chat IDs for proactive notifications
```

#### Project Thread Mode

```bash
# Strict project routing via Telegram project topics
ENABLE_PROJECT_THREADS=false

# Mode: private (default) or group
PROJECT_THREADS_MODE=private

# YAML registry file with project slugs/names/paths
PROJECTS_CONFIG_PATH=config/projects.yaml

# Required only for PROJECT_THREADS_MODE=group
PROJECT_THREADS_CHAT_ID=-1001234567890

# Minimum delay (seconds) between Telegram API calls during topic sync
# Set 0 to disable pacing
PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS=1.1
```

`PROJECTS_CONFIG_PATH` schema:

```yaml
projects:
  - slug: my-app
    name: My App
    path: my-app
    enabled: true
```

When `ENABLE_PROJECT_THREADS=true`:
- `PROJECT_THREADS_MODE=private`:
  - `/start` and `/sync_threads` are allowed outside topics in private chat.
  - all other updates must be inside mapped project topics.
- `PROJECT_THREADS_MODE=group`:
  - behavior remains forum-topic based using `PROJECT_THREADS_CHAT_ID`.

#### Automation Budget

```bash
# Daily budget for runs the event bus starts on its own (webhooks, scheduled
# jobs). These runs are attributed to a synthetic automation subject, not to
# ALLOWED_USERS[0], so they neither spend a person's budget nor evict their
# sessions — and they are no longer unmetered.
AUTOMATION_MAX_COST_PER_DAY=5.0
```

#### Uploads

```bash
# Extra extensions accepted for uploads, on top of the built-in allowlist.
# With or without the leading dot.
UPLOAD_EXTRA_EXTENSIONS=.parquet,svg
```

#### Tool Path Boundary

```bash
# What file tools and bash path checks are confined to.
#   approved (default) — APPROVED_DIRECTORY, as before
#   working            — the run's own project directory, so in project-thread
#                        mode a topic cannot read or write a sibling project
TOOL_PATH_BOUNDARY=approved
```

#### Project Settings Trust

```bash
# Load <working_directory>/.claude/settings.json as trusted agent configuration.
# Off by default: hooks declared there execute arbitrary commands, which makes
# the file a stronger vector than the CLAUDE.md the same run already wraps as
# untrusted data. When on, the bot logs the load and the hook names it found.
TRUST_PROJECT_SETTINGS=false
```

#### Link Intake

```bash
# Route messages containing links through an external link-analysis pipeline.
# All four paths are REQUIRED when enabled — the fetcher lives outside this
# repository, so there is no portable default and startup fails without them
# (and if LINK_INTAKE_FETCH_SCRIPT does not exist).
ENABLE_LINK_INTAKE=false
LINK_INTAKE_PYTHON=/usr/bin/python3
LINK_INTAKE_FETCH_SCRIPT=/opt/link-analysis/fetch_source.py
LINK_INTAKE_WORK_ROOT=/var/lib/link-analysis/incoming
LINK_INTAKE_REGISTRY=/opt/link-analysis/project-registry.json
```

#### Monitoring & Logging

```bash
# Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
LOG_LEVEL=INFO
```

There is no telemetry or error-reporting integration. `ENABLE_TELEMETRY` and
`SENTRY_DSN` existed as settings that nothing ever read — no Sentry
initialization, no metrics — and were removed rather than left to imply a
monitoring control that was not there.

#### Development

```bash
# Enable debug mode
DEBUG=false

# Enable development features
DEVELOPMENT_MODE=false

# Environment override (development, testing, production).
# Defaults to "production" when unset — every difference in the development
# profile is a relaxation (open /docs, ALLOW_ALL_USERS honoured, 10x looser
# rate limit), so an unconfigured deployment must not land there.
ENVIRONMENT=development
```

#### Webhook (Telegram Polling vs Webhook)

```bash
# Webhook URL for bot (leave empty for polling mode)
WEBHOOK_URL=https://your-domain.com/webhook

# Webhook port
WEBHOOK_PORT=8443

# Webhook path
WEBHOOK_PATH=/webhook

# Bind address (defaults to loopback; use 0.0.0.0 only when the listener must
# be reachable directly rather than through a reverse proxy)
WEBHOOK_LISTEN=127.0.0.1

# Secret token -- REQUIRED when WEBHOOK_URL is set
TELEGRAM_WEBHOOK_SECRET=
```

**Webhook mode is fail-closed.** If `WEBHOOK_URL` is set but
`TELEGRAM_WEBHOOK_SECRET` is empty, the bot refuses to start. The secret is
passed to Telegram's `setWebhook` and echoed back in the
`X-Telegram-Bot-Api-Secret-Token` header of every delivery; updates whose
header does not match are dropped before any handler runs. Without it, anyone
who can reach the listening port can POST a forged `Update` carrying an allowed
user's ID and pass the whitelist all the way to Claude tools or `/restart`.

Generate a secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The value must be 1-256 characters of `A-Z`, `a-z`, `0-9`, `_` or `-`.

## Environment-Specific Configuration

The bot automatically applies different settings based on the environment:

### Development Environment

Activated when `ENVIRONMENT=development`. (`DEBUG=true` alone does *not* select
this profile — only `ENVIRONMENT` chooses one.) Note that `production` is the
default when `ENVIRONMENT` is unset, and every difference below is a relaxation,
so the bot logs a warning listing them when this profile is active:

- `debug = true`
- `development_mode = true`
- `log_level = "DEBUG"`
- `rate_limit_requests = 100` (more lenient)
- `claude_timeout_seconds = 600` (longer timeout)

### Testing Environment

Activated when `ENVIRONMENT=testing`:

- `debug = true`
- `database_url = "sqlite:///:memory:"` (in-memory database)
- `approved_directory = <platform temp dir>/test_projects` (`tempfile.gettempdir()`, not a hardcoded `/tmp` — the suite also runs on Windows)
- `claude_timeout_seconds = 30` (faster timeout)
- `rate_limit_requests = 1000` (no effective rate limiting)

### Production Environment

Activated when `ENVIRONMENT=production`, and the default when `ENVIRONMENT` is
unset:

- `debug = false`
- `log_level = "INFO"`
- `claude_max_cost_per_user = 5.0` (stricter cost limit)
- `claude_max_cost_per_request = 2.0` (per-request SDK cap)
- `rate_limit_requests = 5` (stricter rate limiting)
- `session_timeout_hours = 12` (shorter session timeout)

## Feature Flags

Feature flags allow you to enable or disable functionality dynamically:

```python
from src.config import load_config, FeatureFlags

config = load_config()
features = FeatureFlags(config)

if features.agentic_mode_enabled:
    # Use agentic mode handlers
    pass

if features.api_server_enabled:
    # Start webhook API server
    pass
```

Available feature flags:

- `agentic_mode_enabled`: Agentic conversational mode (default: true)
- `api_server_enabled`: Webhook API server
- `scheduler_enabled`: Cron job scheduler
- `mcp_enabled`: Model Context Protocol support
- `git_enabled`: Git integration commands
- `file_uploads_enabled`: File upload handling
- `quick_actions_enabled`: Quick action buttons
- `webhook_enabled`: Telegram webhook mode (vs polling)
- `voice_messages_enabled`: Voice message transcription (default: true)
- `development_features_enabled`: Development-only features

## Validation

The configuration system performs extensive validation:

### Path Validation

- `APPROVED_DIRECTORY` must exist and be accessible
- `MCP_CONFIG_PATH` must exist if MCP is enabled

### Cross-Field Validation

- `MCP_CONFIG_PATH` is required when `ENABLE_MCP=true`
- `TELEGRAM_WEBHOOK_SECRET` is required when `WEBHOOK_URL` is set (and must match `[A-Za-z0-9_-]{1,256}`)
- `PROJECT_THREADS_CHAT_ID` is required when `PROJECT_THREADS_MODE=group`, and `PROJECTS_CONFIG_PATH` is required whenever `ENABLE_PROJECT_THREADS=true`

The voice provider's API key (`MISTRAL_API_KEY` / `OPENAI_API_KEY`) is **not**
checked at startup: a missing key only disables transcription, and a voice
message is answered with a notice naming the variable to set. The bot starts
normally, which is what keeps the default `ENABLE_VOICE_MESSAGES=true` usable
without a voice provider.

### Value Validation

- `LOG_LEVEL` must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL
- `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_WINDOW`, `RATE_LIMIT_BURST`, `CLAUDE_MAX_TURNS` and `CLAUDE_TIMEOUT_SECONDS` must be greater than 0
- `CLAUDE_RETRY_MAX_ATTEMPTS` must be ≥ 0 (0 disables retries); `VERBOSE_LEVEL` must be 0-2
- User IDs in `ALLOWED_USERS` must be valid integers

## Claude Integration Options

### Authentication Options

#### Option 1: Use Existing Claude CLI Authentication (Recommended)
```bash
# No ANTHROPIC_API_KEY needed - SDK will use CLI credentials
# Ensure Claude CLI is installed and authenticated: claude auth login
```

#### Option 2: Direct API Key
```bash
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
```

## Troubleshooting

### Common Issues

1. **"Approved directory does not exist"**
   - Ensure the path in `APPROVED_DIRECTORY` exists
   - Use absolute paths, not relative paths
   - Check file permissions

2. **"MCP config file does not exist"**
   - Ensure `MCP_CONFIG_PATH` points to an existing file
   - Or disable MCP with `ENABLE_MCP=false`

## Security Considerations

- **Never commit secrets** to version control
- **Use environment variables** for sensitive data
- **Restrict `APPROVED_DIRECTORY`** to only necessary paths
- **Monitor logs** for configuration errors and security events
