import json
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import streamlit as st
import time, os
import duckdb
import shutil
import sqlparse

from studio.state_db import connect, init_db
from studio.datasets import load_csvs_to_duckdb, list_tables, describe_table
from studio.dictionary import load_dictionary, to_json, pretty
from studio.agent_sql import run_agent_turn
from studio.duckdb_adapter import run_sql, get_conn, close_conn
from studio.sanitize import safe_table_name
from studio.sql_safety import wrap_with_limit, validate_readonly_sql


from dotenv import load_dotenv
load_dotenv()

# -------------------------
# Workspace-isolated storage
# -------------------------
STORAGE = Path("storage")
WORKSPACES_DIR = STORAGE / "workspaces"

def get_workspace_id() -> str:
    """
    Deployed-friendly default: each visitor/session gets a fresh workspace,
    so they don't see other people's datasets.

    Optional local persistence:
      export STUDIO_WORKSPACE_ID=local
    """
    if "workspace_id" not in st.session_state:
        st.session_state["workspace_id"] = os.getenv("STUDIO_WORKSPACE_ID") or str(uuid.uuid4())
    return st.session_state["workspace_id"]

def clear_app_state(keep: tuple[str, ...] = ("workspace_id",)) -> None:
    for k in list(st.session_state.keys()):
        if k not in keep:
            st.session_state.pop(k, None)

WS_ID = get_workspace_id()
WS_ROOT = WORKSPACES_DIR / WS_ID
UPLOADS = WS_ROOT / "uploads"
DUCKDB_DIR = WS_ROOT / "duckdb"
STATE_DB_PATH = WS_ROOT / "studio_state.sqlite"

st.set_page_config(page_title="Analytics Studio (BYO Data)", layout="wide")
st.title("Analytics Studio (BYO Data)")

# --- State DB init (per workspace) ---
conn = connect(STATE_DB_PATH)
init_db(conn)

def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

# -------------------------
# DB helpers
# -------------------------
def db_all_datasets():
    return conn.execute("SELECT * FROM datasets ORDER BY created_at DESC").fetchall()

def db_insert_dataset(dataset_id: str, name: str, duckdb_path: str, dictionary_json: str | None):
    conn.execute(
        "INSERT INTO datasets(dataset_id, name, duckdb_path, dictionary_json, created_at) VALUES (?,?,?,?,?)",
        (dataset_id, name, duckdb_path, dictionary_json, now_iso()),
    )
    conn.commit()

def get_dataset(dataset_id: str):
    return conn.execute("SELECT * FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()

def db_get_or_create_session(dataset_id: str) -> str:
    row = conn.execute(
        "SELECT session_id FROM chat_sessions WHERE dataset_id=? ORDER BY created_at DESC LIMIT 1",
        (dataset_id,),
    ).fetchone()
    if row:
        return row["session_id"]
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO chat_sessions(session_id, dataset_id, title, created_at) VALUES (?,?,?,?)",
        (session_id, dataset_id, "Chat", now_iso()),
    )
    conn.commit()
    return session_id

def db_add_message(session_id: str, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO chat_messages(session_id, role, content, created_at) VALUES (?,?,?,?)",
        (session_id, role, content, now_iso()),
    )
    conn.commit()

def db_get_messages(session_id: str, limit: int = 200):
    return conn.execute(
        "SELECT role, content FROM chat_messages WHERE session_id=? ORDER BY id ASC LIMIT ?",
        (session_id, limit),
    ).fetchall()

def db_save_view(dataset_id: str, name: str, sql: str) -> None:
    view_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO saved_views(view_id, dataset_id, name, sql, created_at) VALUES (?,?,?,?,?)",
        (view_id, dataset_id, name, sql, now_iso()),
    )
    conn.commit()

def db_save_plot(dataset_id: str, name: str, sql: str, chart_type: str, x_col: str, y_col: str, note: str = ""):
    plot_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO saved_plots(plot_id, dataset_id, name, sql, chart_type, x_col, y_col, note, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (plot_id, dataset_id, name, sql, chart_type, x_col, y_col, note, now_iso()),
    )
    conn.commit()

def db_clear_dataset_chats(dataset_id: str) -> None:
    conn.execute(
        "DELETE FROM chat_messages WHERE session_id IN (SELECT session_id FROM chat_sessions WHERE dataset_id=?)",
        (dataset_id,),
    )
    conn.execute("DELETE FROM chat_sessions WHERE dataset_id=?", (dataset_id,))
    conn.commit()

def db_clear_dataset_history(dataset_id: str) -> None:
    conn.execute("DELETE FROM saved_views WHERE dataset_id=?", (dataset_id,))
    conn.execute("DELETE FROM saved_plots WHERE dataset_id=?", (dataset_id,))
    conn.commit()

def delete_dataset_files(dataset_id: str, duckdb_path: str) -> None:
    # Close any cached DuckDB connection so the file can be removed cleanly
    try:
        close_conn(duckdb_path)
    except Exception:
        pass

    # Delete duckdb file
    try:
        Path(duckdb_path).unlink(missing_ok=True)
    except Exception:
        pass

    # Delete uploaded files for this dataset
    if UPLOADS.exists():
        for p in UPLOADS.glob(f"{dataset_id}__*"):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

def db_clear_dataset_monitoring(dataset_id: str) -> None:
    # Delete feedback for runs on this dataset
    conn.execute(
        "DELETE FROM llm_feedback WHERE run_id IN (SELECT run_id FROM llm_runs WHERE dataset_id=?)",
        (dataset_id,),
    )
    conn.execute("DELETE FROM llm_runs WHERE dataset_id=?", (dataset_id,))
    conn.commit()

def db_clear_dataset_tests(dataset_id: str) -> None:
    conn.execute("DELETE FROM eval_results WHERE dataset_id=?", (dataset_id,))
    conn.execute("DELETE FROM eval_questions WHERE dataset_id=?", (dataset_id,))
    conn.commit()

