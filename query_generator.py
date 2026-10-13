import os
import traceback
import re
import json
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from impact_analyzer import analyze_query_impact
from ollama_service import OllamaService
from query_history import save_history, timer
from database import get_database_dialect, get_engine, get_schema, refresh_schema_cache, set_active_database, validate_database_name
from schema_analyzer import find_schema_matches, get_query_entities, schema_as_prompt_text
from sql_explainer import explain_sql_query
from sql_validator import get_operation, get_optimization_suggestions, validate_sql_query

load_dotenv()

AI_PROVIDER = os.getenv("AI_PROVIDER", "local")
MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "100"))
QUERY_TIMEOUT = int(os.getenv("QUERY_TIMEOUT", "30"))
OLLAMA_SERVICE = OllamaService()
SQL_REQUEST_WORDS = {
    "all", "and", "any", "average", "by", "count", "delete", "display", "each",
    "find", "for", "from", "get", "greater", "group", "having", "insert",
    "less", "like", "limit", "list", "max", "min", "more", "order", "records",
    "remove", "rows", "select", "show", "sort", "sum", "table", "tables",
    "than", "the", "their", "to", "top", "total", "update", "where", "whose",
    "with",
}


def check_ollama_health():
    """Verify Ollama is reachable and the configured model is installed."""
    return OLLAMA_SERVICE.health()

def clean_sql_output(response_text):
    """removes markdown formatting and extra whitespace from the SQL query"""
    clean_query = response_text.strip()
    clean_query = re.sub(r"^\s*```(?:sql)?\s*", "", clean_query, flags=re.IGNORECASE)
    clean_query = re.sub(r"\s*```\s*$", "", clean_query)
    sql_match = re.search(
        r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|SHOW|DESCRIBE|DESC|EXPLAIN)\b.*?;",
        clean_query,
        re.DOTALL | re.IGNORECASE,
    )

    sql_query = sql_match.group(0) if sql_match else clean_query.strip()
    if re.match(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|SHOW|DESCRIBE|DESC|EXPLAIN)\b", sql_query, flags=re.IGNORECASE) and not sql_query.rstrip().endswith(";"):
        sql_query = sql_query.rstrip() + ";"
    return sql_query


def detect_request_intent(nl_query):
    """Detect broad operation and query concepts from natural language."""
    text_lower = nl_query.lower()
    operation_patterns = [
        ("DELETE", r"\b(delete|remove)\b"),
        ("DROP", r"\b(drop|remove table|delete table)\b"),
        ("TRUNCATE", r"\b(truncate|empty table)\b"),
        ("ALTER", r"\b(alter|add column|drop column|modify column|rename column)\b"),
        ("CREATE", r"\b(create table|create index|make table|new table)\b"),
        ("UPDATE", r"\b(update|change|modify|set|increase|decrease)\b"),
        ("INSERT", r"\b(insert|add|create|new record)\b"),
        ("SELECT", r"\b(show|list|get|find|display|count|average|sum|min|max|top|highest|lowest)\b"),
    ]
    operation = next(
        (name for name, pattern in operation_patterns if re.search(pattern, text_lower)),
        "SELECT",
    )
    concept_patterns = {
        "filter": r"\b(where|whose|with|having|older than|greater than|less than|equal|between|after|before)\b",
        "sort": r"\b(order|sort|highest|lowest|top|latest|oldest)\b",
        "group": r"\b(group by|per|each|by category|by department|by status)\b",
        "aggregate": r"\b(count|average|avg|sum|total|min|max|minimum|maximum)\b",
        "join": r"\b(join|with their|along with|related|for each)\b",
        "limit": r"\b(top|first|last|limit)\b",
        "date_filtering": r"\b(date|day|month|year|today|yesterday|after|before|between)\b",
        "comparison": r"\b(greater than|less than|more than|under|over|equal|at least|at most)\b",
        "calculation": r"\b(calculate|percentage|ratio|difference|increase|decrease)\b",
    }
    concepts = [
        concept for concept, pattern in concept_patterns.items()
        if re.search(pattern, text_lower)
    ]
    return {"operation": operation, "concepts": concepts}


