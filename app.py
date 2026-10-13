from fastapi import FastAPI
from pydantic import BaseModel

from impact_analyzer import analyze_query_impact
from query_generator import (
    check_ollama_health,
    execute_selected_sql,
    generate_general_sql_options,
    generate_sql_options,
)
from query_history import get_history, get_history_item, init_history_db
from schema_analyzer import get_database_schema, get_query_entities
from schema_service import SchemaService
from sql_explainer import explain_sql_query
from sql_validator import get_optimization_suggestions, validate_sql_query

app = FastAPI()
init_history_db()
schema_service = SchemaService()


@app.on_event("startup")
async def load_schema_cache():
    try:
        schema_service.refresh()
    except Exception:
        pass


class QueryRequest(BaseModel):
    query: str
    database: str | None = None
    mode: str = "general"


class ApiSqlGenerateRequest(BaseModel):
    question: str
    database: str | None = None
    mode: str = "general"


class DatabaseRequest(BaseModel):
    database: str | None = None


class ExecuteRequest(BaseModel):
    query: str
    natural_language_prompt: str | None = None
    confirmed: bool = False
    database: str | None = None


@app.post("/generate_sql")
async def generate_sql(request: QueryRequest):
    if request.mode == "database_aware":
        generated = generate_sql_options(request.query, database=request.database)
    else:
        generated = generate_general_sql_options(request.query)
    if "error" in generated:
        return generated

    queries = generated.get("queries", [])
    if not queries:
        return {"error": "Failed to generate SQL query."}

    return {
        "mode": generated.get("mode", request.mode),
        "executable": generated.get("executable", request.mode == "database_aware"),
        "execution_warning": generated.get("execution_warning"),
        "sql_query": queries[0]["sql"],
        "queries": queries,
    }


@app.post("/api/sql/generate")
async def api_generate_sql(request: ApiSqlGenerateRequest):
    if request.mode == "database_aware":
        generated = generate_sql_options(request.question, database=request.database)
    else:
        generated = generate_general_sql_options(request.question)
    if "error" in generated:
        return {
            "success": False,
            "question": request.question,
            "mode": request.mode,
            "error": generated["error"],
            "validation": {"valid": False, "errors": [generated["error"]]},
        }

    queries = generated.get("queries", [])
    if not queries:
        return {
            "success": False,
            "question": request.question,
            "mode": request.mode,
            "error": "Failed to generate SQL query.",
            "validation": {"valid": False, "errors": ["Failed to generate SQL query."]},
        }

    selected = queries[0]
    entities = selected.get("entities", {})
    return {
        "success": True,
        "question": request.question,
        "mode": generated.get("mode", request.mode),
        "executable": generated.get("executable", request.mode == "database_aware"),
        "execution_warning": generated.get("execution_warning"),
        "database": request.database,
        "sql": selected["sql"],
        "assumptions": selected.get("assumptions", []),
        "proposed_schema": selected.get("proposed_schema", []),
        "tables": entities.get("tables", []),
        "columns": [
            f'{column["table"]}.{column["column"]}'
            for column in entities.get("columns", [])
        ],
        "explanation": selected.get("explanation", {}).get("summary"),
        "validation": selected.get("validation"),
        "impact": selected.get("impact"),
        "risk": selected.get("risk"),
        "optimization": selected.get("optimization"),
        "queries": queries,
    }


@app.post("/execute_sql")
async def execute_sql(request: ExecuteRequest):
    return execute_selected_sql(
        request.query,
        natural_language_prompt=request.natural_language_prompt,
        confirmed=request.confirmed,
        database=request.database,
    )


@app.post("/validate_sql")
async def validate_sql(request: QueryRequest):
    return validate_sql_query(request.query, request.database)


@app.post("/analyze_query")
async def analyze_query(request: QueryRequest):
    return analyze_query_impact(request.query, request.database)


@app.post("/analyze_sql")
async def analyze_sql(request: QueryRequest):
    return analyze_query_impact(request.query, request.database)


@app.post("/explain_sql")
async def explain_sql(request: QueryRequest):
    return explain_sql_query(request.query)


@app.post("/optimize_sql")
async def optimize_sql(request: QueryRequest):
    return get_optimization_suggestions(request.query)


@app.post("/query_entities")
async def query_entities(request: QueryRequest):
    return get_query_entities(request.query, request.database)


@app.get("/databases")
async def databases():
    try:
        return {"databases": schema_service.list_databases()}
    except Exception:
        return {"error": "Unable to list accessible MySQL databases. Please check the database configuration."}


@app.get("/databases/{database}/tables")
async def database_tables(database: str):
    try:
        return {"database": database, "tables": schema_service.list_tables(database)}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception:
        return {"error": "Unable to retrieve tables for the selected database."}


@app.get("/databases/{database}/schema")
async def database_schema(database: str):
    try:
        return schema_service.get_schema(database)
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception:
        return {"error": "Unable to retrieve schema for the selected database."}


@app.get("/databases/{database}/test")
async def database_test(database: str):
    try:
        if not schema_service.validate_database(database):
            return {"ok": False, "error": f"Database '{database}' is not accessible with the configured MySQL credentials."}
        return schema_service.test_connection(database)
    except Exception:
        return {"ok": False, "error": "Unable to connect to the selected database."}


@app.get("/schema")
async def schema():
    return get_database_schema()


@app.get("/api/schema")
async def api_schema(database: str | None = None):
    return schema_service.get_schema(database)


@app.post("/api/schema/refresh")
async def api_schema_refresh(request: DatabaseRequest | None = None):
    try:
        database = request.database if request else None
        return {"success": True, "schema": schema_service.refresh(database)}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception:
        return {
            "success": False,
            "error": "Unable to connect to the database. Please check the database configuration.",
        }


@app.get("/health")
async def health():
    ollama = check_ollama_health()
    try:
        schema_error = schema_service.get_schema().get("cache_error")
        database = {"ok": schema_error is None, "error": schema_error}
    except Exception:
        database = {
            "ok": False,
            "error": "Unable to connect to the database. Please check the database configuration.",
        }
    return {
        "application": "ok",
        "ai_provider": "local",
        "ollama": ollama,
        "database": database,
    }


@app.get("/history")
async def history(limit: int = 50):
    return {"history": get_history(limit)}


@app.get("/query_history")
async def query_history(limit: int = 50):
    return {"history": get_history(limit)}


@app.get("/history/{history_id}")
async def history_item(history_id: int):
    item = get_history_item(history_id)
    if item is None:
        return {"error": "History item not found."}
    return item


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