def db_delete_dataset(dataset_id: str) -> None:
    row = conn.execute("SELECT duckdb_path FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
    if not row:
        return

    duckdb_path = row["duckdb_path"]

    # Delete dependent rows first
    db_clear_dataset_chats(dataset_id)
    db_clear_dataset_history(dataset_id)
    db_clear_dataset_monitoring(dataset_id)
    db_clear_dataset_tests(dataset_id)

    conn.execute("DELETE FROM datasets WHERE dataset_id=?", (dataset_id,))
    conn.commit()

    # Delete files
    delete_dataset_files(dataset_id, duckdb_path)


import json as _json

def db_log_llm_run(
    dataset_id: str,
    page: str,
    model: str,
    top_k: int | None,
    question: str,
    response_text: str | None,
    sql_used: list[str] | None,
    latency_ms: int | None,
    error: str | None,
    session_id: str | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO llm_runs(run_id, dataset_id, session_id, page, model, top_k, question, response_text, sql_json, latency_ms, error, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            dataset_id,
            session_id,
            page,
            model,
            int(top_k) if top_k is not None else None,
            question,
            response_text,
            _json.dumps(sql_used or []),
            latency_ms,
            error,
            now_iso(),
        ),
    )
    conn.commit()
    return run_id

def db_add_feedback(run_id: str, rating: int, note: str = "") -> None:
    conn.execute(
        "INSERT INTO llm_feedback(feedback_id, run_id, rating, note, created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), run_id, int(rating), note.strip(), now_iso()),
    )
    conn.commit()

# -------------------------
# Demo dataset (one-click)
# -------------------------
DEMO_CSVS: dict[str, str] = {
    "patients.csv": """patient_id,full_name,birth_date,sex
1,Alice Johnson,1982-04-11,F
2,Bob Smith,1975-09-23,M
3,Carla Chen,1991-12-02,F
4,David Patel,1968-07-19,M
5,Elena Garcia,1988-03-05,F
""",
    "encounters.csv": """encounter_id,patient_id,encounter_date,department
10,1,2025-01-10,Primary Care
11,2,2025-01-12,Emergency
12,3,2025-01-14,Cardiology
13,4,2025-01-15,Oncology
14,5,2025-01-18,Orthopedics
""",
    "claims.csv": """claim_id,encounter_id,payer,amount,status
100,10,Aetna,125.50,Paid
101,11,BCBS,980.00,Denied
102,12,Aetna,450.00,Pending
103,13,United,1200.00,Paid
104,14,Cigna,700.00,Denied
""",
}

DEMO_DICTIONARY = {
    "about": "Demo healthcare-style dataset for Analytics Studio.",
    "tables": {
        "patients": {
            "description": "One row per patient.",
            "columns": {
                "patient_id": "Unique patient identifier (integer).",
                "full_name": "Patient full name.",
                "birth_date": "Date of birth (YYYY-MM-DD).",
                "sex": "Sex at registration (M/F).",
            },
        },
        "encounters": {
            "description": "One row per clinical encounter/visit.",
            "columns": {
                "encounter_id": "Unique encounter identifier.",
                "patient_id": "Foreign key to patients.patient_id.",
                "encounter_date": "Date of encounter (YYYY-MM-DD).",
                "department": "Department/service line.",
            },
        },
        "claims": {
            "description": "One row per insurance claim linked to an encounter.",
            "columns": {
                "claim_id": "Unique claim identifier.",
                "encounter_id": "Foreign key to encounters.encounter_id.",
                "payer": "Insurance payer.",
                "amount": "Claim amount (numeric).",
                "status": "Claim status (Paid/Denied/Pending).",
            },
        },
    },
    "example_questions": [
        "What tables do I have?",
        "Total claim amount by payer",
        "Denial rate by department",
        "Claims by status",
    ],
}

def create_demo_dataset() -> str:
    """Creates a demo dataset inside the current workspace and returns dataset_id."""
    dataset_id_new = str(uuid.uuid4())
    duckdb_path = DUCKDB_DIR / f"{dataset_id_new}.duckdb"
    UPLOADS.mkdir(parents=True, exist_ok=True)
    DUCKDB_DIR.mkdir(parents=True, exist_ok=True)

    # Write CSVs to disk
    table_to_csv: dict[str, Path] = {}
    for filename, csv_text in DEMO_CSVS.items():
        out_path = UPLOADS / f"{dataset_id_new}__{filename}"
        out_path.write_text(csv_text, encoding="utf-8")
        table = safe_table_name(Path(filename).stem)
        table_to_csv[table] = out_path

    # Load into DuckDB
    load_csvs_to_duckdb(duckdb_path, table_to_csv)

    # Store dictionary
    dictionary_json = json.dumps(DEMO_DICTIONARY)

    db_insert_dataset(
        dataset_id=dataset_id_new,
        name="Demo: Healthcare Claims",
        duckdb_path=str(duckdb_path),
        dictionary_json=dictionary_json,
    )
    return dataset_id_new

def delete_demo_datasets() -> int:
    rows = conn.execute(
        "SELECT dataset_id FROM datasets WHERE name LIKE 'Demo:%'"
    ).fetchall()
    for r in rows:
        db_delete_dataset(r["dataset_id"])
    return len(rows)

def reset_workspace() -> None:
    rows = conn.execute("SELECT dataset_id, duckdb_path FROM datasets").fetchall()
    for r in rows:
        delete_dataset_files(r["dataset_id"], r["duckdb_path"])

    # Wipe workspace DB tables (include monitoring/tests)
    conn.execute("DELETE FROM llm_feedback")
    conn.execute("DELETE FROM llm_runs")
    conn.execute("DELETE FROM eval_results")
    conn.execute("DELETE FROM eval_questions")

    conn.execute("DELETE FROM chat_messages")
    conn.execute("DELETE FROM chat_sessions")
    conn.execute("DELETE FROM saved_views")
    conn.execute("DELETE FROM saved_plots")
    conn.execute("DELETE FROM datasets")
    conn.commit()

    clear_app_state(keep=("workspace_id",))

    try:
        st.cache_resource.clear()
    except Exception:
        pass
    try:
        st.cache_data.clear()
    except Exception:
        pass

def new_empty_workspace() -> None:
    clear_app_state(keep=())
    st.session_state["workspace_id"] = str(uuid.uuid4())
    st.session_state["page"] = "Home"
    st.rerun()


# -------------------------
# Sidebar: Workspace + Dataset controls
# -------------------------

st.sidebar.header("Status")

key_ok = bool(os.getenv("OPENAI_API_KEY"))
if key_ok:
    st.sidebar.success("OPENAI_API_KEY detected")
else:
    st.sidebar.error("OPENAI_API_KEY missing (Chat/Ask SQL generation will fail)")

st.sidebar.caption("Workspace storage is private per visitor/session in deployed mode.")
st.sidebar.code(f"ws: {WS_ID[:8]}", language="text")

st.sidebar.divider()

col_ws1, col_ws2 = st.sidebar.columns(2)
if col_ws1.button("New workspace", use_container_width=True):
    new_empty_workspace()
    st.rerun()

if col_ws2.button("Load demo", use_container_width=True):
    try:
        did = create_demo_dataset()
        st.session_state["selected_dataset_id"] = did
        st.session_state["page"] = "Ask"
        st.rerun()
    except Exception as e:
        st.sidebar.error(f"{type(e).__name__}: {e}")

with st.sidebar.expander("Danger zone", expanded=False):
    st.warning("This will delete ALL datasets, chats, views, plots, monitoring logs, and tests in THIS workspace.")
    confirm_reset = st.checkbox("I understand", key="confirm_reset_ws")
    if st.button("Reset workspace", disabled=not confirm_reset, use_container_width=True):
        reset_workspace()
        st.success("Workspace reset.")
        st.rerun()

    if st.button("Delete demo datasets", use_container_width=True):
        n = delete_demo_datasets()
        st.success(f"Deleted {n} demo dataset(s).")
        st.rerun()

# Load datasets for this workspace DB
datasets = db_all_datasets()

st.sidebar.header("Dataset")

if datasets:
    options = [d["dataset_id"] for d in datasets]

    # Ensure selected value is valid BEFORE widget instantiation
    if st.session_state.get("selected_dataset_id") not in options:
        st.session_state["selected_dataset_id"] = options[0]

    # Map for display
    _by_id = {d["dataset_id"]: d for d in datasets}

    dataset_id = st.sidebar.selectbox(
        "Select",
        options=options,
        format_func=lambda did: f"{_by_id[did]['name']}  ({did[:8]})",
        key="selected_dataset_id",
    )

    # Quick dataset health
    try:
        dset = get_dataset(dataset_id)
        con = get_conn(Path(dset["duckdb_path"]), read_only=True)
        tcount = con.execute("SHOW TABLES").fetchall()
        st.sidebar.caption(f"Tables: {len(tcount)}")
    except Exception:
        st.sidebar.caption("Tables: (unable to inspect)")

    with st.sidebar.expander("Dataset actions", expanded=False):
        st.caption("Affects the selected dataset only.")
        confirm_ds = st.checkbox("Confirm dataset action", key="confirm_ds_action")

        c1, c2 = st.columns(2)
        if c1.button("Clear chats", disabled=not confirm_ds, use_container_width=True):
            db_clear_dataset_chats(dataset_id)
            st.session_state.pop("session_id", None)
            st.session_state.pop("session_dataset", None)
            st.success("Chats cleared.")
            st.rerun()

        if c2.button("Clear history", disabled=not confirm_ds, use_container_width=True):
            db_clear_dataset_history(dataset_id)
            st.success("Views/plots cleared.")
            st.rerun()

        c3, c4 = st.columns(2)
        if c3.button("Clear monitoring", disabled=not confirm_ds, use_container_width=True):
            db_clear_dataset_monitoring(dataset_id)
            st.success("Monitoring logs cleared.")
            st.rerun()

        if c4.button("Clear tests", disabled=not confirm_ds, use_container_width=True):
            db_clear_dataset_tests(dataset_id)
            st.success("Golden questions + results cleared.")
            st.rerun()

        if st.button("Delete dataset", disabled=not confirm_ds, use_container_width=True):
            db_delete_dataset(dataset_id)
            st.success("Dataset deleted.")
            st.rerun()

else:
    st.sidebar.info("No datasets yet. Upload one in Data or click Load demo.")
    dataset_id = None


# -------------------------
# Navigation
# -------------------------
PAGES = ["Home", "Data", "Ask", "Chat", "History", "Monitor", "About"]
default_page = st.session_state.get("page", "Home")
page = st.sidebar.radio("Navigate", PAGES, index=PAGES.index(default_page))
st.session_state["page"] = page


if page == "Home":
    st.header("Welcome")
    st.markdown(
        """
This is a lightweight **analytics copilot**:

- Upload CSVs (each file becomes a table)
- Ask questions → generate **read-only SQL**
- Run queries and export results
- Create plots and save them
- Chat conversationally (with session memory)
        """
    )

    st.subheader("Quick start")
    st.markdown(
        """
1) Go to **Data** → upload one or more CSV files → **Create dataset**  
2) Go to **Ask** → Generate SQL → Run SQL → Export / Save view / Plot  
3) Go to **Chat** → Ask follow-ups (“what tables do I have?”, “trend by month”, etc.)  
4) Go to **History** → Re-run views and plots
        """
    )

    st.info(
        "Deployed mode: each visitor gets a private workspace, so you won’t see other users’ uploads. "
        "Use the sidebar to reset or delete datasets."
    )

    c1, c2, c3 = st.columns(3)
    if c1.button("Go to Data"):
        st.session_state["page"] = "Data"
        st.rerun()
    if c2.button("Go to Ask"):
        st.session_state["page"] = "Ask"
        st.rerun()
    if c3.button("Go to Chat"):
        st.session_state["page"] = "Chat"
        st.rerun()

    st.divider()
    st.subheader("Local persistence (optional)")
    st.code("export STUDIO_WORKSPACE_ID=local", language="bash")
    st.caption("If you set this env var, your local app will reuse the same workspace across restarts.")
elif page == "Data":
    st.subheader("Data")
    st.caption("Upload one or more CSVs. Each CSV becomes a table (table name = filename).")

    # --- Create dataset
    with st.expander("Create a new dataset", expanded=True):
        name = st.text_input("Dataset name", value="My dataset")

        csv_files = st.file_uploader(
            "CSV file(s)",
            type=["csv"],
            accept_multiple_files=True,
        )

        dict_file = st.file_uploader(
            "Data dictionary (optional) — YAML or JSON",
            type=["yml", "yaml", "json"],
            accept_multiple_files=False,
        )

        if st.button("Create dataset", type="primary"):
            if not csv_files:
                st.error("Upload at least one CSV.")
            else:
                dataset_id_new = str(uuid.uuid4())
                duckdb_path = DUCKDB_DIR / f"{dataset_id_new}.duckdb"
                UPLOADS.mkdir(parents=True, exist_ok=True)
                DUCKDB_DIR.mkdir(parents=True, exist_ok=True)

                table_to_csv = {}
                for f in csv_files:
                    table = safe_table_name(Path(f.name).stem)
                    out_path = UPLOADS / f"{dataset_id_new}__{f.name}"
                    out_path.write_bytes(f.getbuffer())
                    table_to_csv[table] = out_path

                load_csvs_to_duckdb(duckdb_path, table_to_csv)

                dictionary_json = None
                if dict_file:
                    dict_path = UPLOADS / f"{dataset_id_new}__{dict_file.name}"
                    dict_path.write_bytes(dict_file.getbuffer())
                    d = load_dictionary(dict_path)
                    dictionary_json = to_json(d)

                db_insert_dataset(
                    dataset_id=dataset_id_new,
                    name=name.strip() or "Untitled dataset",
                    duckdb_path=str(duckdb_path),
                    dictionary_json=dictionary_json,
                )

                st.success("Dataset created. Select it in the sidebar.")
                st.rerun()

    st.divider()

    # --- Manage datasets
    st.subheader("Manage datasets (this workspace)")
    datasets = db_all_datasets()

    if not datasets:
        st.info("No datasets yet. Create one above.")
    else:
        for d in datasets:
            with st.expander(f"{d['name']}  ({d['dataset_id'][:8]})", expanded=False):
                st.write(f"**Dataset ID:** `{d['dataset_id']}`")
                st.write(f"**DuckDB:** `{d['duckdb_path']}`")

                # Inspect tables/columns
                try:
                    con = get_conn(Path(d["duckdb_path"]), read_only=True)
                    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
                    st.write(f"**Tables:** {len(tables)}")

                    if tables:
                        t = st.selectbox(
                            "Inspect table",
                            tables,
                            key=f"inspect_{d['dataset_id']}",
                        )
                        desc = con.execute(f"DESCRIBE {t}").fetchall()
                        df_desc = pd.DataFrame(
                            desc,
                            columns=["column", "type", "null", "key", "default", "extra"][: len(desc[0])],
                        )
                        st.dataframe(df_desc, use_container_width=True)
                except Exception as e:
                    st.warning(f"Could not inspect DuckDB file: {type(e).__name__}: {e}")

                if d["dictionary_json"]:
                    st.subheader("Data dictionary")
                    st.code(pretty(json.loads(d["dictionary_json"])), language="json")

                st.divider()

                # Delete controls
                st.warning("Danger zone: delete dataset")
                confirm = st.checkbox(
                    "I understand this deletes the dataset and its chats/views/plots in this workspace.",
                    key=f"confirm_delete_{d['dataset_id']}",
                )
                if st.button(
                    "Delete dataset",
                    key=f"delete_btn_{d['dataset_id']}",
                    disabled=not confirm,
                ):
                    db_delete_dataset(d["dataset_id"])
                    st.success("Deleted dataset.")
                    st.rerun()
elif page == "Ask":
    st.header("Ask")
    st.caption("Generate read-only SQL, edit it, run it, export results, plot, and save as a view.")

    if not dataset_id:
        st.info("Select a dataset in the sidebar first.")
    else:
        dset = get_dataset(dataset_id)
        duckdb_path = dset["duckdb_path"]

        # Give Ask its own session id so monitoring can group "Ask" runs too
        if "ask_session_id" not in st.session_state or st.session_state.get("ask_session_dataset") != dataset_id:
            st.session_state["ask_session_id"] = str(uuid.uuid4())
            st.session_state["ask_session_dataset"] = dataset_id
        ask_session_id = st.session_state["ask_session_id"]

        # ---- Controls
        colA, colB, colC = st.columns([2, 1, 1])
        model = colA.text_input("OpenAI model", value=os.getenv("OPENAI_MODEL", "gpt-5-nano"))
        top_k = colB.number_input("Top-K rows (agent)", min_value=5, max_value=200, value=50, step=5)
        limit = colC.number_input("Preview rows", min_value=10, max_value=5000, value=200, step=10)

        question = st.text_input(
            "Question",
            value=st.session_state.get("ask_question", "denial rate by department"),
            placeholder="e.g., total claim amount by payer",
        )
        st.session_state["ask_question"] = question

        # ---- State init
        st.session_state.setdefault("sql_text", "")
        st.session_state.setdefault("ask_trace", "")
        st.session_state.setdefault("last_df", None)
        st.session_state.setdefault("last_safe_sql", None)
        st.session_state.setdefault("last_run_id_ask", None)  # for feedback

        # ---- Actions
        col1, col2, col3 = st.columns([1, 1, 2])
        generate = col1.button("Generate SQL", use_container_width=True)
        run_btn = col2.button("Run SQL", use_container_width=True)
        clear_btn = col3.button("Clear", use_container_width=False)

        if clear_btn:
            st.session_state["sql_text"] = ""
            st.session_state["ask_trace"] = ""
            st.session_state["last_df"] = None
            st.session_state["last_safe_sql"] = None
            st.session_state["last_run_id_ask"] = None
            st.rerun()

        # ---- Generate SQL (LLM call + monitoring)
        if generate:
            start = time.time()
            run_id = None
            try:
                mini_history = [
                    ("user", "You generate SQL for analytics questions over the dataset."),
                    ("assistant", "OK. I will generate read-only SQL (SELECT/CTE) only."),
                ]

                assistant_text, sql_used = run_agent_turn(
                    duckdb_path=str(duckdb_path),
                    model=model,
                    user_text=(
                        "Generate SQL ONLY (no explanation) for this question. "
                        "Return a single SELECT or WITH...SELECT statement.\n\n"
                        f"Question: {question}"
                    ),
                    history=mini_history,
                    top_k=int(top_k),
                )

                sql = sql_used[-1] if sql_used else (assistant_text or "")
                sql = sql.replace("```sql", "").replace("```", "").strip()

                safe_sql = validate_readonly_sql(sql)
                st.session_state["sql_text"] = sqlparse.format(safe_sql, reindent=True, keyword_case="upper")
                st.session_state["ask_trace"] = "Generated SQL via agent (SQL-only instruction)."

                latency_ms = int((time.time() - start) * 1000)
                run_id = db_log_llm_run(
                    dataset_id=dataset_id,
                    session_id=ask_session_id,
                    page="Ask.GenerateSQL",
                    model=model,
                    top_k=int(top_k),
                    question=question,
                    response_text=assistant_text or "",
                    sql_used=sql_used,
                    latency_ms=latency_ms,
                    error=None,
                )
                st.session_state["last_run_id_ask"] = run_id

                st.success("Generated SQL. Edit it if you want, then click Run SQL.")
                st.rerun()

            except Exception as e:
                latency_ms = int((time.time() - start) * 1000)
                err = f"{type(e).__name__}: {e}"
                db_log_llm_run(
                    dataset_id=dataset_id,
                    session_id=ask_session_id,
                    page="Ask.GenerateSQL",
                    model=model,
                    top_k=int(top_k),
                    question=question,
                    response_text=None,
                    sql_used=[],
                    latency_ms=latency_ms,
                    error=err,
                )
                st.error(err)

        # ---- SQL Editor (always visible)
        sql_text = st.text_area("SQL (editable)", value=st.session_state["sql_text"], height=220)
        st.session_state["sql_text"] = sql_text

        if st.session_state.get("ask_trace"):
            st.caption(f"Trace: {st.session_state['ask_trace']}")

        # ---- Optional feedback on last Ask run
        if st.session_state.get("last_run_id_ask"):
            with st.expander("Rate the last SQL generation (optional)"):
                with st.form("ask_feedback_form", clear_on_submit=True):
                    rating = st.radio("Rating", ["👍 Good", "👎 Bad"], horizontal=True)
                    note = st.text_input("Note (optional)")
                    submitted = st.form_submit_button("Save feedback")
                    if submitted:
                        r = +1 if rating.startswith("👍") else -1
                        db_add_feedback(st.session_state["last_run_id_ask"], rating=r, note=note)
                        st.success("Saved feedback.")
                        st.rerun()
        st.divider()

        # ---- Run SQL (no LLM call; just DuckDB)
        if run_btn:
            try:
                sql = (st.session_state.get("sql_text") or "").strip()
                if not sql:
                    st.warning("SQL editor is empty. Click Generate SQL first (or paste a SELECT).")
                else:
                    safe_sql = validate_readonly_sql(sql)
                    preview_sql = wrap_with_limit(safe_sql, int(limit))

                    cols, rows = run_sql(duckdb_path, preview_sql)
                    df = pd.DataFrame(rows, columns=cols)

                    st.session_state["last_df"] = df
                    st.session_state["last_safe_sql"] = safe_sql

                    st.subheader("Results")
                    st.dataframe(df, use_container_width=True)
                    st.caption(f"Showing up to {int(limit)} rows.")

            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")

        # ---- Export + Save + Plot (only if we have results)
        df = st.session_state.get("last_df")
        safe_sql = st.session_state.get("last_safe_sql")

        if df is not None and safe_sql:
            st.divider()

            # Export
            st.subheader("Export")
            csv_bytes = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download results as CSV",
                data=csv_bytes,
                file_name=f"{(question or 'results').strip().replace(' ', '_')}.csv",
                mime="text/csv",
            )

            # Save View
            st.subheader("Save as View")
            default_name = f"view_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            view_name = st.text_input("View name", value=default_name, key="view_name")
            if st.button("Save view"):
                db_save_view(dataset_id, view_name.strip(), safe_sql)
                st.success(f"Saved view: {view_name.strip()}")

            # Plot (keep your existing plotting logic)
            st.divider()
            st.subheader("Plot")

            if df.empty:
                st.info("No rows returned, nothing to plot.")
            elif len(df.columns) < 2:
                st.info("Need at least 2 columns to plot.")
            else:
                chart = st.selectbox("Chart type", ["Bar", "Line", "Scatter"], index=0)
                cols_all = list(df.columns)
                x_col = st.selectbox("X column", cols_all, index=0)
                y_candidates = [c for c in cols_all if c != x_col]
                y_col = st.selectbox("Y column", y_candidates, index=0)

                y = pd.to_numeric(df[y_col], errors="coerce")
                plot_df = df.copy()
                plot_df["_y"] = y
                plot_df = plot_df.dropna(subset=["_y"])

                if plot_df.empty:
                    st.warning("Selected Y column isn't numeric (or all values are missing). Try a different Y column.")
                else:
                    fig, ax = plt.subplots()
                    if chart == "Bar":
                        ax.bar(plot_df[x_col].astype(str), plot_df["_y"])
                        plt.xticks(rotation=45, ha="right")
                    elif chart == "Line":
                        ax.plot(plot_df[x_col], plot_df["_y"], marker="o")
                    else:
                        ax.scatter(plot_df[x_col], plot_df["_y"])
                    ax.set_xlabel(x_col)
                    ax.set_ylabel(y_col)
                    st.pyplot(fig, clear_figure=True)

                    st.subheader("Save plot")
                    plot_name = st.text_input(
                        "Plot name",
                        value=f"plot_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}",
                        key="plot_name",
                    )
                    plot_note = st.text_input("Description (optional)", value="", key="plot_note")

                    if st.button("Save plot"):
                        db_save_plot(
                            dataset_id=dataset_id,
                            name=plot_name.strip() or "Untitled plot",
                            sql=safe_sql,
                            chart_type=chart,
                            x_col=x_col,
                            y_col=y_col,
                            note=plot_note.strip(),
                        )
                        st.success("Saved plot to History.")