def schema_supports_request(nl_query, database: str | None = None):
    """Return a clear unsupported message when the schema lacks required concepts."""
    text_lower = nl_query.lower()
    schema = get_schema(database)
    all_columns = [
        column["column_name"].lower()
        for columns in schema.values()
        for column in columns
    ]
    all_tables = [table.lower() for table in schema]
    intent = detect_request_intent(nl_query)
    if intent["operation"] in {"CREATE", "ALTER", "DROP", "TRUNCATE"}:
        return {"ok": True}

    age_requested = re.search(r"\b(age|aged|older than|younger than)\b", text_lower)
    has_age_attribute = any(
        re.search(r"(^|_)(age|dob|birth|birthdate|date_of_birth|birth_date)($|_)", column)
        or column in {"dateofbirth", "birthdate"}
        for column in all_columns
    )
    if age_requested and not has_age_attribute:
        return {
            "ok": False,
            "error": "The current database schema does not contain an age or date-of-birth attribute, so this request cannot be answered reliably using the available data.",
        }

    matches = find_schema_matches(nl_query, database)
    operation_is_global = re.search(r"\b(all|records|rows|tables|database|schema)\b", text_lower)
    aggregate_is_global = re.search(r"\b(count|total|how many)\b", text_lower)
    if not matches["tables"] and not matches["columns"] and not operation_is_global and not aggregate_is_global:
        return {
            "ok": False,
            "error": "I could not find tables or columns relevant to your request. Please try a more specific question.",
        }

    missing_table_words = [
        word for word in re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", text_lower)
        if word.endswith("s") and word not in all_tables and word not in SQL_REQUEST_WORDS
    ]
    if missing_table_words and not matches["tables"] and not matches["columns"]:
        return {
            "ok": False,
            "error": f"The current database schema does not contain a matching table or column for: {', '.join(sorted(set(missing_table_words)))}.",
        }

    return {"ok": True}


def generate_sql_query(nl_query, database: str | None = None, user_id: int | None = None):
    """convert natural language to an optimised SQL query."""
    generated = generate_sql_options(nl_query, database=database, user_id=user_id)
    if "error" in generated:
        return generated
    queries = generated.get("queries", [])
    return queries[0]["sql"] if queries else {"error": "No SQL query was generated."}


