import json
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import time, os
import duckdb

from studio.state_db import connect, init_db
from studio.datasets import load_csvs_to_duckdb, list_tables, describe_table
from studio.dictionary import load_dictionary, to_json, pretty
from studio.agent_sql import run_agent_turn

from dotenv import load_dotenv
load_dotenv()

STORAGE = Path("storage")
UPLOADS = STORAGE / "uploads"
DUCKDB_DIR = STORAGE / "duckdb"

st.set_page_config(page_title="Analytics Studio (BYO Data)", layout="wide")
st.title("Analytics Studio (BYO Data)")

# --- State DB init ---
conn = connect()
init_db(conn)

def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def db_all_datasets():
    cur = conn.execute("SELECT * FROM datasets ORDER BY created_at DESC")
    return cur.fetchall()

def db_insert_dataset(dataset_id: str, name: str, duckdb_path: str, dictionary_json: str | None):
    conn.execute(
        "INSERT INTO datasets(dataset_id, name, duckdb_path, dictionary_json, created_at) VALUES (?,?,?,?,?)",
        (dataset_id, name, duckdb_path, dictionary_json, now_iso()),
    )
    conn.commit()

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

def db_get_messages(session_id: str, limit: int = 50):
    cur = conn.execute(
        "SELECT role, content FROM chat_messages WHERE session_id=? ORDER BY id ASC LIMIT ?",
        (session_id, limit),
    )
    return cur.fetchall()

def db_save_view(dataset_id: str, name: str, sql: str) -> None:
    view_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO saved_views(view_id, dataset_id, name, sql, created_at) VALUES (?,?,?,?,?)",
        (view_id, dataset_id, name, sql, now_iso()),
    )
    conn.commit()

@st.cache_resource
def get_duckdb_conn(db_path: str):
    # One shared connection per file path, consistent config
    return duckdb.connect(db_path, read_only=True, config={})

datasets = db_all_datasets()

# --- Sidebar: dataset picker ---
st.sidebar.header("Dataset")
if datasets:
    label_to_id = {f"{d['name']}  ({d['dataset_id'][:8]})": d["dataset_id"] for d in datasets}
    selected_label = st.sidebar.selectbox("Select", list(label_to_id.keys()))
    dataset_id = label_to_id[selected_label]
else:
    st.sidebar.info("No datasets yet. Add one in the Data tab.")
    dataset_id = None

def get_dataset(dataset_id: str):
    row = conn.execute("SELECT * FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
    return row

tab_data, tab_chat, tab_history, tab_eval = st.tabs(["Data", "Chat", "History", "Eval"])

# =========================
# DATA TAB: register datasets
# =========================
with tab_data:
    st.subheader("Register a dataset")

    st.caption("Upload one or more CSVs. Each CSV becomes a table (table name = file name).")
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

    if st.button("Create dataset"):
        if not csv_files:
            st.error("Upload at least one CSV.")
        else:
            dataset_id_new = str(uuid.uuid4())
            duckdb_path = DUCKDB_DIR / f"{dataset_id_new}.duckdb"
            UPLOADS.mkdir(parents=True, exist_ok=True)
            DUCKDB_DIR.mkdir(parents=True, exist_ok=True)

            # Save uploads to disk (so DuckDB can read them reliably)
            table_to_csv = {}
            for f in csv_files:
                table = Path(f.name).stem
                out_path = UPLOADS / f"{dataset_id_new}__{f.name}"
                out_path.write_bytes(f.getbuffer())
                table_to_csv[table] = out_path

            # Load into DuckDB
            load_csvs_to_duckdb(duckdb_path, table_to_csv)

            # Dictionary
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

            st.success("Dataset created. Use the sidebar to select it.")
            st.rerun()

    st.divider()
    st.subheader("Selected dataset details")

    if dataset_id:
        dset = get_dataset(dataset_id)
        st.write(f"**Name:** {dset['name']}")
        st.write(f"**ID:** `{dset['dataset_id']}`")
        st.write(f"**DuckDB:** `{dset['duckdb_path']}`")

        duckdb_path = Path(dset["duckdb_path"])
        con = get_duckdb_conn(str(duckdb_path))
        tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
        st.write(f"**Tables:** {len(tables)}")

        if tables:
            t = st.selectbox("Inspect table", tables)
            desc = con.execute(f"DESCRIBE {t}").fetchall()
            df_desc = pd.DataFrame(desc, columns=["column", "type", "null", "key", "default", "extra"][: len(desc[0])])
            st.dataframe(df_desc, use_container_width=True)

        if dset["dictionary_json"]:
            st.subheader("Data dictionary")
            st.code(pretty(json.loads(dset["dictionary_json"])), language="json")
    else:
        st.info("Create/select a dataset to see details.")

with tab_chat:
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
            st.rerun()

        # Render existing messages
        msgs = db_get_messages(session_id, limit=200)
        for m in msgs:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])

        user_text = st.chat_input("Ask about your dataset (tables, columns, metrics, etc.)")

        if user_text:
            # Persist user message
            db_add_message(session_id, "user", user_text)

            with st.chat_message("user"):
                st.markdown(user_text)

            with st.chat_message("assistant"):
                placeholder = st.empty()

                try:
                    # Re-fetch messages so history includes the new user message
                    msgs = db_get_messages(session_id, limit=200)
                    history = [(m["role"], m["content"]) for m in msgs]

                    assistant_text, sql_used = run_agent_turn(
                        duckdb_path=str(duckdb_path),
                        model=model,
                        user_text=user_text,
                        history=history,
                        top_k=int(top_k),
                    )

                    # Show assistant response
                    placeholder.markdown(assistant_text)

                    # Persist assistant response
                    db_add_message(session_id, "assistant", assistant_text)

                    # Save SQL into views/history
                    if sql_used:
                        for i, q in enumerate(sql_used[:5], start=1):
                            db_save_view(
                                dataset_id,
                                f"chat_query_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{i}",
                                q,
                            )

                        with st.expander("SQL used"):
                            for q in sql_used:
                                st.code(q, language="sql")

                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    placeholder.error(err)
                    db_add_message(session_id, "assistant", f"Error: {err}")

with tab_history:
    st.subheader("Analytics history")

    if not dataset_id:
        st.info("Select a dataset in the sidebar first.")
    else:
        rows = conn.execute(
            "SELECT name, sql, created_at FROM saved_views WHERE dataset_id=? ORDER BY created_at DESC LIMIT 50",
            (dataset_id,),
        ).fetchall()

        if not rows:
            st.info("No saved views yet. Ask questions in Chat to generate some.")
        else:
            for r in rows:
                with st.expander(f"{r['name']} — {r['created_at']}"):
                    st.code(r["sql"], language="sql")

# =========================
# EVAL TAB: placeholder for Course 3
# =========================
with tab_eval:
    st.subheader("Eval harness (coming in Course 3)")
    st.caption("Next: one-line QA tests, prompt variants, latency/token logging, leaderboard.")
    if not dataset_id:
        st.info("Select a dataset in the sidebar first.")

