import re
from typing import Any

from database import get_database_dialect, get_database_name, get_foreign_keys, get_primary_keys, get_schema


def get_database_schema(database: str | None = None) -> dict[str, Any]:
    """Return tables, columns, primary keys, and foreign keys for the current DB."""
    columns = get_schema(database)
    primary_keys = get_primary_keys(database)
    foreign_keys = get_foreign_keys(database)

    return {
        "database": database or get_database_name(),
        "dialect": get_database_dialect(),
        "tables": [
            {
                "name": table_name,
                "columns": [
                    {
                        "name": column["column_name"],
                        "data_type": column["data_type"],
                        "primary_key": column["column_name"] in primary_keys.get(table_name, []),
                    }
                    for column in table_columns
                ],
            }
            for table_name, table_columns in columns.items()
        ],
        "relationships": foreign_keys,
    }


def get_query_entities(sql_query: str, database: str | None = None) -> dict[str, Any]:
    """Identify known schema tables, columns, and relationships used by a query."""
    schema = get_schema(database)
    relationships = get_foreign_keys(database)
    query_lower = sql_query.lower()
    operation_match = re.match(r"\s*(\w+)", sql_query or "")
    operation = operation_match.group(1).upper() if operation_match else ""
    selects_all_columns = bool(re.search(r"\bSELECT\s+\*", sql_query, flags=re.IGNORECASE))

    tables = [
        table for table in schema
        if re.search(rf"\b{re.escape(table.lower())}\b", query_lower)
    ]

    columns: list[dict[str, str]] = []
    if operation in {"CREATE", "DROP", "TRUNCATE"}:
        return {"tables": tables, "columns": columns, "relationships": []}
    if tables or not selects_all_columns:
        for table, table_columns in schema.items():
            if tables and table not in tables:
                continue
            for column in table_columns:
                column_name = column["column_name"]
                if selects_all_columns or re.search(rf"\b{re.escape(column_name.lower())}\b", query_lower):
                    columns.append(
                        {
                            "table": table,
                            "column": column_name,
                            "data_type": column["data_type"],
                        }
                    )

    column_names = {column["column"].lower() for column in columns}
    used_relationships = [
        relationship for relationship in relationships
        if relationship["table"] in tables
        and relationship["referenced_table"] in tables
        and (
            relationship["column"].lower() in column_names
            or relationship["referenced_column"].lower() in column_names
        )
    ]

    return {
        "tables": tables,
        "columns": columns,
        "relationships": used_relationships,
    }


def schema_as_prompt_text(relevant_terms: list[str] | None = None, database: str | None = None) -> str:
    schema = get_schema(database)
    selected_schema = _select_relevant_schema(schema, relevant_terms or [], database)
    relationships = [
        relationship for relationship in get_foreign_keys(database)
        if relationship["table"] in selected_schema
        or relationship["referenced_table"] in selected_schema
    ]

    table_text = "\n".join(
        f"{table}: "
        + ", ".join(
            f'{column["column_name"]} ({column["data_type"]})'
            for column in columns
        )
        for table, columns in selected_schema.items()
    )

    if not relationships:
        return table_text

    relationship_text = "\nRelationships:\n" + "\n".join(
        f'{item["table"]}.{item["column"]} -> {item["referenced_table"]}.{item["referenced_column"]}'
        for item in relationships
    )
    return table_text + relationship_text


def find_schema_matches(text: str, database: str | None = None) -> dict[str, list[str]]:
    schema = get_schema(database)
    text_lower = text.lower()
    tables = [
        table for table in schema
        if _name_matches_text(table, text_lower)
    ]
    columns = [
        f'{table}.{column["column_name"]}'
        for table, table_columns in schema.items()
        for column in table_columns
        if _name_matches_text(column["column_name"], text_lower)
    ]
    return {"tables": tables, "columns": columns}


def _select_relevant_schema(
    schema: dict[str, list[dict[str, str]]],
    terms: list[str],
    database: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    if not terms:
        return schema

    normalized_terms = [term.lower() for term in terms if term.strip()]
    selected = {}
    for table, columns in schema.items():
        table_matches = any(_name_matches_text(table, term) for term in normalized_terms)
        column_matches = [
            column for column in columns
            if any(_name_matches_text(column["column_name"], term) for term in normalized_terms)
        ]
        if table_matches or column_matches:
            selected[table] = columns

    if not selected:
        return schema

    for relationship in get_foreign_keys(database):
        if relationship["table"] in selected and relationship["referenced_table"] in schema:
            selected.setdefault(relationship["referenced_table"], schema[relationship["referenced_table"]])
        if relationship["referenced_table"] in selected and relationship["table"] in schema:
            selected.setdefault(relationship["table"], schema[relationship["table"]])

    return selected


def _name_matches_text(name: str, text: str) -> bool:
    name_lower = name.lower()
    compact = name_lower.replace("_", "")
    words = name_lower.replace("_", " ")
    return (
        re.search(rf"\b{re.escape(name_lower)}\b", text) is not None
        or re.search(rf"\b{re.escape(compact)}\b", text.replace(" ", "")) is not None
        or words in text
    )