def generate_sql_options(nl_query, database: str | None = None, user_id: int | None = None):
    """Generate SQL with local Ollama and attach local analysis."""
    if AI_PROVIDER.lower() != "local":
        return {"error": "This project is configured to generate SQL with the local Ollama provider. Set AI_PROVIDER=local."}

    if database:
        if not validate_database_name(database):
            return {"error": f"Database '{database}' is not accessible with the configured MySQL credentials."}
        set_active_database(database)

    health = check_ollama_health()
    if not health["ok"]:
        return {"error": health["error"]}

    schema_support = schema_supports_request(nl_query, database)
    if not schema_support["ok"]:
        return {"error": schema_support["error"]}

    intent = detect_request_intent(nl_query)
    try:
        terms = re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", nl_query)
        schema_text = schema_as_prompt_text(terms, database=database)
    except SQLAlchemyError as e:
        return {"error": f"Database schema lookup failed: {str(e)}"}

    prompt = f"""
You are an expert SQL query generator.

Your task is to convert the user's natural-language question into valid {get_database_dialect()} SQL.

DATABASE DIALECT:
{get_database_dialect()}

DETECTED INTENT:
Operation: {intent["operation"]}
Concepts: {", ".join(intent["concepts"]) if intent["concepts"] else "none"}

DATABASE SCHEMA:
{schema_text}

USER QUESTION:
{nl_query}

Rules:
1. Return valid JSON only.
2. JSON shape: {{"answerable": true, "reason": "", "queries": [{{"sql": "SELECT ...;", "description": "short reason this option is useful"}}]}}
3. If the schema cannot answer the request, return: {{"answerable": false, "reason": "clear reason based on missing schema attributes", "queries": []}}
4. Generate one high-quality query for unambiguous requests.
5. Generate two or three alternatives only when there are meaningfully different interpretations or approaches.
6. Do not use markdown code fences.
7. Use only tables and columns that exist in the provided schema.
8. Generate syntactically valid {get_database_dialect()} SQL.
9. Do not invent columns, tables, relationships, or data types.
10. Do not infer age from unrelated dates unless the schema has an explicit age or birth-date column.
11. Generate CREATE, ALTER, DROP, TRUNCATE, or index DDL only when the user explicitly asks for schema changes.
12. Prefer read-only SQL unless the user's request clearly asks to modify data or schema.
"""
    try:
        raw_content = OLLAMA_SERVICE.generate(prompt)
        parsed = parse_ollama_generation(raw_content)
        if not parsed.get("answerable", True):
            return {"error": parsed.get("reason") or "The current database schema cannot answer this request reliably."}

        generated_queries = parsed.get("queries", [])
        if not generated_queries:
            return {"error": "SQL generation failed. Ollama did not return a usable SQL query."}

        queries = []
        seen_sql = set()
        for index, query in enumerate(generated_queries[:3], start=1):
            sql_query = clean_sql_output(query.get("sql", ""))
            if not sql_query or sql_query.lower() in seen_sql:
                continue
            validation = validate_sql_query(sql_query, database)
            if not validation.get("valid"):
                corrected_sql = correct_sql_with_ollama(sql_query, validation, nl_query, schema_text, database)
                if corrected_sql:
                    sql_query = corrected_sql
            seen_sql.add(sql_query.lower())
            queries.append(
                _build_query_option(
                    sql_query,
                    len(queries) + 1,
                    query.get("description") or "Generated by local Ollama model.",
                    database,
                )
            )
        if not queries:
            return {"error": "SQL generation failed. Ollama did not return a usable SQL query."}
        for query in queries:
            save_history(
                natural_language_prompt=nl_query,
                sql_query=query["sql"],
                query_type=query["validation"].get("operation", get_operation(query["sql"])),
                status="Generated",
                user_id=user_id,
                generation_mode="database_aware",
                database=database,
                tables=query["entities"].get("tables", []),
                explanation=query["explanation"].get("summary"),
                validation_result=query["validation"],
                result_summary="SQL generated by local Ollama.",
            )
        return {"queries": queries}
    except RuntimeError as e:
       return {"error": str(e)}
    except Exception as e:
       print("=" * 60)
       traceback.print_exc()
       print("=" * 60)
       return {"error": f"Unexpected SQL generation error: {str(e)}"}