elif page == "Chat":
    st.subheader("Chat")

    if not dataset_id:
        st.info("Select a dataset in the sidebar first.")
    else:
        dset = get_dataset(dataset_id)
        duckdb_path = dset["duckdb_path"]

        # Chat session (persisted)
        if "session_id" not in st.session_state or st.session_state.get("session_dataset") != dataset_id:
            st.session_state.session_id = db_get_or_create_session(dataset_id)
            st.session_state.session_dataset = dataset_id

        session_id = st.session_state.session_id

        # Controls
        colA, colB, colC = st.columns([2, 1, 1])
        model = colA.text_input("OpenAI model", value=os.getenv("OPENAI_MODEL", "gpt-5-nano"))
        top_k = colB.number_input("Top-K rows", min_value=5, max_value=200, value=50, step=5)

        if colC.button("New chat"):
            session_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO chat_sessions(session_id, dataset_id, title, created_at) VALUES (?,?,?,?)",
                (session_id, dataset_id, "Chat", now_iso()),
            )
            conn.commit()
            st.session_state.session_id = session_id
            st.session_state["last_run_id_chat"] = None
            st.rerun()

        st.session_state.setdefault("last_run_id_chat", None)

        # Render existing messages
        msgs = db_get_messages(session_id, limit=200)
        for m in msgs:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])

        user_text = st.chat_input("Ask about your dataset (tables, columns, metrics, etc.)")

        if user_text:
            db_add_message(session_id, "user", user_text)
            with st.chat_message("user"):
                st.markdown(user_text)

            with st.chat_message("assistant"):
                placeholder = st.empty()

                start = time.time()
                run_id = None
                try:
                    msgs = db_get_messages(session_id, limit=200)
                    history = [(m["role"], m["content"]) for m in msgs]

                    assistant_text, sql_used = run_agent_turn(
                        duckdb_path=str(duckdb_path),
                        model=model,
                        user_text=user_text,
                        history=history,
                        top_k=int(top_k),
                    )

                    placeholder.markdown(assistant_text)
                    db_add_message(session_id, "assistant", assistant_text)

                    latency_ms = int((time.time() - start) * 1000)
                    run_id = db_log_llm_run(
                        dataset_id=dataset_id,
                        session_id=session_id,
                        page="Chat",
                        model=model,
                        top_k=int(top_k),
                        question=user_text,
                        response_text=assistant_text,
                        sql_used=sql_used,
                        latency_ms=latency_ms,
                        error=None,
                    )
                    st.session_state["last_run_id_chat"] = run_id

                    # Save SQL into views/history
                    if sql_used:
                        saved = 0
                        for q in list(dict.fromkeys(sql_used))[:5]:  # dedupe + cap
                            try:
                                safe_q = validate_readonly_sql(q)
                                db_save_view(
                                    dataset_id,
                                    f"chat_query_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{saved+1}",
                                    safe_q,
                                )
                                saved += 1
                            except Exception:
                                # Skip unsafe/unparseable SQL
                                continue

                        with st.expander("SQL used"):
                            for q in sql_used:
                                st.code(q, language="sql")
                
                except Exception as e:
                    latency_ms = int((time.time() - start) * 1000)
                    err = f"{type(e).__name__}: {e}"
                    placeholder.error(err)
                    db_add_message(session_id, "assistant", f"Error: {err}")

                    db_log_llm_run(
                        dataset_id=dataset_id,
                        session_id=session_id,
                        page="Chat",
                        model=model,
                        top_k=int(top_k),
                        question=user_text,
                        response_text=None,
                        sql_used=[],
                        latency_ms=latency_ms,
                        error=err,
                    )

        # Optional feedback UI for last chat response
        if st.session_state.get("last_run_id_chat"):
            with st.expander("Rate the last assistant response (optional)"):
                with st.form("chat_feedback_form", clear_on_submit=True):
                    rating = st.radio("Rating", ["👍 Good", "👎 Bad"], horizontal=True, key="chat_rating")
                    note = st.text_input("Note (optional)", key="chat_note")
                    submitted = st.form_submit_button("Save feedback")
                    if submitted:
                        r = +1 if rating.startswith("👍") else -1
                        db_add_feedback(st.session_state["last_run_id_chat"], rating=r, note=note)
                        st.success("Saved feedback.")
                        st.rerun()
