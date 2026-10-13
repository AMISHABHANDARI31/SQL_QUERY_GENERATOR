import os
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DB_DIALECT = os.getenv("DB_DIALECT", os.getenv("DATABASE_DIALECT", "mysql")).lower()
if DB_DIALECT in {"postgres", "postgresql"}:
    DB_DIALECT = "postgresql"
else:
    DB_DIALECT = "mysql"

MY_SQL_HOST = os.getenv("MYSQL_HOST")
MY_SQL_USER = os.getenv("MYSQL_USER")
MY_SQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MY_SQL_DATABASE = os.getenv("MYSQL_DATABASE")
MY_SQL_PORT = os.getenv("MYSQL_PORT", 3306)

POSTGRES_HOST = os.getenv("POSTGRES_HOST", os.getenv("PGHOST"))
POSTGRES_USER = os.getenv("POSTGRES_USER", os.getenv("PGUSER"))
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", os.getenv("PGPASSWORD"))
POSTGRES_DATABASE = os.getenv("POSTGRES_DATABASE", os.getenv("PGDATABASE"))
POSTGRES_PORT = os.getenv("POSTGRES_PORT", os.getenv("PGPORT", 5432))
POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "public")

if DB_DIALECT == "postgresql":
    DB_HOST = POSTGRES_HOST
    DB_USER = POSTGRES_USER
    DB_PASSWORD = POSTGRES_PASSWORD
    DB_NAME = POSTGRES_DATABASE
    DB_PORT = POSTGRES_PORT
else:
    DB_HOST = MY_SQL_HOST
    DB_USER = MY_SQL_USER
    DB_PASSWORD = MY_SQL_PASSWORD
    DB_NAME = MY_SQL_DATABASE
    DB_PORT = MY_SQL_PORT

missing_database_settings = [
    name for name, value in {
        "DB_HOST": DB_HOST,
        "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD,
    }.items()
    if not value
]

if DB_DIALECT == "postgresql" and not DB_NAME:
    missing_database_settings.append("DB_NAME")

if missing_database_settings:
    missing = ", ".join(missing_database_settings)
    raise RuntimeError(f"Missing database environment variable(s): {missing}")

SYSTEM_MYSQL_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}
ACTIVE_DATABASE = DB_NAME
ENGINE_CACHE = {}
SCHEMA_CACHES = {}


def normalize_database_name(database_name: str | None):
    return database_name.strip() if isinstance(database_name, str) and database_name.strip() else None


def _database_url(database_name: str | None = None):
    encoded_password = quote_plus(DB_PASSWORD)
    if DB_DIALECT == "postgresql":
        database = database_name or DB_NAME
        return f"postgresql+psycopg2://{DB_USER}:{encoded_password}@{DB_HOST}:{DB_PORT}/{database}"

    database_part = f"/{database_name}" if database_name else ""
    return f"mysql+pymysql://{DB_USER}:{encoded_password}@{DB_HOST}:{DB_PORT}{database_part}"


def get_engine(database_name: str | None = None):
    database = normalize_database_name(database_name or ACTIVE_DATABASE)
    cache_key = database or "__server__"
    if cache_key not in ENGINE_CACHE:
        ENGINE_CACHE[cache_key] = create_engine(_database_url(database), echo=False)
    return ENGINE_CACHE[cache_key]


engine = get_engine(DB_NAME)


def get_database_name():
    return ACTIVE_DATABASE or DB_NAME


def get_database_dialect():
    return "PostgreSQL" if DB_DIALECT == "postgresql" else "MySQL"


def list_accessible_databases():
    if DB_DIALECT != "mysql":
        return [DB_NAME] if DB_NAME else []

    if "__server__" not in ENGINE_CACHE:
        ENGINE_CACHE["__server__"] = create_engine(_database_url(None), echo=False)
    with ENGINE_CACHE["__server__"].connect() as connection:
        rows = connection.execute(text("SHOW DATABASES")).fetchall()
    return sorted(row[0] for row in rows if row[0] not in SYSTEM_MYSQL_DATABASES)


def validate_database_name(database_name: str):
    database = normalize_database_name(database_name)
    return bool(database) and database in list_accessible_databases()


def set_active_database(database_name: str | None):
    global ACTIVE_DATABASE, engine
    database = normalize_database_name(database_name) or DB_NAME
    if DB_DIALECT == "mysql" and not validate_database_name(database):
        raise ValueError(f"Database '{database}' is not accessible with the configured MySQL credentials.")
    ACTIVE_DATABASE = database
    engine = get_engine(database)
    if database not in SCHEMA_CACHES:
        refresh_schema_cache(database)
    return ACTIVE_DATABASE


def _cache_for(database_name: str | None = None):
    database = normalize_database_name(database_name) or get_database_name()
    return SCHEMA_CACHES.setdefault(
        database,
        {
            "schema": None,
            "primary_keys": None,
            "foreign_keys": None,
            "indexes": None,
            "error": None,
        },
    )


def refresh_schema_cache(database_name: str | None = None):
    """Refresh metadata cache from INFORMATION_SCHEMA without loading table rows."""
    database = normalize_database_name(database_name) or get_database_name()
    cache = _cache_for(database)
    try:
        cache["schema"] = _load_schema_from_db(database)
        cache["primary_keys"] = _load_primary_keys_from_db(database)
        cache["foreign_keys"] = _load_foreign_keys_from_db(database)
        cache["indexes"] = _load_indexes_from_db(database)
        cache["error"] = None
    except Exception as exc:
        cache["error"] = str(exc)
        raise
    return get_schema_cache(database)