def generate_general_sql_options(nl_query, user_id: int | None = None):
    """Generate schema-independent SQL with explicit assumptions and no execution claim."""
    if AI_PROVIDER.lower() != "local":
        return {"error": "This project is configured to generate SQL with the local Ollama provider. Set AI_PROVIDER=local."}

    health = check_ollama_health()
    if not health["ok"]:
        return {"error": health["error"]}

    intent = detect_request_intent(nl_query)
    prompt = f"""
You are an expert MySQL SQL generator.

Generate SQL for a natural-language request without relying on an existing database schema.

USER REQUEST:
{nl_query}

DETECTED INTENT:
Operation: {intent["operation"]}
Concepts: {", ".join(intent["concepts"]) if intent["concepts"] else "none"}

Rules:
1. Return valid JSON only.
2. JSON shape:
{{
  "queries": [
    {{
      "sql": "SELECT ...;",
      "description": "short description",
      "assumptions": ["Assume a table named ...", "Assume a column named ..."],
      "proposed_schema": [
        {{"table": "table_name", "columns": [{{"name": "column_name", "type": "MYSQL_TYPE", "reason": "why needed"}}]}}
      ],
      "warnings": ["This SQL is schema-independent and may not execute until the schema exists."]
    }}
  ]
}}
3. Infer reasonable table and column names from the user request.
4. Clearly list every inferred table and column as an assumption.
5. If a SELECT needs missing structure, propose schema but do not generate CREATE TABLE unless the user explicitly asks to create or alter schema.
6. Generate CREATE, ALTER, DROP, or TRUNCATE only when explicitly requested.
7. Do not claim that generated SQL is executable against a real database.
8. Do not include markdown fences or explanations outside JSON.
9. Generate one query for clear requests and multiple alternatives only for meaningful ambiguity.
"""
    try:
        raw_content = OLLAMA_SERVICE.generate(prompt)
        parsed = parse_ollama_generation(raw_content)
        generated_queries = parsed.get("queries", [])
        if not generated_queries:
            return {"error": "SQL generation failed. Ollama did not return a usable SQL query."}

        queries = []
        seen_sql = set()
        for query in generated_queries[:3]:
            sql_query = clean_sql_output(query.get("sql", ""))
            if not sql_query or sql_query.lower() in seen_sql:
                continue
            seen_sql.add(sql_query.lower())
            assumptions = _normalize_list(query.get("assumptions"))
            warnings = _normalize_list(query.get("warnings"))
            proposed_schema = query.get("proposed_schema") or []
            option = _build_general_query_option(
                sql_query=sql_query,
                option_id=len(queries) + 1,
                description=query.get("description") or "Generated from inferred schema assumptions.",
                assumptions=assumptions,
                proposed_schema=proposed_schema,
                warnings=warnings,
            )
            queries.append(option)

        if not queries:
            return {"error": "SQL generation failed. Ollama did not return a usable SQL query."}

        for query in queries:
            save_history(
                natural_language_prompt=nl_query,
                sql_query=query["sql"],
                query_type=query["validation"].get("operation", get_operation(query["sql"])),
                status="Generated",
                user_id=user_id,
                generation_mode="general",
                assumptions=query.get("assumptions", []),
                explanation=query["explanation"].get("summary"),
                validation_result=query["validation"],
                result_summary="Schema-independent SQL generated from assumptions.",
            )
        return {
            "mode": "general",
            "executable": False,
            "execution_warning": "This SQL was generated without an existing database schema. It is not executable until the required schema and data exist.",
            "queries": queries,
        }
    except RuntimeError as e:
        return {"error": str(e)}
    except Exception as e:
        print("=" * 60)
        traceback.print_exc()
        print("=" * 60)
        return {"error": f"Unexpected general SQL generation error: {str(e)}"}


def parse_ollama_generation(raw_content):
    cleaned = raw_content.strip()
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    json_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return {
        "answerable": True,
        "queries": [
            {
                "sql": clean_sql_output(cleaned),
                "description": "Generated by local Ollama model.",
            }
        ],
    }


def correct_sql_with_ollama(sql_query, validation, nl_query, schema_text, database: str | None = None):
    prompt = f"""
You are correcting SQL for a {get_database_dialect()} database.

User request:
{nl_query}

Relevant database schema:
{schema_text}

Invalid SQL:
{sql_query}

Validation errors:
{"; ".join(validation.get("errors", []))}

Return ONLY corrected SQL. Do not return markdown or explanations. Use only the provided schema.
"""
    try:
        corrected = clean_sql_output(OLLAMA_SERVICE.generate(prompt))
    except RuntimeError:
        return None
    corrected_validation = validate_sql_query(corrected, database)
    return corrected if corrected_validation.get("valid") else None


def _build_query_option(sql_query, option_id, description, database: str | None = None):
    impact = analyze_query_impact(sql_query, database)
    return {
        "id": option_id,
        "sql": sql_query,
        "description": description,
        "explanation": explain_sql_query(sql_query),
        "entities": get_query_entities(sql_query, database),
        "validation": validate_sql_query(sql_query, database),
        "impact": impact,
        "risk": {
            "level": impact.get("risk_level"),
            "requires_confirmation": impact.get("requires_confirmation", False),
            "warning": impact.get("warning"),
        },
        "optimization": get_optimization_suggestions(sql_query),
    }