elif page == "History":
    st.subheader("Analytics History")
    st.caption("Views are saved SQL. Plots are saved chart configs you can re-run.")

    if not dataset_id:
        st.info("Select a dataset in the sidebar first.")
    else:
        dset = get_dataset(dataset_id)
        duckdb_path = dset["duckdb_path"]

        preview_limit = st.number_input("Preview rows", 10, 5000, 200, 10)

        # ---------- Saved Views (SQLite) ----------
        st.markdown("## Saved Views")
        view_rows = conn.execute(
            "SELECT view_id, name, sql, created_at "
            "FROM saved_views WHERE dataset_id=? "
            "ORDER BY created_at DESC LIMIT 50",
            (dataset_id,),
        ).fetchall()

        if not view_rows:
            st.info("No saved views yet.")
        else:
            for r in view_rows:
                with st.expander(f"{r['name']} — {r['created_at']}", expanded=False):
                    st.code(r["sql"], language="sql")

                    c1, c2, c3, c4 = st.columns([1, 1, 1, 1])

                    if c1.button("Open in Ask", key=f"open_view_{r['view_id']}"):
                        st.session_state["sql_text"] = r["sql"]
                        st.session_state["page"] = "Ask"
                        st.rerun()

                    if c2.button("Run", key=f"run_view_{r['view_id']}"):
                        try:
                            safe_sql = validate_readonly_sql(r["sql"])
                            cols, rows = run_sql(duckdb_path, wrap_with_limit(safe_sql, int(preview_limit)))
                            df = pd.DataFrame(rows, columns=cols)
                            st.dataframe(df, use_container_width=True)
                        except Exception as e:
                            st.error(f"{type(e).__name__}: {e}")

                    c3.download_button(
                        "Download SQL",
                        data=r["sql"],
                        file_name=f"{(r['name'] or 'view').replace(' ', '_')}.sql",
                        mime="text/plain",
                        key=f"dl_view_{r['view_id']}",
                    )

                    if c4.button("Delete", key=f"del_view_{r['view_id']}"):
                        conn.execute("DELETE FROM saved_views WHERE view_id=?", (r["view_id"],))
                        conn.commit()
                        st.success("Deleted view.")
                        st.rerun()

        st.divider()

        # ---------- Saved Plots (SQLite metadata + DuckDB execution) ----------
        st.markdown("## Saved Plots")
        plot_rows = conn.execute(
            "SELECT plot_id, name, sql, chart_type, x_col, y_col, note, created_at "
            "FROM saved_plots WHERE dataset_id=? "
            "ORDER BY created_at DESC LIMIT 50",
            (dataset_id,),
        ).fetchall()

        if not plot_rows:
            st.info("No saved plots yet. Make one in Ask → Plot → Save plot.")
        else:
            for p in plot_rows:
                with st.expander(f"{p['name']} — {p['chart_type']} — {p['created_at']}", expanded=False):
                    if p["note"]:
                        st.caption(p["note"])

                    st.code(p["sql"], language="sql")
                    st.caption(f"X: {p['x_col']}   |   Y: {p['y_col']}")

                    c1, c2 = st.columns([1, 1])

                    if c1.button("Re-run & Preview", key=f"rerun_plot_{p['plot_id']}"):
                        try:
                            safe_sql = validate_readonly_sql(p["sql"])
                            cols, rows = run_sql(duckdb_path, wrap_with_limit(safe_sql, int(preview_limit)))
                            df = pd.DataFrame(rows, columns=cols)

                            if df.empty:
                                st.warning("Query returned no rows.")
                            elif p["x_col"] not in df.columns or p["y_col"] not in df.columns:
                                st.error("Saved plot columns not found in the query result.")
                            else:
                                y = pd.to_numeric(df[p["y_col"]], errors="coerce")
                                plot_df = df.copy()
                                plot_df["_y"] = y
                                plot_df = plot_df.dropna(subset=["_y"])

                                if plot_df.empty:
                                    st.warning("Y column isn't numeric after coercion.")
                                else:
                                    fig, ax = plt.subplots()
                                    if p["chart_type"] == "Bar":
                                        ax.bar(plot_df[p["x_col"]].astype(str), plot_df["_y"])
                                        plt.xticks(rotation=45, ha="right")
                                    elif p["chart_type"] == "Line":
                                        ax.plot(plot_df[p["x_col"]], plot_df["_y"], marker="o")
                                    else:
                                        ax.scatter(plot_df[p["x_col"]], plot_df["_y"])

                                    ax.set_xlabel(p["x_col"])
                                    ax.set_ylabel(p["y_col"])
                                    st.pyplot(fig, clear_figure=True)

                                    st.dataframe(df.head(200), use_container_width=True)

                        except Exception as e:
                            st.error(f"{type(e).__name__}: {e}")

                    if c2.button("Delete", key=f"del_plot_{p['plot_id']}"):
                        conn.execute("DELETE FROM saved_plots WHERE plot_id=?", (p["plot_id"],))
                        conn.commit()
                        st.success("Deleted plot.")
                        st.rerun()
