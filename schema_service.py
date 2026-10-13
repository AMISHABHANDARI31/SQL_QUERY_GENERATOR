from typing import Any

from database import (
    get_database_dialect,
    get_database_name,
    get_foreign_keys,
    get_indexes,
    list_accessible_databases,
    list_tables,
    get_primary_keys,
    get_schema,
    get_schema_cache_error,
    refresh_schema_cache,
    set_active_database,
    test_connection,
    validate_database_name,
)
from schema_analyzer import schema_as_prompt_text


class SchemaService:
    """Schema metadata service backed by INFORMATION_SCHEMA cache."""

    def list_databases(self) -> list[str]:
        return list_accessible_databases()

    def validate_database(self, database: str) -> bool:
        return validate_database_name(database)

    def select_database(self, database: str) -> dict[str, Any]:
        set_active_database(database)
        return self.get_schema(database)

    def refresh(self, database: str | None = None) -> dict[str, Any]:
        if database:
            set_active_database(database)
        refresh_schema_cache(database)
        return self.get_schema(database)

    def list_tables(self, database: str | None = None) -> list[str]:
        if database:
            self._ensure_database(database)
        return list_tables(database)

    def test_connection(self, database: str | None = None) -> dict[str, Any]:
        if database:
            self._ensure_database(database)
        return test_connection(database)

    def get_schema(self, database: str | None = None) -> dict[str, Any]:
        if database:
            self._ensure_database(database)
        columns = get_schema(database)
        primary_keys = get_primary_keys(database)
        foreign_keys = get_foreign_keys(database)
        indexes = get_indexes(database)

        return {
            "database": database or get_database_name(),
            "dialect": get_database_dialect(),
            "tables": {
                table_name: {
                    "columns": [
                        {
                            "name": column["column_name"],
                            "type": column["data_type"],
                            "primary_key": column["column_name"] in primary_keys.get(table_name, []),
                            "foreign_key": self._foreign_key_target(table_name, column["column_name"], foreign_keys),
                        }
                        for column in table_columns
                    ],
                    "indexes": indexes.get(table_name, {}),
                }
                for table_name, table_columns in columns.items()
            },
            "relationships": foreign_keys,
            "cache_error": get_schema_cache_error(database),
        }

    def get_relevant_schema_text(self, question: str, database: str | None = None) -> str:
        if database:
            self._ensure_database(database)
        terms = question.split()
        return schema_as_prompt_text(terms, database=database)

    def _ensure_database(self, database: str) -> None:
        if not self.validate_database(database):
            raise ValueError(f"Database '{database}' is not accessible with the configured MySQL credentials.")

    @staticmethod
    def _foreign_key_target(table_name: str, column_name: str, foreign_keys: list[dict[str, str]]) -> str | None:
        for key in foreign_keys:
            if key["table"] == table_name and key["column"] == column_name:
                return f'{key["referenced_table"]}.{key["referenced_column"]}'
        return None
