import re
from typing import Any

import sqlparse

from database import get_database_dialect
from schema_analyzer import get_query_entities


READ_ONLY_OPERATIONS = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
DML_OPERATIONS = {"INSERT", "UPDATE", "DELETE"}
DDL_OPERATIONS = {"CREATE", "ALTER", "DROP", "TRUNCATE"}
SUPPORTED_OPERATIONS = READ_ONLY_OPERATIONS | DML_OPERATIONS | DDL_OPERATIONS


def validate_sql_query(sql_query: str, database: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    if not sql_query or not sql_query.strip():
        return {"valid": False, "errors": ["SQL query is empty."], "warnings": warnings}

    parsed = sqlparse.parse(sql_query)
    if not parsed:
        return {"valid": False, "errors": ["SQL query could not be parsed."], "warnings": warnings}

    if len(parsed) > 1:
        errors.append("Only one SQL statement is allowed at a time.")

    operation = get_operation(sql_query)
    if operation not in SUPPORTED_OPERATIONS:
        errors.append(f"{operation} statements are not supported.")

    if re.search(r"(--|/\*|\*/|;\s*\w+)", sql_query):
        warnings.append("Query contains comment markers or extra statement-like text. Review carefully.")

    if operation in {"UPDATE", "DELETE"} and not re.search(r"\bWHERE\b", sql_query, flags=re.IGNORECASE):
        errors.append(f"{operation} without a WHERE clause is blocked to prevent accidental bulk changes.")

    if operation in DDL_OPERATIONS:
        warnings.append(f"{operation} changes database structure and requires explicit confirmation before execution.")

    entities = get_query_entities(sql_query, database)
    if operation in READ_ONLY_OPERATIONS | DML_OPERATIONS and operation not in {"SHOW"} and not entities["tables"]:
        errors.append("No known database table was found in the SQL query.")
    errors.extend(_unknown_column_errors(sql_query, operation, database))
    errors.extend(_ddl_schema_errors(sql_query, operation, database))
    errors.extend(_dialect_errors(sql_query))

    warnings.extend(_optimization_warnings(sql_query, operation))

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "operation": operation,
        "tables": entities["tables"],
        "columns": entities["columns"],
    }


def get_operation(sql_query: str) -> str:
    parsed = sqlparse.parse(sql_query or "")
    if parsed:
        operation = parsed[0].get_type()
        if operation != "UNKNOWN":
            return operation
    match = re.match(r"\s*(\w+)", sql_query or "")
    operation = match.group(1).upper() if match else "UNKNOWN"
    return "DESCRIBE" if operation == "DESC" else operation


def get_optimization_suggestions(sql_query: str) -> dict[str, Any]:
    operation = get_operation(sql_query)
    suggestions = _optimization_warnings(sql_query, operation)
    return {
        "optimized_query": sql_query.strip(),
        "suggestions": suggestions or ["No obvious optimization suggestions found."],
    }


def _optimization_warnings(sql_query: str, operation: str) -> list[str]:
    suggestions: list[str] = []
    if operation == "SELECT" and re.search(r"\bSELECT\s+\*", sql_query, flags=re.IGNORECASE):
        suggestions.append("Avoid SELECT * when only specific columns are needed.")
    if re.search(r"\bWHERE\b", sql_query, flags=re.IGNORECASE):
        suggestions.append("Consider indexing columns used in WHERE filters if this query runs often.")
    if re.search(r"\bJOIN\b", sql_query, flags=re.IGNORECASE):
        suggestions.append("Make sure joined columns are indexed for better JOIN performance.")
    if re.search(r"\bORDER\s+BY\b", sql_query, flags=re.IGNORECASE):
        suggestions.append("Consider indexing columns used in ORDER BY for large result sets.")
    if re.search(r"\(\s*SELECT\b", sql_query, flags=re.IGNORECASE):
        suggestions.append("Review subqueries; a JOIN or common table expression may be clearer or faster.")
    return suggestions


def _unknown_column_errors(sql_query: str, operation: str, database: str | None = None) -> list[str]:
    if operation not in READ_ONLY_OPERATIONS | DML_OPERATIONS:
        return []

    from database import get_schema

    schema = get_schema(database)
    known_tables = {table.lower() for table in schema}
    table_columns = {
        table.lower(): {column["column_name"].lower() for column in columns}
        for table, columns in schema.items()
    }
    known_columns = {column for columns in table_columns.values() for column in columns}
    aliases = _table_aliases(sql_query)
    output_aliases = _output_aliases(sql_query)
    errors = []

    referenced_tables = _referenced_table_names(sql_query)
    for table_name in referenced_tables:
        table_lower = table_name.lower()
        if table_lower not in known_tables:
            errors.append(f"Table '{table_name}' was not found in the database schema.")

    for qualifier, column in _qualified_identifiers(sql_query):
        qualifier_lower = qualifier.lower()
        column_lower = column.lower()
        table_name = aliases.get(qualifier_lower, qualifier_lower)
        if table_name not in table_columns:
            errors.append(f"Table or alias '{qualifier}' was not found in the database schema.")
            continue
        if column_lower not in table_columns[table_name]:
            errors.append(f"Column '{column}' was not found on table '{table_name}'.")

    identifiers = _candidate_identifiers(sql_query)
    alias_names = {alias for alias, target in aliases.items() if target in known_tables}
    alias_targets = {target for target in aliases.values() if target in known_tables}
    for identifier in identifiers:
        name = identifier.lower()
        if name in {table.lower() for table in referenced_tables}:
            continue
        if name in known_tables or name in known_columns or name in alias_names or name in alias_targets or name in output_aliases or name in SQL_WORDS:
            continue
        if name.isdigit():
            continue
        errors.append(f"Column or identifier '{identifier}' was not found in the database schema.")

    return sorted(set(errors))


