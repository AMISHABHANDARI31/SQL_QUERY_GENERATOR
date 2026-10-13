import re
from typing import Any

import sqlparse


CLAUSE_PATTERNS = [
    ("SELECT", r"\bSELECT\b(.*?)(?=\bFROM\b|$)"),
    ("FROM", r"\bFROM\b(.*?)(?=\bWHERE\b|\bJOIN\b|\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|$)"),
    ("JOIN", r"\b(?:INNER\s+|LEFT\s+|RIGHT\s+|FULL\s+|CROSS\s+)?JOIN\b(.*?)(?=\bWHERE\b|\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|\bJOIN\b|$)"),
    ("WHERE", r"\bWHERE\b(.*?)(?=\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|$)"),
    ("GROUP BY", r"\bGROUP\s+BY\b(.*?)(?=\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|$)"),
    ("HAVING", r"\bHAVING\b(.*?)(?=\bORDER\s+BY\b|\bLIMIT\b|$)"),
    ("ORDER BY", r"\bORDER\s+BY\b(.*?)(?=\bLIMIT\b|$)"),
    ("LIMIT", r"\bLIMIT\b(.*?)(?=$)"),
    ("INSERT", r"\bINSERT\s+INTO\b(.*?)(?=\bVALUES\b|\bSELECT\b|$)"),
    ("UPDATE", r"\bUPDATE\b(.*?)(?=\bSET\b|$)"),
    ("DELETE", r"\bDELETE\b(.*?)(?=\bFROM\b|$)"),
]


def explain_sql_query(sql_query: str) -> dict[str, Any]:
    operation = get_operation(sql_query)
    clauses = []

    for clause, pattern in CLAUSE_PATTERNS:
        matches = re.findall(pattern, sql_query, flags=re.IGNORECASE | re.DOTALL)
        for match in matches:
            text = _single_line(match)
            if text:
                clauses.append({"clause": clause, "text": text, "explanation": _explain_clause(clause, text)})

    extra_notes = []
    if re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", sql_query, flags=re.IGNORECASE):
        extra_notes.append("Uses aggregate functions to calculate summary values.")
    if re.search(r"\(\s*SELECT\b", sql_query, flags=re.IGNORECASE):
        extra_notes.append("Uses a subquery, meaning one query is nested inside another.")
    if re.search(r"\bAS\b", sql_query, flags=re.IGNORECASE):
        extra_notes.append("Uses aliases to give tables or output columns shorter or clearer names.")

    return {
        "summary": _summary(operation),
        "operation": operation,
        "clauses": clauses,
        "notes": extra_notes,
    }


def get_operation(sql_query: str) -> str:
    parsed = sqlparse.parse(sql_query or "")
    if not parsed:
        return "UNKNOWN"
    operation = parsed[0].get_type()
    return operation if operation != "UNKNOWN" else _fallback_operation(sql_query)


def _fallback_operation(sql_query: str) -> str:
    match = re.match(r"\s*(\w+)", sql_query or "")
    return match.group(1).upper() if match else "UNKNOWN"


def _summary(operation: str) -> str:
    summaries = {
        "SELECT": "This query reads data from the database.",
        "INSERT": "This query adds new data to the database.",
        "UPDATE": "This query modifies existing data in the database.",
        "DELETE": "This query removes existing data from the database.",
    }
    return summaries.get(operation, "This query performs a database operation.")


def _explain_clause(clause: str, text: str) -> str:
    explanations = {
        "SELECT": "Chooses the columns or calculated values to return.",
        "FROM": "Chooses the main table used by the query.",
        "JOIN": "Combines rows from another table using a relationship condition.",
        "WHERE": "Filters rows so only matching records are used.",
        "GROUP BY": "Groups matching rows so aggregate calculations can be made.",
        "HAVING": "Filters grouped results after aggregation.",
        "ORDER BY": "Sorts the result rows.",
        "LIMIT": "Restricts how many rows are returned.",
        "INSERT": "Identifies the table and columns where new data will be inserted.",
        "UPDATE": "Identifies the table whose existing rows will be changed.",
        "DELETE": "Identifies rows that may be removed.",
    }
    return explanations.get(clause, "Part of the SQL query.")


def _single_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