def _build_general_query_option(sql_query, option_id, description, assumptions, proposed_schema, warnings):
    operation = get_operation(sql_query)
    explanation = explain_sql_query(sql_query)
    validation = {
        "valid": True,
        "operation": operation,
        "errors": [],
        "warnings": [
            "Schema-independent SQL: table and column references are inferred assumptions, not validated against a database.",
            *warnings,
        ],
        "executable": False,
    }
    risk_level = "LOW" if operation in {"SELECT", "SHOW", "DESCRIBE", "EXPLAIN"} else "HIGH"
    if operation in {"DROP", "TRUNCATE", "DELETE", "ALTER"}:
        risk_level = "CRITICAL"
    return {
        "id": option_id,
        "sql": sql_query,
        "description": description,
        "assumptions": assumptions,
        "proposed_schema": proposed_schema,
        "execution_available": False,
        "execution_warning": "Execution is unavailable in General SQL Generator mode until a matching database schema and data are provided.",
        "explanation": explanation,
        "entities": {"tables": [], "columns": [], "relationships": []},
        "validation": validation,
        "impact": {
            "operation": operation,
            "tables": [],
            "columns": [],
            "estimated_rows": None,
            "estimate_note": "No database was selected, so row impact cannot be estimated.",
            "risk_level": risk_level,
            "warning": "Review assumptions before using this SQL against a real database.",
            "requires_confirmation": operation not in {"SELECT", "SHOW", "DESCRIBE", "EXPLAIN"},
            "destructive": operation in {"DROP", "TRUNCATE", "DELETE", "ALTER"},
        },
        "risk": {
            "level": risk_level,
            "requires_confirmation": operation not in {"SELECT", "SHOW", "DESCRIBE", "EXPLAIN"},
            "warning": "Schema and data must exist before this SQL can be executed.",
        },
        "optimization": get_optimization_suggestions(sql_query),
    }


def _normalize_list(value):
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []

def suggest_index(sql_query, database: str | None = None):
    """suggest indexes based on the sql query"""
    if get_operation(sql_query) != "SELECT":
        return "No index suggestion needed for this statement type."
    try:
        with get_engine(database).connect() as connection:
            result = connection.execute(text(f"EXPLAIN {sql_query}"))
            execution_plan = result.fetchall()
        print("\n Query Execution Plan:")
        for row in execution_plan:
            print(row)
        return "Consider adding indexes on columns used in WHERE, JOIN, and ORDER BY clauses for better performance."
    except Exception as e:
        return f"Error suggesting indexes: {e}"

def execute_sql_query(sql_query, database: str | None = None, user_id: int | None = None):
    """execute a validated and optimized sql query and return the results"""
    return execute_selected_sql(sql_query, database=database, user_id=user_id)