def _ddl_schema_errors(sql_query: str, operation: str, database: str | None = None) -> list[str]:
    if operation not in DDL_OPERATIONS:
        return []

    from database import get_schema

    schema = get_schema(database)
    known_tables = {table.lower() for table in schema}
    if re.search(r"\b(?:CREATE|DROP)\s+INDEX\b", sql_query, flags=re.IGNORECASE):
        on_match = re.search(r"\bON\s+[`]?([A-Za-z_][A-Za-z0-9_]*)[`]?", sql_query, flags=re.IGNORECASE)
        if on_match and on_match.group(1).lower() not in known_tables:
            return [f"Table '{on_match.group(1)}' was not found in the selected database."]
        return []
    table_match = re.search(
        r"\b(?:CREATE|ALTER|DROP|TRUNCATE)\s+(?:TABLE\s+|INDEX\s+)?(?:IF\s+(?:NOT\s+)?EXISTS\s+)?[`]?([A-Za-z_][A-Za-z0-9_]*)[`]?",
        sql_query,
        flags=re.IGNORECASE,
    )
    if not table_match:
        return []

    object_name = table_match.group(1)
    object_lower = object_name.lower()
    if operation == "CREATE" and re.search(r"\bCREATE\s+TABLE\b", sql_query, flags=re.IGNORECASE) and object_lower in known_tables:
        return [f"Table '{object_name}' already exists in the selected database."]
    if operation in {"ALTER", "DROP", "TRUNCATE"} and object_lower not in known_tables:
        return [f"Table '{object_name}' was not found in the selected database."]
    return []


def _table_aliases(sql_query: str) -> set[str]:
    aliases = {}
    pattern = r"\b(?:FROM|JOIN|UPDATE|INTO|DESCRIBE|DESC|EXPLAIN)\s+[`]?(\w+)[`]?(?:\s+(?:AS\s+)?(\w+))?"
    for table, alias in re.findall(pattern, sql_query, flags=re.IGNORECASE):
        if alias and alias.upper() not in SQL_WORDS:
            aliases[alias.lower()] = table.lower()
        aliases[table.lower()] = table.lower()
    return aliases


def _referenced_table_names(sql_query: str) -> set[str]:
    pattern = r"\b(?:FROM|JOIN|UPDATE|INTO|DESCRIBE|DESC|EXPLAIN)\s+[`]?(\w+)[`]?"
    return {table for table in re.findall(pattern, sql_query, flags=re.IGNORECASE)}


def _candidate_identifiers(sql_query: str) -> set[str]:
    cleaned = re.sub(r"'[^']*'|\"[^\"]*\"", " ", sql_query)
    cleaned = re.sub(r"`(\w+)`", r"\1", cleaned)
    cleaned = re.sub(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(", " ", cleaned)
    cleaned = re.sub(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)", r" \1", cleaned)
    candidates = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", cleaned))
    return {
        candidate
        for candidate in candidates
        if candidate.upper() not in SQL_WORDS
    }


def _output_aliases(sql_query: str) -> set[str]:
    aliases = {
        alias.lower()
        for alias in re.findall(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\b", sql_query, flags=re.IGNORECASE)
    }
    select_match = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql_query, flags=re.IGNORECASE | re.DOTALL)
    if not select_match:
        return aliases
    for expression in select_match.group(1).split(","):
        parts = expression.strip().split()
        if len(parts) > 1 and parts[-1].upper() not in SQL_WORDS:
            aliases.add(parts[-1].lower())
    return aliases


def _qualified_identifiers(sql_query: str) -> set[tuple[str, str]]:
    cleaned = re.sub(r"'[^']*'|\"[^\"]*\"", " ", sql_query)
    cleaned = re.sub(r"`(\w+)`", r"\1", cleaned)
    return set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b", cleaned))


def _dialect_errors(sql_query: str) -> list[str]:
    dialect = get_database_dialect()
    if dialect == "PostgreSQL" and re.search(r"\b(TIMESTAMPDIFF|CURDATE|IFNULL)\s*\(", sql_query, flags=re.IGNORECASE):
        return ["Query uses MySQL-specific functions while the configured dialect is PostgreSQL."]
    if dialect == "MySQL" and re.search(r"\b(DATE_PART|NOW\s*\(\)\s*::|ILIKE)\b", sql_query, flags=re.IGNORECASE):
        return ["Query uses PostgreSQL-specific syntax while the configured dialect is MySQL."]
    return []


SQL_WORDS = {
    "ADD", "ALL", "AND", "AS", "ASC", "AVG", "BETWEEN", "BY", "CASE", "COUNT",
    "CREATE", "DELETE", "DESC", "DESCRIBE", "DISTINCT", "DROP", "ELSE", "END", "EXISTS", "EXPLAIN", "FROM", "GROUP",
    "HAVING", "IN", "INNER", "INSERT", "INTO", "IS", "JOIN", "LEFT", "LIKE",
    "LIMIT", "MAX", "MIN", "NOT", "NULL", "ON", "OR", "ORDER", "OUTER",
    "RIGHT", "SELECT", "SET", "SHOW", "SUM", "TABLE", "TABLES", "THEN", "TRUNCATE", "UPDATE", "VALUES", "WHEN",
    "WHERE", "YEAR", "MONTH", "DAY", "TIMESTAMPDIFF", "CURDATE", "CURRENT_DATE",
    "CURRENT_TIMESTAMP", "NOW", "DATE", "DATEDIFF", "DATE_ADD", "DATE_SUB",
    "COALESCE", "IFNULL", "NULLIF", "ROUND", "LOWER", "UPPER", "CONCAT",
    "SUBSTRING", "CAST", "EXTRACT", "DATE_PART", "TRUE", "FALSE",
}
