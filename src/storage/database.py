"""Database connection and initialization.

Features:
- Connection pooling
- Automatic migrations
- Health checks
- Schema versioning
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, List, Optional, Tuple, Union

import aiosqlite
import structlog

from ..exceptions import DatabaseConnectionError

logger = structlog.get_logger()


# Python 3.12+: sqlite3's default datetime adapter is deprecated.
# Register explicit adapters/converters once at import time to avoid warnings
# and keep consistent ISO-8601 persistence for datetime values.
sqlite3.register_adapter(datetime, lambda value: value.isoformat())
sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))
sqlite3.register_converter("DATETIME", lambda b: datetime.fromisoformat(b.decode()))
# Keep DATE columns as raw ISO strings (matches existing model expectations).
sqlite3.register_converter("DATE", lambda b: b.decode())

# Initial schema migration
INITIAL_SCHEMA = """
-- Core Tables

-- Users table
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    telegram_username TEXT,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_allowed BOOLEAN DEFAULT FALSE,
    total_cost REAL DEFAULT 0.0,
    message_count INTEGER DEFAULT 0,
    session_count INTEGER DEFAULT 0
);

-- Sessions table
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    project_path TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_cost REAL DEFAULT 0.0,
    total_turns INTEGER DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Messages table
CREATE TABLE messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    prompt TEXT NOT NULL,
    response TEXT,
    cost REAL DEFAULT 0.0,
    duration_ms INTEGER,
    error TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Tool usage table
CREATE TABLE tool_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message_id INTEGER,
    tool_name TEXT NOT NULL,
    tool_input JSON,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    success BOOLEAN DEFAULT TRUE,
    error_message TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (message_id) REFERENCES messages(message_id)
);

-- Audit log table
-- NOTE: user_id deliberately carries NO foreign key to users(user_id).
-- Audit records the *subject* of a security event, who need not be a
-- registered user (rejected auth attempts, unknown senders). See migration 8,
-- which drops this FK on databases created before that decision.
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_data JSON,
    success BOOLEAN DEFAULT TRUE,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ip_address TEXT
);