def get_schema_cache(database_name: str | None = None):
    cache = _cache_for(database_name)
    return {
        "schema": cache["schema"],
        "primary_keys": cache["primary_keys"],
        "foreign_keys": cache["foreign_keys"],
        "indexes": cache["indexes"],
        "error": cache["error"],
    }


def get_schema_cache_error(database_name: str | None = None):
    return _cache_for(database_name)["error"]


def test_connection(database_name: str | None = None):
    database = normalize_database_name(database_name) or get_database_name()
    try:
        with get_engine(database).connect() as connection:
            database_sql = "SELECT current_database();" if DB_DIALECT == "postgresql" else "SELECT DATABASE();"
            result = connection.execute(text(database_sql))
            return {"ok": True, "database": result.fetchone()[0]}
    except Exception as exc:
        return {"ok": False, "database": database, "error": str(exc)}


def list_tables(database_name: str | None = None):
    return sorted(get_schema(database_name).keys())


def get_schema(database_name: str | None = None):
    database = normalize_database_name(database_name) or get_database_name()
    cache = _cache_for(database)
    if cache["schema"] is None:
        refresh_schema_cache(database)
    return cache["schema"]


def _load_schema_from_db(database_name: str):
    if DB_DIALECT == "postgresql":
        query = """
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = :schema_name
        ORDER BY TABLE_NAME, ORDINAL_POSITION;
        """
        params = {"schema_name": POSTGRES_SCHEMA}
    else:
        query = """
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = :database_name
        ORDER BY TABLE_NAME, ORDINAL_POSITION;
        """
        params = {"database_name": database_name}
    with get_engine(database_name).connect() as connection:
        result = connection.execute(text(query), params)
        schema_info = result.fetchall()
    schema_dict = {}
    for table_name, column_name, data_type in schema_info:
        schema_dict.setdefault(table_name, []).append({"column_name": column_name, "data_type": data_type})
    return schema_dict


def get_primary_keys(database_name: str | None = None):
    database = normalize_database_name(database_name) or get_database_name()
    cache = _cache_for(database)
    if cache["primary_keys"] is None:
        refresh_schema_cache(database)
    return cache["primary_keys"]


def _load_primary_keys_from_db(database_name: str):
    if DB_DIALECT == "postgresql":
        query = """
        SELECT kcu.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = :schema_name;
        """
        params = {"schema_name": POSTGRES_SCHEMA}
    else:
        query = """
        SELECT TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = :database_name
          AND CONSTRAINT_NAME = 'PRIMARY';
        """
        params = {"database_name": database_name}

    with get_engine(database_name).connect() as connection:
        rows = connection.execute(text(query), params).fetchall()

    primary_keys = {}
    for table_name, column_name in rows:
        primary_keys.setdefault(table_name, []).append(column_name)
    return primary_keys


def get_foreign_keys(database_name: str | None = None):
    database = normalize_database_name(database_name) or get_database_name()
    cache = _cache_for(database)
    if cache["foreign_keys"] is None:
        refresh_schema_cache(database)
    return cache["foreign_keys"]


def _load_foreign_keys_from_db(database_name: str):
    if DB_DIALECT == "postgresql":
        query = """
        SELECT
            kcu.table_name,
            kcu.column_name,
            ccu.table_name AS referenced_table_name,
            ccu.column_name AS referenced_column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = :schema_name;
        """
        params = {"schema_name": POSTGRES_SCHEMA}
    else:
        query = """
        SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = :database_name
          AND REFERENCED_TABLE_NAME IS NOT NULL;
        """
        params = {"database_name": database_name}

    with get_engine(database_name).connect() as connection:
        rows = connection.execute(text(query), params).fetchall()

    return [
        {
            "table": table_name,
            "column": column_name,
            "referenced_table": referenced_table,
            "referenced_column": referenced_column,
        }
        for table_name, column_name, referenced_table, referenced_column in rows
    ]


def get_indexes(database_name: str | None = None):
    database = normalize_database_name(database_name) or get_database_name()
    cache = _cache_for(database)
    if cache["indexes"] is None:
        refresh_schema_cache(database)
    return cache["indexes"]


def _load_indexes_from_db(database_name: str):
    if DB_DIALECT == "postgresql":
        query = """
        SELECT
            t.relname AS table_name,
            i.relname AS index_name,
            a.attname AS column_name,
            ix.indisunique AS is_unique
        FROM pg_class t
        JOIN pg_index ix ON t.oid = ix.indrelid
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = :schema_name
        ORDER BY t.relname, i.relname;
        """
        params = {"schema_name": POSTGRES_SCHEMA}
    else:
        query = """
        SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, NON_UNIQUE
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = :database_name
        ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;
        """
        params = {"database_name": database_name}

    with get_engine(database_name).connect() as connection:
        rows = connection.execute(text(query), params).fetchall()

    indexes = {}
    for table_name, index_name, column_name, uniqueness in rows:
        is_unique = bool(uniqueness) if DB_DIALECT == "postgresql" else not bool(uniqueness)
        indexes.setdefault(table_name, {}).setdefault(index_name, {"columns": [], "unique": is_unique})
        indexes[table_name][index_name]["columns"].append(column_name)
    return indexes


if __name__ == "__main__":
    print(test_connection())
