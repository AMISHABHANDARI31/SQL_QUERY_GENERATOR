import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from database import get_engine
from schema_analyzer import get_query_entities
from sql_validator import get_operation

READ_ONLY_OPERATIONS = {"SELECT", "SHOW", "DESCRIBE", "EXPLAIN"}
DDL_OPERATIONS = {"CREATE", "ALTER", "DROP", "TRUNCATE"}
DML_OPERATIONS = {"INSERT", "UPDATE", "DELETE"}


def analyze_query_impact(sql_query: str, database: str | None = None) -> dict[str, Any]:
    operation = get_operation(sql_query)
    entities = get_query_entities(sql_query, database)
    estimated_rows = _estimate_rows(sql_query, operation, database)
    risk_level = _risk_level(operation, estimated_rows)
    has_where = bool(re.search(r"\bWHERE\b", sql_query, flags=re.IGNORECASE))

    may_affect_entire_table = operation in {"UPDATE", "DELETE"} and not has_where
    return {
        "operation": operation,
        "tables": entities["tables"],
        "columns": entities["columns"],
        "modified_columns": _modified_columns(sql_query, operation),
        "estimated_rows": estimated_rows,
        "estimate_note": "Estimated from the current database when possible; null means it could not be estimated safely.",
        "risk_level": risk_level,
        "warning": _warning(operation, estimated_rows, may_affect_entire_table),
        "impact": _impact(operation, estimated_rows),
        "requires_confirmation": operation in DML_OPERATIONS | DDL_OPERATIONS,
        "destructive": operation in {"DROP", "TRUNCATE", "ALTER", "DELETE"},
        "may_affect_entire_table": may_affect_entire_table,
    }


def _estimate_rows(sql_query: str, operation: str, database: str | None = None) -> int | None:
    if operation == "SELECT":
        return _safe_count(f"SELECT COUNT(*) AS count_value FROM ({sql_query.rstrip(';')}) AS generated_query", database)
    if operation == "DELETE":
        count_sql = _delete_to_count(sql_query)
        return _safe_count(count_sql, database) if count_sql else None
    if operation == "UPDATE":
        count_sql = _update_to_count(sql_query)
        return _safe_count(count_sql, database) if count_sql else None
    if operation == "INSERT":
        values_match = re.search(r"\bVALUES\b(.*)$", sql_query, flags=re.IGNORECASE | re.DOTALL)
        values_groups = re.findall(r"\([^()]*\)", values_match.group(1)) if values_match else []
        return max(1, len(values_groups))
    return None


def _delete_to_count(sql_query: str) -> str | None:
    match = re.search(r"\bDELETE\s+FROM\s+([\w`]+)(.*)", sql_query, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    table = match.group(1)
    tail = match.group(2).rstrip(";")
    where_match = re.search(r"\bWHERE\b.*", tail, flags=re.IGNORECASE | re.DOTALL)
    return f"SELECT COUNT(*) AS count_value FROM {table} {where_match.group(0) if where_match else ''}"


def _update_to_count(sql_query: str) -> str | None:
    match = re.search(r"\bUPDATE\s+([\w`]+)\s+SET\b.*?(\bWHERE\b.*)?$", sql_query.rstrip(";"), flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    table = match.group(1)
    where_clause = match.group(2) or ""
    return f"SELECT COUNT(*) AS count_value FROM {table} {where_clause}"


def _safe_count(count_sql: str, database: str | None = None) -> int | None:
    try:
        with get_engine(database).connect() as connection:
            row = connection.execute(text(count_sql)).fetchone()
        return int(row[0]) if row else None
    except SQLAlchemyError:
        return None


def _risk_level(operation: str, estimated_rows: int | None) -> str:
    if operation in READ_ONLY_OPERATIONS:
        return "LOW"
    if operation == "INSERT":
        return "MEDIUM"
    if operation == "UPDATE":
        return "HIGH"
    if operation == "DELETE":
        return "CRITICAL"
    if operation == "CREATE":
        return "HIGH"
    if operation in {"ALTER", "DROP", "TRUNCATE"}:
        return "CRITICAL"
    return "HIGH"


def _warning(operation: str, estimated_rows: int | None, may_affect_entire_table: bool = False) -> str:
    if operation == "UPDATE":
        if may_affect_entire_table:
            return f"This query has no WHERE clause and may modify every row in the target table, approximately {_format_estimate(estimated_rows)} rows."
        return f"This query may modify approximately {_format_estimate(estimated_rows)} existing rows."
    if operation == "DELETE":
        if may_affect_entire_table:
            return f"CRITICAL WARNING: This query has no WHERE clause and may delete all records in the target table, approximately {_format_estimate(estimated_rows)} rows."
        return f"CRITICAL WARNING: This query may permanently delete approximately {_format_estimate(estimated_rows)} rows."
    if operation == "INSERT":
        return f"This query may add approximately {_format_estimate(estimated_rows)} new row(s)."
    if operation == "CREATE":
        return "This query creates database structure and requires explicit confirmation."
    if operation in {"ALTER", "DROP", "TRUNCATE"}:
        return f"CRITICAL WARNING: {operation} changes or removes database structure and requires explicit confirmation."
    return "Read-only operation."


def _impact(operation: str, estimated_rows: int | None) -> str:
    if operation in READ_ONLY_OPERATIONS:
        return f"Approximately {_format_estimate(estimated_rows)} rows may be returned."
    if operation == "UPDATE":
        return f"Approximately {_format_estimate(estimated_rows)} rows may be changed."
    if operation == "DELETE":
        return f"Approximately {_format_estimate(estimated_rows)} rows may be deleted."
    if operation == "INSERT":
        return f"Approximately {_format_estimate(estimated_rows)} rows may be inserted."
    if operation in DDL_OPERATIONS:
        return "Database structure may be changed."
    return "Impact could not be determined safely."


def _modified_columns(sql_query: str, operation: str) -> list[str]:
    if operation != "UPDATE":
        return []
    match = re.search(r"\bSET\b(.*?)(?=\bWHERE\b|$)", sql_query, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    assignments = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=", match.group(1))
    return sorted(set(assignments))


def _format_estimate(value: int | None) -> str:
    return str(value) if value is not None else "an unknown number of"