-- User tokens table (for token auth)
CREATE TABLE user_tokens (
    token_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    last_used TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Cost tracking table
CREATE TABLE cost_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date DATE NOT NULL,
    daily_cost REAL DEFAULT 0.0,
    request_count INTEGER DEFAULT 0,
    UNIQUE(user_id, date),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Indexes for performance
CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_project_path ON sessions(project_path);
CREATE INDEX idx_messages_session_id ON messages(session_id);
CREATE INDEX idx_messages_timestamp ON messages(timestamp);
CREATE INDEX idx_audit_log_user_id ON audit_log(user_id);
CREATE INDEX idx_audit_log_timestamp ON audit_log(timestamp);
CREATE INDEX idx_cost_tracking_user_date ON cost_tracking(user_id, date);
"""


class DatabaseManager:
    """Manage database connections and initialization."""

    def __init__(self, database_url: str):
        """Initialize database manager."""
        self.database_path = self._parse_database_url(database_url)
        self._connection_pool: List[aiosqlite.Connection] = []
        self._pool_size = 5
        self._pool_lock = asyncio.Lock()
        # Semaphore caps concurrent borrows at _pool_size so we never end up
        # with more live connections than the configured pool size, even on
        # a burst. Initialised lazily in initialize() once _pool_size is fixed.
        self._pool_semaphore: Optional[asyncio.Semaphore] = None
        # Set by close(); makes close() idempotent and stops get_connection()
        # from lazily reopening the database after shutdown.
        self._closed = False

    def _parse_database_url(self, database_url: str) -> Union[str, Path]:
        """Parse database URL to a path or the literal in-memory marker.

        In-memory forms (``sqlite:///:memory:`` / ``:memory:`` / empty path)
        are passed to aiosqlite as the literal string ``":memory:"`` rather
        than a filesystem Path so SQLite opens a transient in-memory database
        and no parent directory is created.
        """
        if database_url.startswith("sqlite:///"):
            path = database_url[10:]
        elif database_url.startswith("sqlite://"):
            path = database_url[9:]
        else:
            path = database_url

        if path in (":memory:", ""):
            return ":memory:"
        return Path(path)

    async def initialize(self):
        """Initialize database and run migrations."""
        logger.info("Initializing database", path=str(self.database_path))

        # Ensure directory exists (skip for the in-memory marker, which has no
        # backing file and therefore no parent directory to create).
        if isinstance(self.database_path, Path):
            self.database_path.parent.mkdir(parents=True, exist_ok=True)

        # Run migrations
        await self._run_migrations()

        # Initialize connection pool
        await self._init_pool()

        logger.info("Database initialization complete")

    async def _run_migrations(self):
        """Run database migrations."""
        async with aiosqlite.connect(
            self.database_path, detect_types=sqlite3.PARSE_DECLTYPES
        ) as conn:
            conn.row_factory = aiosqlite.Row

            # Enable foreign keys
            await conn.execute("PRAGMA foreign_keys = ON")

            # Get current version
            current_version = await self._get_schema_version(conn)
            logger.info("Current schema version", version=current_version)

            # Run migrations. executescript() performs NO implicit transaction
            # control of its own, so each migration's DDL and its schema_version
            # row are wrapped in one explicit transaction here: either both land
            # or neither does. Committing them separately is not atomic — the
            # DDL commits on its own and a crash before the version row leaves
            # migration 1 (INITIAL_SCHEMA, bare CREATE TABLE) to rerun on the
            # next start and fail with "table users already exists".
            # Consequence for migration scripts: they must NOT contain their own
            # BEGIN/COMMIT (it would nest) and must not run statements that
            # cannot execute inside a transaction (e.g. PRAGMA journal_mode).
            migrations = self._get_migrations()
            for version, migration in migrations:
                if version > current_version:
                    logger.info("Running migration", version=version)
                    await conn.executescript(
                        "BEGIN;\n"
                        f"{migration}\n"
                        f"INSERT INTO schema_version (version) VALUES ({version});\n"
                        "COMMIT;"
                    )

    async def _get_schema_version(self, conn: aiosqlite.Connection) -> int:
        """Get current schema version."""
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)

        cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        return row[0] if row and row[0] else 0

    def _get_migrations(self) -> List[Tuple[int, str]]:
        """Get migration scripts."""
        return [
            (1, INITIAL_SCHEMA),
            (
                2,
                """
                -- Add analytics views
                CREATE VIEW IF NOT EXISTS daily_stats AS
                SELECT
                    date(timestamp) as date,
                    COUNT(DISTINCT user_id) as active_users,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost,
                    AVG(duration_ms) as avg_duration
                FROM messages
                GROUP BY date(timestamp);

                CREATE VIEW IF NOT EXISTS user_stats AS
                SELECT
                    u.user_id,
                    u.telegram_username,
                    COUNT(DISTINCT s.session_id) as total_sessions,
                    COUNT(m.message_id) as total_messages,
                    SUM(m.cost) as total_cost,
                    MAX(m.timestamp) as last_activity
                FROM users u
                LEFT JOIN sessions s ON u.user_id = s.user_id
                LEFT JOIN messages m ON u.user_id = m.user_id
                GROUP BY u.user_id;
                """,
            ),
            (
                3,
                """
                -- Agentic platform tables

                -- Scheduled jobs for recurring agent tasks
                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    job_id TEXT PRIMARY KEY,
                    job_name TEXT NOT NULL,
                    cron_expression TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    target_chat_ids TEXT DEFAULT '',
                    working_directory TEXT NOT NULL,
                    skill_name TEXT,
                    created_by INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                -- Webhook events for deduplication and audit
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    delivery_id TEXT UNIQUE,
                    payload JSON,
                    processed BOOLEAN DEFAULT FALSE,
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_webhook_events_delivery
                    ON webhook_events(delivery_id);
                CREATE INDEX IF NOT EXISTS idx_webhook_events_provider
                    ON webhook_events(provider, received_at);
                CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_active
                    ON scheduled_jobs(is_active);

                -- NOTE: PRAGMA journal_mode=WAL is enabled in DatabaseManager.
                -- _init_pool() via a dedicated connection — it cannot run
                -- inside a transaction. It persists across opens so we only
                -- need to set it once.
                """,
            ),
            (
                4,
                """
                -- Project thread mapping for strict forum-topic routing
                CREATE TABLE IF NOT EXISTS project_threads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_slug TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_thread_id INTEGER NOT NULL,
                    topic_name TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, project_slug),
                    UNIQUE(chat_id, message_thread_id)
                );

                CREATE INDEX IF NOT EXISTS idx_project_threads_chat_active
                    ON project_threads(chat_id, is_active);
                CREATE INDEX IF NOT EXISTS idx_project_threads_slug
                    ON project_threads(project_slug);
                """,
            ),
            (
                5,
                """
                -- Performance indexes for tool-usage and per-user message lookups
                CREATE INDEX IF NOT EXISTS idx_tool_usage_session
                    ON tool_usage(session_id);
                CREATE INDEX IF NOT EXISTS idx_messages_user_id
                    ON messages(user_id, timestamp);
                """,
            ),
            (
                6,
                """
                -- Durable webhook retry: attempt counter, last error, last
                -- attempt time. ``processed`` becomes tri-state: 0=pending,
                -- 1=done, 2=dead-letter (retries exhausted).
                ALTER TABLE webhook_events ADD COLUMN attempts INTEGER NOT NULL
                    DEFAULT 0;
                ALTER TABLE webhook_events ADD COLUMN last_error TEXT;
                ALTER TABLE webhook_events ADD COLUMN last_attempt_at TIMESTAMP;
                CREATE INDEX IF NOT EXISTS idx_webhook_events_pending
                    ON webhook_events(processed, attempts);
                """,
            ),
            (
                7,
                """
                -- Expression indexes matching the retention-purge / windowed
                -- analytics predicates. Those queries filter on
                -- ``datetime(<col>) </> datetime('now', ...)``. Wrapping the
                -- indexed column in datetime() makes the plain
                -- column indexes (idx_messages_timestamp, ...) unusable, so
                -- every purge/analytics run degrades to a full table scan
                -- (under a write lock for the DELETEs). SQLite can use an index
                -- on the *expression* datetime(<col>) when the WHERE clause
                -- uses the exact same expression -- so these are additive and
                -- require no query rewrite or data migration. datetime(<col>)
                -- is deterministic (no 'now' argument), so it is indexable.
                CREATE INDEX IF NOT EXISTS idx_messages_ts_expr
                    ON messages(datetime(timestamp));
                CREATE INDEX IF NOT EXISTS idx_audit_log_ts_expr
                    ON audit_log(datetime(timestamp));
                CREATE INDEX IF NOT EXISTS idx_tool_usage_ts_expr
                    ON tool_usage(datetime(timestamp));
                CREATE INDEX IF NOT EXISTS idx_sessions_last_used_expr
                    ON sessions(datetime(last_used));
                CREATE INDEX IF NOT EXISTS idx_webhook_events_received_expr
                    ON webhook_events(datetime(received_at));
                """,
            ),
            (
                8,
                """
                -- Drop the FOREIGN KEY on audit_log.user_id.
                --
                -- audit_log records the *subject* of a security event. The auth
                -- middleware logs an auth attempt before any users row exists
                -- (the row is only created later by the handler/session layer),
                -- so on a fresh install the very first update from any user hit
                -- "FOREIGN KEY constraint failed" and neither the user nor the
                -- audit row was written. Creating a users row per inbound update
                -- is not an option -- any unauthorized sender could then inflate
                -- the users table -- so the constraint itself goes.
                --
                -- SQLite has no DROP CONSTRAINT, so the table is rebuilt
                -- (new -> copy -> drop -> rename). ``audit_log`` has no children
                -- referencing it, so no other table needs fixing up. The rebuild
                -- is atomic because _run_migrations wraps every migration in one
                -- transaction; DROP ... IF EXISTS keeps the script re-runnable.
                --
                -- Rollback: recreate audit_log with
                -- "FOREIGN KEY (user_id) REFERENCES users(user_id)" using the
                -- same rebuild dance and delete the version-8 row from
                -- schema_version. Rows whose user_id has no users entry must be
                -- purged first or the copy will fail.
                DROP TABLE IF EXISTS audit_log_new;

                CREATE TABLE audit_log_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    event_data JSON,
                    success BOOLEAN DEFAULT TRUE,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    ip_address TEXT
                );

                INSERT INTO audit_log_new
                    (id, user_id, event_type, event_data, success, timestamp,
                     ip_address)
                SELECT id, user_id, event_type, event_data, success, timestamp,
                       ip_address
                FROM audit_log;

                DROP TABLE audit_log;
                ALTER TABLE audit_log_new RENAME TO audit_log;

                -- Indexes lived on the dropped table; recreate them.
                CREATE INDEX IF NOT EXISTS idx_audit_log_user_id
                    ON audit_log(user_id);
                CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp
                    ON audit_log(timestamp);
                CREATE INDEX IF NOT EXISTS idx_audit_log_ts_expr
                    ON audit_log(datetime(timestamp));
                """,
            ),
            (
                9,
                """
                -- Persist the two AuditEvent fields the table never had.
                --
                -- ``AuditEvent`` carries ``risk_level`` and ``session_id``, but
                -- audit_log had no columns for them, so SQLiteAuditStorage
                -- silently dropped both on write and rebuilt every event with
                -- risk_level="low" on read. The security dashboard and the
                -- per-user activity summary therefore reported {"low": N} for
                -- everything, including logged security violations.
                --
                -- Both columns are nullable with no default: rows written
                -- before this migration have no recorded risk level, and the
                -- read path maps NULL back to the "low" dataclass default.
                --
                -- Rollback: SQLite cannot DROP COLUMN before 3.35; rebuild
                -- audit_log without the two columns (see migration 8 for the
                -- new -> copy -> drop -> rename dance) and delete the version-9
                -- row from schema_version.
                ALTER TABLE audit_log ADD COLUMN risk_level TEXT;
                ALTER TABLE audit_log ADD COLUMN session_id TEXT;
                """,
            ),
        ]

    async def _init_pool(self):
        """Initialize connection pool."""
        logger.info("Initializing connection pool", size=self._pool_size)

        # WAL must be set OUTSIDE any open transaction and persists across
        # opens, so we set it once via a one-shot connection here.
        try:
            async with aiosqlite.connect(self.database_path) as conn:
                await conn.execute("PRAGMA journal_mode=WAL")
        except Exception as e:
            # Non-fatal: fall back to default journal_mode (DELETE).
            logger.warning("Failed to enable WAL journal mode", error=str(e))

        self._pool_semaphore = asyncio.Semaphore(self._pool_size)
        async with self._pool_lock:
            for _ in range(self._pool_size):
                conn = await aiosqlite.connect(
                    self.database_path, detect_types=sqlite3.PARSE_DECLTYPES
                )
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA foreign_keys = ON")
                # busy_timeout is per-connection (not persisted): make a
                # concurrent writer wait for the WAL write lock instead of
                # failing immediately with SQLITE_BUSY.
                await conn.execute("PRAGMA busy_timeout=5000")
                self._connection_pool.append(conn)

    @asynccontextmanager
    async def get_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get database connection from pool.

        Concurrent borrows are capped at ``_pool_size`` by an asyncio.Semaphore
        so we never have more live connections than the pool can hold even
        under burst load.

        Raises ``DatabaseConnectionError`` once :meth:`close` has run: without
        this guard the lazy-open branch below would silently open a brand new
        connection and repopulate the pool after shutdown.
        """
        if self._closed:
            raise DatabaseConnectionError(
                "Database manager is closed; no new connections can be acquired"
            )

        if self._pool_semaphore is None:
            # Pool not yet initialised; emulate sequential behaviour by opening
            # a transient connection. Should only happen during early startup.
            transient_conn = await aiosqlite.connect(
                self.database_path, detect_types=sqlite3.PARSE_DECLTYPES
            )
            transient_conn.row_factory = aiosqlite.Row
            await transient_conn.execute("PRAGMA foreign_keys = ON")
            await transient_conn.execute("PRAGMA busy_timeout=5000")
            try:
                yield transient_conn
            finally:
                # Roll back any half-finished write so it isn't implicitly
                # committed/leaked on close (e.g. on cancellation or error).
                if transient_conn.in_transaction:
                    await transient_conn.rollback()
                await transient_conn.close()
            return

        await self._pool_semaphore.acquire()
        conn: Optional[aiosqlite.Connection] = None
        try:
            async with self._pool_lock:
                if self._connection_pool:
                    conn = self._connection_pool.pop()
            if conn is None:
                conn = await aiosqlite.connect(
                    self.database_path, detect_types=sqlite3.PARSE_DECLTYPES
                )
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA foreign_keys = ON")
                await conn.execute("PRAGMA busy_timeout=5000")

            yield conn
        finally:
            # Always release the semaphore permit, even if rolling back or
            # closing the connection raises. Otherwise a failed await below
            # would leak a permit and, after _pool_size leaks, acquire() would
            # block forever.
            try:
                released = False
                if conn is not None:
                    # Roll back any open write transaction before the connection
                    # goes back to the pool. If a task was cancelled or raised
                    # between execute() and commit(), an open transaction would
                    # otherwise be inherited by the next borrower and either
                    # implicitly committed or cause "database is locked".
                    if conn.in_transaction:
                        await conn.rollback()
                    async with self._pool_lock:
                        if len(self._connection_pool) < self._pool_size:
                            self._connection_pool.append(conn)
                            released = True
                    if not released:
                        await conn.close()
            finally:
                self._pool_semaphore.release()

    async def close(self):
        """Close all pooled connections and mark the manager closed.

        Idempotent: a second call is a no-op. NOTE: connections currently
        borrowed by in-flight callers are *not* drained — such a borrower keeps
        using its connection until it finishes and then hands it back to the
        (already cleared) pool. Only the reopen-after-close path is closed here.
        """
        if self._closed:
            return

        logger.info("Closing database connections")

        async with self._pool_lock:
            self._closed = True
            for conn in self._connection_pool:
                await conn.close()
            self._connection_pool.clear()

    async def health_check(self) -> bool:
        """Check database health.

        Reports False once the manager is closed rather than reopening the
        database to answer the question.
        """
        if self._closed:
            return False

        try:
            async with self.get_connection() as conn:
                await conn.execute("SELECT 1")
                return True
        except Exception as e:
            logger.error("Database health check failed", error=str(e))
            return False
