import streamlit as st
import pandas as pd
import psycopg2
from api.db_smoketest import nl_to_sql, _jsonable
import sqlparse
from api.catalog import fetch_catalog

st.set_page_config(page_title="Healthcare NL2SQL Studio", layout="wide")
st.title("Healthcare NL2SQL Analytics Studio")
tab_ask, tab_eval, tab_chat = st.tabs(["Ask", "Eval", "Chat"])
if "history" not in st.session_state:
    st.session_state.history = []

st.sidebar.header("History (last 10)")
for i, h in enumerate(st.session_state.history):
    if st.sidebar.button(f"{i+1}. {h['question']} ({h['mode']}, {h['rows']} rows)"):
        st.session_state["question_restore"] = h["question"]
        st.session_state["sql_restore"] = h["sql"]
if "messages" not in st.session_state:
    st.session_state.messages = []

with tab_ask:
    mode = st.radio("Mode", ["llm", "rules"], horizontal=True)
    if st.button("Refresh schema info"):
        conn = psycopg2.connect(host="localhost", port=5432, dbname="nl2sql", user="nl2sql", password="nl2sql_password")
        st.session_state["catalog"] = fetch_catalog(conn)

    catalog = st.session_state.get("catalog", {})
    st.sidebar.subheader("Schema")
    st.sidebar.write(f"{len(catalog)} tables")
    for t, cols in catalog.items():
        st.sidebar.write(f"- {t} ({len(cols)} cols)")

    question = st.text_input(
        "Ask a question (e.g., 'denial rate by department')",
        value=st.session_state.get("question_restore", "denial rate by department as a decimal between 0 and 1"),
    )
    if "sql_text" not in st.session_state:
        st.session_state.sql_text = ""
    
    st.session_state.setdefault("sql_text", "")
    sql_text = st.text_area("SQL (editable)", value=st.session_state.sql_text, height=220)
    st.session_state.sql_text = sql_text

    col1, col2 = st.columns(2)
    generate = col1.button("Generate SQL")
    run_sql = col2.button("Run SQL")
    auto_run = st.checkbox("Run immediately after generating", value=True)
    limit = st.number_input("Row limit", min_value=10, max_value=5000, value=500, step=10)

    st.divider()
    # A) Generate SQL: fill the editor + store trace
    if generate:
        try:
            sql, trace = nl_to_sql(question, mode)
            st.session_state["last_trace"] = trace
            st.session_state.sql_text = sqlparse.format(sql, reindent=True, keyword_case="upper")
            st.success("SQL generated. You can edit it, then click Run SQL.")
        except Exception as e:
            st.error(f"{type(e).__name__}: {e}")
        if auto_run:
            st.session_state["run_after_generate"] = True
            st.rerun()


    # B) Run SQL: execute whatever is currently in the editor
    if run_sql or st.session_state.pop("run_after_generate", False):
        conn = psycopg2.connect(
            host="localhost", port=5432,
            dbname="nl2sql", user="nl2sql", password="nl2sql_password",
        )

        with conn, conn.cursor() as cur:
            try:
                trace = st.session_state.get("last_trace", "(No trace yet — click Generate SQL first.)")

                sql_inner = (st.session_state.sql_text or "").strip().rstrip(";")
                if not sql_inner:
                    st.error("SQL editor is empty. Click **Generate SQL** first (or paste SQL into the editor).")
                    st.stop()

                sql_to_run = f"SELECT * FROM ({sql_inner}) q LIMIT {int(limit)}"

                cur.execute(sql_to_run)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []

                st.caption("Tip: select the SQL text and copy (⌘C).")

                st.subheader("Trace")
                st.write(trace)

                st.subheader("SQL (executed)")
                st.code(st.session_state.sql_text, language="sql")

                st.subheader("Results")
                data = [[_jsonable(v) for v in row] for row in rows]
                df = pd.DataFrame(data, columns=cols)
                st.dataframe(df, use_container_width=True)
                csv = df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "Download results as CSV",
                    data=csv,
                    file_name="query_results.csv",
                    mime="text/csv",
                )
                if df.shape[1] >= 2 and pd.api.types.is_numeric_dtype(df.iloc[:, 1]):
                    st.subheader("Quick chart")
                    chart_df = df.iloc[:, :2].copy()
                    chart_df.columns = ["category", "value"]
                    st.bar_chart(chart_df.set_index("category"))
                st.session_state.history.insert(0, {
                    "question": question,
                    "mode": mode,
                    "sql": st.session_state.sql_text,
                    "rows": len(df),
                })
                st.session_state.history = st.session_state.history[:10]

            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")


