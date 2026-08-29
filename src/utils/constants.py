"""Application-wide constants.

Only values that are actually imported live here. Security data in particular
(dangerous patterns, allowed extensions) belongs to ``SecurityValidator`` and is
deliberately *not* duplicated: a second, drifted copy of a blocklist invites
someone to wire it up without noticing it is stricter, looser or simply stale —
the same reasoning that removed ``threat_detection_middleware``.
"""

# Version info
APP_NAME = "Claude Code Telegram Bot"
APP_DESCRIPTION = "Telegram bot for remote Claude Code access"

# Default limits
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 300
DEFAULT_CLAUDE_MAX_TURNS = 10
DEFAULT_CLAUDE_MAX_COST_PER_USER = 10.0
DEFAULT_CLAUDE_MAX_COST_PER_REQUEST = 5.0
DEFAULT_AUTOMATION_MAX_COST_PER_DAY = 5.0

DEFAULT_RATE_LIMIT_REQUESTS = 10
DEFAULT_RATE_LIMIT_WINDOW = 60
DEFAULT_RATE_LIMIT_BURST = 20
DEFAULT_PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS = 1.1

DEFAULT_SESSION_TIMEOUT_HOURS = 24
DEFAULT_MAX_SESSIONS_PER_USER = 5

# Message limits
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Session limits
MAX_SESSION_LENGTH = 1000  # Maximum messages per session

# Database defaults
DEFAULT_DATABASE_URL = "sqlite:///data/bot.db"

# Retry defaults (transient SDK connection errors)
DEFAULT_RETRY_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY = 1.0
DEFAULT_RETRY_BACKOFF_FACTOR = 3.0
DEFAULT_RETRY_MAX_DELAY = 30.0

# Synthetic subject for runs the bus starts on its own (webhooks, cron). Telegram
# user ids are positive, so this can never collide with a real user. Automation
# runs are attributed to it instead of to ``ALLOWED_USERS[0]`` so their spend has
# its own daily budget and their history does not evict the owner's sessions.
AUTOMATION_USER_ID = -1
