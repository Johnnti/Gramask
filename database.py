"""SQLite storage. Anonymous MVP data is retained in quarantined legacy tables."""
import os
import sqlite3
from contextlib import contextmanager
from config import settings
from knowledge import init_index

@contextmanager
def db():
    connection = sqlite3.connect(settings.db_path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=15000')
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(settings.db_path)), exist_ok=True)
    with db() as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('BEGIN IMMEDIATE')
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'queries' in tables:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(queries)')}
            if 'user_id' not in columns:
                for name in ('facts', 'crowd_questions', 'crowd_answers', 'queries', 'notifications'):
                    if name in tables:
                        target = 'legacy_' + name
                        if target in tables:
                            raise RuntimeError('Legacy migration needs manual review: duplicate archive table')
                        connection.execute(f'ALTER TABLE "{name}" RENAME TO "{target}"')
        schema = '''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL, password_hash TEXT, created_at REAL NOT NULL,
            is_simulated INTEGER NOT NULL DEFAULT 0, demo_owner_id INTEGER REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS crowd_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, attribute TEXT NOT NULL,
            prompt TEXT NOT NULL, answer_type TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'campus',
            question_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
            asked_by INTEGER NOT NULL REFERENCES users(id), created_at REAL NOT NULL, closes_at REAL NOT NULL,
            demo_owner_id INTEGER REFERENCES users(id), engine TEXT NOT NULL DEFAULT 'gemma3',
            next_attempt_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS crowd_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL REFERENCES crowd_questions(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id), value TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(question_id, user_id));
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, attribute TEXT NOT NULL,
            question TEXT NOT NULL, question_key TEXT NOT NULL, answer_type TEXT NOT NULL, category TEXT NOT NULL,
            value TEXT NOT NULL, confidence REAL NOT NULL, n_votes INTEGER NOT NULL,
            created_at REAL NOT NULL, valid_until REAL NOT NULL,
            crowd_question_id INTEGER NOT NULL UNIQUE REFERENCES crowd_questions(id) ON DELETE CASCADE,
            demo_owner_id INTEGER REFERENCES users(id), engine TEXT NOT NULL DEFAULT 'gemma3');
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
            text TEXT NOT NULL, status TEXT NOT NULL, answer TEXT,
            crowd_question_id INTEGER REFERENCES crowd_questions(id) ON DELETE CASCADE,
            created_at REAL NOT NULL, answered_at REAL, source TEXT NOT NULL, classification TEXT,
            confidence REAL, n_votes INTEGER, valid_until REAL, source_url TEXT,
            engine TEXT NOT NULL DEFAULT 'gemma3', demo_owner_id INTEGER REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, query_id INTEGER REFERENCES queries(id) ON DELETE CASCADE,
            crowd_question_id INTEGER REFERENCES crowd_questions(id) ON DELETE CASCADE,
            demo_owner_id INTEGER REFERENCES users(id));
        CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_queries_user ON queries(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_queries_crowd ON queries(crowd_question_id, user_id);
        CREATE INDEX IF NOT EXISTS idx_crowd_open ON crowd_questions(status, closes_at);
        CREATE INDEX IF NOT EXISTS idx_crowd_key ON crowd_questions(question_key, demo_owner_id, status);
        CREATE INDEX IF NOT EXISTS idx_answers_question ON crowd_answers(question_id);
        CREATE INDEX IF NOT EXISTS idx_facts_fresh ON facts(question_key, demo_owner_id, valid_until);
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at DESC);
        '''
        for statement in schema.split(';'):
            if statement.strip():
                connection.execute(statement)
        columns = {row[1] for row in connection.execute('PRAGMA table_info(crowd_questions)')}
        if 'next_attempt_at' not in columns:
            connection.execute('ALTER TABLE crowd_questions ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0')
        for table in ('queries', 'facts'):
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
            if 'report_summary' not in columns:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN report_summary TEXT')
        # Bring older long-lived records and their history labels under the
        # same freshness policy. Do not refresh timestamps during migration.
        connection.execute('''UPDATE facts SET created_at=MIN(created_at, COALESCE(
            (SELECT MIN(a.created_at) FROM crowd_answers a WHERE a.question_id=facts.crowd_question_id), created_at))''')
        connection.execute('UPDATE facts SET valid_until=MIN(valid_until, created_at+?)', (settings.live_ttl_seconds,))
        connection.execute('''UPDATE queries SET valid_until=MIN(valid_until,
            (SELECT f.valid_until FROM facts f WHERE f.crowd_question_id=queries.crowd_question_id))
            WHERE valid_until IS NOT NULL AND EXISTS
            (SELECT 1 FROM facts f WHERE f.crowd_question_id=queries.crowd_question_id)''')
        # Import here to keep configuration and initialization explicit.
        from llm import STATIC_FACTS
        init_index(connection, STATIC_FACTS)
        connection.execute('PRAGMA user_version=4')