elif page == "About":
    st.subheader("About this app")

    st.markdown(
        """
### Healthcare NL2SQL Analytics Studio

A lightweight analytics copilot you can demo and deploy:

- Upload datasets (CSV) + optional data dictionary/docs
- Ask questions conversationally
- Generates **read-only SQL** and runs it
- Saves queries as reusable **views**
- Maintains a reproducible **analysis history**
        """
    )

    st.markdown("### Tips")
    st.markdown(
        """
- Start by uploading a small CSV and (optionally) a dictionary.
- Use Chat to explore: “what tables/columns do I have?”, “denial rate by department”, etc.
- Save useful queries in **Ask** so they show up in **History**.
        """
    )
elif page == "Monitor":
    st.subheader("Monitoring & Testing")

    if not dataset_id:
        st.info("Select a dataset in the sidebar first.")
    else:
        st.markdown("## Recent LLM Runs")

        # quick KPIs
        kpi = conn.execute(
            "SELECT "
            "COUNT(*) as n, "
            "SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) as n_err, "
            "AVG(CASE WHEN latency_ms IS NOT NULL THEN latency_ms END) as avg_ms "
            "FROM llm_runs WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
        n = kpi["n"] or 0
        n_err = kpi["n_err"] or 0
        avg_ms = int(kpi["avg_ms"] or 0)
        # Latency distribution (p50/p95) ignoring nulls
        lat_rows = conn.execute(
            "SELECT latency_ms FROM llm_runs WHERE dataset_id=? AND latency_ms IS NOT NULL ORDER BY latency_ms",
            (dataset_id,),
        ).fetchall()
        lats = [r["latency_ms"] for r in lat_rows if r["latency_ms"] is not None]

        def _pct(vals, p):
            if not vals:
                return 0
            i = int(round((len(vals) - 1) * p))
            i = max(0, min(i, len(vals) - 1))
            return vals[i]

        p50 = _pct(lats, 0.50)
        p95 = _pct(lats, 0.95)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total runs", n)
        c2.metric("Errors", n_err)
        c3.metric("Avg latency (ms)", avg_ms)
        c4.metric("p50 (ms)", p50)
        c5.metric("p95 (ms)", p95)

        st.divider()

        # Recent errors quick view
        err_rows = conn.execute(
            "SELECT created_at, page, question, error "
            "FROM llm_runs WHERE dataset_id=? AND error IS NOT NULL AND error != '' "
            "ORDER BY created_at DESC LIMIT 20",
            (dataset_id,),
        ).fetchall()

        if err_rows:
            st.markdown("## Recent errors")
            df_err = pd.DataFrame([dict(r) for r in err_rows])
            st.dataframe(df_err, use_container_width=True)

        # Export runs
        all_runs = conn.execute(
            "SELECT created_at, page, model, top_k, question, latency_ms, error "
            "FROM llm_runs WHERE dataset_id=? ORDER BY created_at DESC LIMIT 2000",
            (dataset_id,),
        ).fetchall()
        if all_runs:
            df_runs = pd.DataFrame([dict(r) for r in all_runs])
            st.download_button(
                "Download runs CSV",
                data=df_runs.to_csv(index=False).encode("utf-8"),
                file_name="llm_runs.csv",
                mime="text/csv",
            )

        st.divider()

        q = st.text_input("Filter (question contains)", value="")
        limit = st.number_input("Rows", 10, 500, 100, 10)

        if q.strip():
            rows = conn.execute(
                "SELECT run_id, page, model, question, latency_ms, error, created_at "
                "FROM llm_runs WHERE dataset_id=? AND question LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (dataset_id, f"%{q.strip()}%", int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT run_id, page, model, question, latency_ms, error, created_at "
                "FROM llm_runs WHERE dataset_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (dataset_id, int(limit)),
            ).fetchall()

        if not rows:
            st.info("No runs logged yet. Use Ask or Chat first.")
        else:
            for r in rows:
                label = f"{r['created_at']} — {r['page']} — {r['model']} — {r['question'][:80]}"
                with st.expander(label, expanded=False):
                    full = conn.execute(
                        "SELECT * FROM llm_runs WHERE run_id=?",
                        (r["run_id"],),
                    ).fetchone()

                    st.write(f"**Latency:** {full['latency_ms']} ms")
                    if full["error"]:
                        st.error(full["error"])

                    st.markdown("**Question**")
                    st.code(full["question"])

                    if full["response_text"]:
                        st.markdown("**Response**")
                        st.write(full["response_text"])

                    sqls = json.loads(full["sql_json"] or "[]")
                    if sqls:
                        st.markdown("**SQL used**")
                        for s in sqls:
                            st.code(s, language="sql")

                    # feedback summary + add feedback
                    fb = conn.execute(
                        "SELECT rating, note, created_at FROM llm_feedback WHERE run_id=? ORDER BY created_at DESC",
                        (full["run_id"],),
                    ).fetchall()
                    if fb:
                        st.markdown("**Feedback**")
                        for f in fb:
                            st.write(f"{'👍' if f['rating'] > 0 else '👎'} {f['created_at']} — {f['note'] or ''}")

                    st.divider()
                    st.markdown("### Add feedback")
                    c1, c2 = st.columns([1,3])
                    up = c1.button("👍", key=f"mon_up_{full['run_id']}")
                    dn = c1.button("👎", key=f"mon_dn_{full['run_id']}")
                    note = c2.text_input("Note", key=f"mon_note_{full['run_id']}")
                    if up:
                        db_add_feedback(full["run_id"], +1, note)
                        st.success("Saved.")
                        st.rerun()
                    if dn:
                        db_add_feedback(full["run_id"], -1, note)
                        st.success("Saved.")
                        st.rerun()

        st.divider()
        st.markdown("## Test Harness (Golden Questions)")

        st.caption("Add a few golden questions + expected SQL. Run tests after prompt/model changes.")
        cA, cB = st.columns([2, 1])
        new_q = cA.text_input("New golden question", value="")
        add_btn = cB.button("Add")

        exp_sql = st.text_area("Expected SQL (optional, but enables pass/fail)", value="", height=120)
        note = st.text_input("Note (optional)", value="")

        if add_btn and new_q.strip():
            qid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO eval_questions(qid, dataset_id, question, expected_sql, note, created_at) VALUES (?,?,?,?,?,?)",
                (qid, dataset_id, new_q.strip(), exp_sql.strip() or None, note.strip() or None, now_iso()),
            )
            conn.commit()
            st.success("Added golden question.")
            st.rerun()

        gold = conn.execute(
            "SELECT qid, question, expected_sql, note, created_at FROM eval_questions WHERE dataset_id=? ORDER BY created_at DESC",
            (dataset_id,),
        ).fetchall()

        if not gold:
            st.info("No golden questions yet.")
        else:
            st.write(f"Golden questions: **{len(gold)}**")
            run_tests = st.button("Run test suite")

            # controls for running tests
            model_t = st.text_input("Test model", value=os.getenv("OPENAI_MODEL", "gpt-5-nano"), key="test_model")
            topk_t = st.number_input("Test top_k", 5, 200, 50, 5, key="test_topk")

            if run_tests:
                dset = get_dataset(dataset_id)
                duckdb_path = dset["duckdb_path"]

                def _norm(rows):
                    # order-insensitive normalization
                    return sorted(tuple(map(str, r)) for r in rows)

                for g in gold:
                    q = g["question"]
                    expected_sql = (g["expected_sql"] or "").strip()

                    got_sql = ""
                    passed = 0
                    err = None
                    start = time.time()
                    try:
                        # run agent to get SQL
                        assistant_text, sql_used = run_agent_turn(
                            duckdb_path=str(duckdb_path),
                            model=model_t,
                            user_text=("Generate SQL ONLY (no explanation). " + q),
                            history=[("user", "Generate read-only SQL only."), ("assistant", "OK.")],
                            top_k=int(topk_t),
                        )
                        got_sql = (sql_used[-1] if sql_used else assistant_text).strip()
                        got_sql = got_sql.replace("```sql", "").replace("```", "").strip()
                        got_sql = validate_readonly_sql(got_sql)

                        if expected_sql:
                            expected_sql = validate_readonly_sql(expected_sql)

                            # compare results
                            exp_cols, exp_rows = run_sql(duckdb_path, wrap_with_limit(expected_sql, 500))
                            got_cols, got_rows = run_sql(duckdb_path, wrap_with_limit(got_sql, 500))
                            passed = 1 if _norm(exp_rows) == _norm(got_rows) else 0
                        else:
                            passed = 0  # no expected → just log for review
                    except Exception as e:
                        err = f"{type(e).__name__}: {e}"
                        passed = 0

                    latency_ms = int((time.time() - start) * 1000)

                    conn.execute(
                        "INSERT INTO eval_results(eval_id, dataset_id, qid, model, top_k, got_sql, pass, latency_ms, error, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), dataset_id, g["qid"], model_t, int(topk_t), got_sql or None, passed, latency_ms, err, now_iso()),
                    )
                    conn.commit()

                st.success("Test run complete.")
                st.rerun()

            # show latest results
            res = conn.execute(
                "SELECT e.created_at, q.question, e.model, e.pass, e.latency_ms, e.error "
                "FROM eval_results e JOIN eval_questions q ON e.qid=q.qid "
                "WHERE e.dataset_id=? ORDER BY e.created_at DESC LIMIT 50",
                (dataset_id,),
            ).fetchall()
            if res:
                df = pd.DataFrame([dict(r) for r in res])
                st.dataframe(df, use_container_width=True)