with tab_eval:
    import json

    st.subheader("Evaluation")
    st.caption("Runs expected SQL vs model-generated SQL and compares result rows (order-insensitive).")

    eval_mode = st.radio("Evaluate mode", ["llm", "rules"], horizontal=True, key="eval_mode")
    run_eval = st.button("Run eval", key="run_eval")

    def norm(rows):
        def canon(v):
            # normalize numeric-ish strings
            if isinstance(v, str) and v.replace('.', '', 1).isdigit():
                return round(float(v), 6)
            return v
        return sorted(tuple(canon(x) for x in r) for r in rows)

    if run_eval:
        conn = psycopg2.connect(
            host="localhost", port=5432,
            dbname="nl2sql", user="nl2sql", password="nl2sql_password",
        )

        results = []
        with open("eval/questions.jsonl") as f, conn, conn.cursor() as cur:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ex = json.loads(line)
                qid = ex["id"]
                q = ex["question"]
                expected_sql = ex["expected_sql"]

                # expected
                exp_err = got_err = None
                exp_rows = got_rows = []
                got_sql = ""

                try:
                    cur.execute(expected_sql)
                    exp_rows = [[_jsonable(v) for v in row] for row in cur.fetchall()]
                except Exception as e:
                    exp_err = f"{type(e).__name__}: {e}"

                # model
                try:
                    got_sql, _trace = nl_to_sql(q, eval_mode)
                    cur.execute(got_sql)
                    got_rows = [[_jsonable(v) for v in row] for row in cur.fetchall()]
                except Exception as e:
                    got_err = f"{type(e).__name__}: {e}"

                # score
                if exp_err or got_err:
                    passed = False
                else:
                    k = len(got_rows[0]) if got_rows else 0
                    exp_k = [row[:k] for row in exp_rows]
                    got_k = [row[:k] for row in got_rows]
                    passed = norm(exp_k) == norm(got_k)

                results.append({
                    "id": qid,
                    "question": q,
                    "pass": passed,
                    "error": exp_err or got_err or "",
                    "sql": got_sql,
                })

        df = pd.DataFrame(results)
        st.dataframe(df[["id", "question", "pass", "error"]], use_container_width=True)

        fails = df[~df["pass"]]
        if len(fails):
            st.subheader("Failures (details)")
            for _, row in fails.iterrows():
                st.write(f"**{row['id']}** — {row['question']}")
                if row["error"]:
                    st.error(row["error"])
                if row["sql"]:
                    st.code(row["sql"], language="sql")

with tab_chat:
    st.subheader("Chat")

    # make sure we have a catalog
    catalog = st.session_state.get("catalog", {})
    if not catalog:
        st.info("Click **Refresh schema info** in the Ask tab first so I can see your tables/columns.")

    # render history
    for m in st.session_state.messages:
        st.chat_message(m["role"]).write(m["content"])

    user = st.chat_input("Ask about the schema (e.g., 'how many tables?' or 'columns in claims')")
    if user:
        st.session_state.messages.append({"role": "user", "content": user})

        u = user.lower().strip()
        reply = None

        if "how many table" in u:
            reply = f"You have **{len(catalog)}** tables: {', '.join(sorted(catalog.keys()))}."
        elif "what table" in u or "list table" in u:
            reply = "Tables: " + ", ".join(sorted(catalog.keys()))
        elif "column" in u and " in " in u:
            # super simple parse: "columns in claims"
            t = u.split(" in ", 1)[1].strip().split()[0]
            if t in catalog:
                reply = f"Columns in **{t}**: " + ", ".join(catalog[t])
            else:
                reply = f"I don't see a table named `{t}`. I see: " + ", ".join(sorted(catalog.keys()))
        else:
            reply = "I can answer schema questions here for now. For data questions, use the **Ask** tab (we’ll connect chat→SQL next)."

        st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