def execute_selected_sql(sql_query, natural_language_prompt=None, confirmed=False, database: str | None = None, user_id: int | None = None):
    """Execute validated SQL. INSERT/UPDATE/DELETE require explicit confirmation."""
    if database:
        if not validate_database_name(database):
            return {"error": f"Database '{database}' is not accessible with the configured MySQL credentials."}
        set_active_database(database)

    validation = validate_sql_query(sql_query, database)
    operation = validation.get("operation", get_operation(sql_query))
    if not validation["valid"]:
        error_message = "; ".join(validation["errors"])
        save_history(
            natural_language_prompt=natural_language_prompt,
            sql_query=sql_query,
            query_type=operation,
            status="Validation failed",
            error_message=error_message,
            user_id=user_id,
            database=database,
            tables=validation.get("tables", []),
            explanation=explain_sql_query(sql_query).get("summary"),
            validation_result=validation,
            result_summary="SQL was not executed because validation failed.",
        )
        return {"error": f"SQL query validation failed: {error_message}", "validation": validation}

    impact = analyze_query_impact(sql_query, database)
    if impact["requires_confirmation"] and not confirmed:
        return {
            "error": "This query changes data and requires explicit confirmation before execution.",
            "requires_confirmation": True,
            "impact": impact,
            "validation": validation,
        }

    try:
        start_time = timer()
        with get_engine(database).connect() as connection:
            if operation == "SELECT":
                if get_database_dialect() == "MySQL":
                    connection.execute(text("SET SESSION MAX_EXECUTION_TIME=:timeout_ms"), {"timeout_ms": QUERY_TIMEOUT * 1000})
                result = connection.execute(text(sql_query))
                fetched_results = [dict(row._mapping) for row in result.fetchmany(MAX_RESULT_ROWS)]
                rows_affected = len(fetched_results)
            elif operation in {"SHOW", "DESCRIBE", "EXPLAIN"}:
                result = connection.execute(text(sql_query))
                fetched_results = [dict(row._mapping) for row in result.fetchmany(MAX_RESULT_ROWS)]
                rows_affected = len(fetched_results)
            else:
                with connection.begin():
                    result = connection.execute(text(sql_query))
                    fetched_results = []
                    rows_affected = result.rowcount if result.rowcount is not None else 0
                if operation in {"CREATE", "ALTER", "DROP", "TRUNCATE"}:
                    refresh_schema_cache(database)
        execution_time = timer() - start_time
        index_suggestion = suggest_index(sql_query, database)
        history_id = save_history(
            natural_language_prompt=natural_language_prompt,
            sql_query=sql_query,
            query_type=operation,
            status="Success",
            execution_time=execution_time,
            rows_affected=rows_affected,
            user_id=user_id,
            database=database,
            tables=validation.get("tables", []),
            explanation=explain_sql_query(sql_query).get("summary"),
            validation_result=validation,
            result_summary=_execution_summary(operation, rows_affected),
        )
        return {
            "results": fetched_results,
            "optimization_tips": index_suggestion,
            "rows_affected": rows_affected,
            "row_count": rows_affected,
            "result_limit": MAX_RESULT_ROWS,
            "result_limited": operation in {"SELECT", "SHOW", "DESCRIBE", "EXPLAIN"} and rows_affected == MAX_RESULT_ROWS,
            "columns": list(fetched_results[0].keys()) if fetched_results else [],
            "result_summary": _execution_summary(operation, rows_affected),
            "execution_time": execution_time,
            "history_id": history_id,
        }
    except SQLAlchemyError as e:
        save_history(
            natural_language_prompt=natural_language_prompt,
            sql_query=sql_query,
            query_type=operation,
            status="Execution failed",
            error_message=str(e),
            user_id=user_id,
            database=database,
            tables=validation.get("tables", []),
            explanation=explain_sql_query(sql_query).get("summary"),
            validation_result=validation,
            result_summary="Database execution failed.",
        )
        return {"error": f"SQL execution error: {str(e)}"}


def _execution_summary(operation, rows_affected):
    labels = {
        "SELECT": "Rows returned",
        "INSERT": "Rows inserted",
        "UPDATE": "Rows updated",
        "DELETE": "Rows deleted",
        "CREATE": "Schema objects created",
        "ALTER": "Schema objects changed",
        "DROP": "Schema objects dropped",
        "TRUNCATE": "Tables truncated",
        "SHOW": "Rows returned",
        "DESCRIBE": "Rows returned",
        "EXPLAIN": "Rows returned",
    }
    return f"{labels.get(operation, 'Rows affected')}: {rows_affected}"

if __name__ == "__main__":
    # Example usage
    user_query = input("Enter your natural language query: ")
    sql_query = generate_sql_query(user_query)
    if sql_query:
        print("Generated SQL Query:", sql_query)
        execution_results = execute_sql_query(sql_query)
        if execution_results and "results" in execution_results:
            print("\nQuery result")
            for rows in execution_results["results"]:
                print(rows)
            print("\nOptimization Tips:", execution_results["optimization_tips"])
        elif execution_results and "error" in execution_results:
            print(execution_results["error"])
        else:
            print("No results returned from the query execution.")
    else:
        print("Failed to generate SQL query.")
