import requests
import streamlit as st

API_URL = "http://127.0.0.1:8000"

st.set_page_config(page_title="AI SQL Assistant", layout="wide")
st.title("AI-Powered SQL Intelligent Assistant")


def api_get(path):
    response = requests.get(f"{API_URL}{path}", timeout=30)
    response.raise_for_status()
    return response.json()


def api_post(path, payload):
    response = requests.post(f"{API_URL}{path}", json=payload, timeout=120)
    response.raise_for_status()
    return response.json()


def show_api_error(exc):
    try:
        st.error(exc.response.json().get("detail", str(exc)))
    except Exception:
        st.error(str(exc))


def load_databases():
    try:
        return api_get("/databases").get("databases", [])
    except requests.RequestException:
        return []


def render_generator():
    st.subheader("Natural Language SQL Generator")
    databases = load_databases()
    use_database = st.checkbox("Use an existing MySQL database schema", value=False)
    selected_database = None

    if use_database:
        if databases:
            selected_database = st.selectbox("Database", databases)
            st.caption(f"SQL will be generated using the schema from `{selected_database}`.")
        else:
            st.warning("No accessible MySQL databases were found. General SQL generation is still available.")
            use_database = False
    else:
        st.info("General mode uses inferred tables and columns. SQL is not executable until a matching schema exists.")

    prompt = st.text_area("Ask your database question...", height=110)
    generate_col, clear_col = st.columns([1, 1])
    generate_clicked = generate_col.button("Generate SQL", type="primary")
    clear_clicked = clear_col.button("Clear")

    if clear_clicked:
        for key in ("prompt", "queries", "selected_index", "execution_result", "selected_database", "generation_mode"):
            st.session_state.pop(key, None)
        st.rerun()

    if generate_clicked:
        if not prompt.strip():
            st.warning("Enter a request first.")
        else:
            mode = "database_aware" if use_database and selected_database else "general"
            payload = {"question": prompt, "mode": mode, "database": selected_database}
            try:
                with st.spinner("Generating SQL with local Ollama..."):
                    data = api_post("/api/sql/generate", payload)
                if data.get("error"):
                    st.error(data["error"])
                else:
                    st.session_state["prompt"] = prompt
                    st.session_state["queries"] = data.get("queries", [])
                    st.session_state["selected_index"] = 0
                    st.session_state["selected_database"] = selected_database
                    st.session_state["generation_mode"] = mode
                    st.session_state.pop("execution_result", None)
            except requests.RequestException as exc:
                show_api_error(exc)

    render_queries()


def render_queries():
    queries = st.session_state.get("queries", [])
    if not queries:
        return

    st.subheader("Generated Query Options")
    labels = [f"Option {query['id']}: {query.get('description', '')}" for query in queries]
    selected_label = st.radio("Choose a query", labels, index=st.session_state.get("selected_index", 0))
    selected_query = queries[labels.index(selected_label)]
    sql_query = selected_query["sql"]
    database = st.session_state.get("selected_database")
    mode = st.session_state.get("generation_mode", "general")

    st.subheader("Selected Query")
    st.code(sql_query, language="sql")
    st.download_button("Download SQL", sql_query, file_name="generated_query.sql", mime="text/sql")

    warning = selected_query.get("execution_warning")
    if warning:
        st.warning(warning)

    if selected_query.get("assumptions"):
        st.subheader("Assumptions")
        for assumption in selected_query["assumptions"]:
            st.write(f"- {assumption}")

    if selected_query.get("proposed_schema"):
        st.subheader("Proposed Schema")
        st.write(selected_query["proposed_schema"])

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Explanation")
        explanation = selected_query.get("explanation", {})
        st.write(explanation.get("summary", "No explanation available."))
        for clause in explanation.get("clauses", []):
            st.markdown(f"**{clause['clause']}**")
            st.write(clause["explanation"])
    with col2:
        st.subheader("Validation and Risk")
        validation = selected_query.get("validation", {})
        if validation.get("valid"):
            st.success("Valid SQL")
        else:
            st.error("Invalid SQL")
        for error in validation.get("errors", []):
            st.error(error)
        for validation_warning in validation.get("warnings", []):
            st.warning(validation_warning)
        impact = selected_query.get("impact", {})
        st.write(f"Operation: {impact.get('operation')}")
        st.write(f"Risk: {impact.get('risk_level')}")

    can_execute = mode == "database_aware" and database and validation.get("valid")
    if can_execute:
        confirmed = False
        if selected_query.get("impact", {}).get("requires_confirmation"):
            confirmed = st.checkbox("I approve this data/schema-changing operation.")
        if st.button("Execute SQL"):
            try:
                result = api_post(
                    "/execute_sql",
                    {
                        "query": sql_query,
                        "natural_language_prompt": st.session_state.get("prompt"),
                        "confirmed": confirmed,
                        "database": database,
                    },
                )
                st.session_state["execution_result"] = result
            except requests.RequestException as exc:
                show_api_error(exc)
    elif mode == "general":
        st.info("Execution is disabled in general mode because no real database schema was selected.")

    render_execution_result()


def render_execution_result():
    result = st.session_state.get("execution_result")
    if not result:
        return
    st.subheader("Execution Result")
    if result.get("error"):
        st.error(result["error"])
        if result.get("requires_confirmation"):
            st.write(result.get("impact", {}))
        return
    rows = result.get("results", [])
    if rows:
        st.dataframe(rows, use_container_width=True)
    else:
        st.write(result.get("result_summary", "Query executed."))
    st.caption(f"Rows affected: {result.get('rows_affected', 0)}")


def render_schema_browser():
    st.subheader("MySQL Schema Browser")
    databases = load_databases()
    if not databases:
        st.info("No accessible MySQL databases found.")
        return
    database = st.selectbox("Database", databases, key="schema_database")
    col1, col2 = st.columns([1, 1])
    if col1.button("Test Connection"):
        try:
            st.session_state["connection_test"] = api_get(f"/databases/{database}/test")
        except requests.RequestException as exc:
            show_api_error(exc)
    if col2.button("Refresh Schema"):
        try:
            st.session_state["schema_result"] = api_post("/api/schema/refresh", {"database": database}).get("schema")
        except requests.RequestException as exc:
            show_api_error(exc)

    if st.session_state.get("connection_test"):
        test = st.session_state["connection_test"]
        if test.get("ok"):
            st.success(f"Connected to {test.get('database')}")
        else:
            st.error(test.get("error", "Connection failed."))

    try:
        schema = st.session_state.get("schema_result") or api_get(f"/databases/{database}/schema")
        tables = schema.get("tables", {})
        st.write(f"Available tables: {', '.join(tables.keys()) if tables else 'None'}")
        for table_name, table_info in tables.items():
            with st.expander(table_name):
                st.dataframe(table_info.get("columns", []), use_container_width=True)
    except requests.RequestException as exc:
        show_api_error(exc)


def render_history():
    st.subheader("Query History")
    try:
        history = api_get("/history").get("history", [])
        if history:
            st.dataframe(history, use_container_width=True)
        else:
            st.info("No query history yet.")
    except requests.RequestException as exc:
        show_api_error(exc)


tab_generate, tab_schema, tab_history = st.tabs(["SQL Generator", "Schema Browser", "Query History"])
with tab_generate:
    render_generator()
with tab_schema:
    render_schema_browser()
with tab_history:
    render_history()
