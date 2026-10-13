import sqlite3
import time
import json
from datetime import datetime
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).with_name("query_history.sqlite3")


def init_history_db() -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS query_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                natural_language_prompt TEXT,
                sql_query TEXT NOT NULL,
                query_type TEXT,
                status TEXT NOT NULL,
                execution_time REAL,
                rows_affected INTEGER,
                error_message TEXT,
                user_id INTEGER,
                generation_mode TEXT,
                database_name TEXT,
                tables TEXT,
                assumptions TEXT,
                explanation TEXT,
                validation_result TEXT,
                result_summary TEXT
            )
            """
        )
        existing_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(query_history)").fetchall()
        }
        if "tables" not in existing_columns:
            connection.execute("ALTER TABLE query_history ADD COLUMN tables TEXT")
        if "result_summary" not in existing_columns:
            connection.execute("ALTER TABLE query_history ADD COLUMN result_summary TEXT")
        if "database_name" not in existing_columns:
            connection.execute("ALTER TABLE query_history ADD COLUMN database_name TEXT")
        if "generation_mode" not in existing_columns:
            connection.execute("ALTER TABLE query_history ADD COLUMN generation_mode TEXT")
        if "user_id" not in existing_columns:
            connection.execute("ALTER TABLE query_history ADD COLUMN user_id INTEGER")
        if "assumptions" not in existing_columns:
            connection.execute("ALTER TABLE query_history ADD COLUMN assumptions TEXT")
        if "explanation" not in existing_columns:
            connection.execute("ALTER TABLE query_history ADD COLUMN explanation TEXT")
        if "validation_result" not in existing_columns:
            connection.execute("ALTER TABLE query_history ADD COLUMN validation_result TEXT")


def save_history(
    *,
    sql_query: str,
    query_type: str,
    status: str,
    natural_language_prompt: str | None = None,
    execution_time: float | None = None,
    rows_affected: int | None = None,
    error_message: str | None = None,
    user_id: int | None = None,
    generation_mode: str | None = None,
    database: str | None = None,
    tables: list[str] | None = None,
    assumptions: list[str] | dict[str, Any] | None = None,
    explanation: str | None = None,
    validation_result: dict[str, Any] | None = None,
    result_summary: str | None = None,
) -> int:
    init_history_db()
    with sqlite3.connect(DB_PATH) as connection:
        cursor = connection.execute(
            """
            INSERT INTO query_history (
                created_at, natural_language_prompt, sql_query, query_type,
                status, execution_time, rows_affected, error_message,
                user_id, generation_mode, database_name, tables, assumptions,
                explanation, validation_result, result_summary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                natural_language_prompt,
                sql_query,
                query_type,
                status,
                execution_time,
                rows_affected,
                error_message,
                user_id,
                generation_mode,
                database,
                json.dumps(tables or []),
                json.dumps(assumptions or []),
                explanation,
                json.dumps(validation_result or {}),
                result_summary,
            ),
        )
        return int(cursor.lastrowid)


def get_history(limit: int = 50, user_id: int | None = None) -> list[dict[str, Any]]:
    init_history_db()
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        if user_id is None:
            rows = connection.execute(
                "SELECT * FROM query_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM query_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
    return [dict(row) for row in rows]


def get_history_item(history_id: int) -> dict[str, Any] | None:
    init_history_db()
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM query_history WHERE id = ?",
            (history_id,),
        ).fetchone()
    return dict(row) if row else None


def timer() -> float:
    return time.perf_counter()
