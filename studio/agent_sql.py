# studio/agent_sql.py
from __future__ import annotations

import json
import re
from typing import List, Tuple

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool

from studio.duckdb_adapter import schema_text, run_sql


def _is_readonly(sql: str) -> bool:
    sql = sql.strip()
    if ";" in sql:
        return False
    if not re.match(r"(?is)^\s*(select|with)\b", sql):
        return False
    banned = ["insert", "update", "delete", "drop", "alter", "create", "truncate", "grant", "revoke", "attach", "detach"]
    return not any(re.search(rf"(?is)\b{kw}\b", sql) for kw in banned)


def _ensure_limit(sql: str, top_k: int) -> str:
    # If the model didn't include a LIMIT, wrap it safely
    if re.search(r"(?is)\blimit\s+\d+\b", sql):
        return sql
    return f"SELECT * FROM ({sql}) AS q LIMIT {int(top_k)}"


def run_agent_turn(
    duckdb_path: str,
    model: str,
    user_text: str,
    history: List[Tuple[str, str]],
    top_k: int = 50,
) -> tuple[str, list[str]]:
    """
    history: list of (role, content) from your sqlite chat log; role in {"user","assistant"}.
    returns: (assistant_text, sql_used)
    """

    @tool
    def get_schema() -> str:
        """Return available tables and columns in the current dataset."""
        return schema_text(duckdb_path)

    @tool
    def query_sql(sql: str) -> str:
        """
        Execute a read-only SQL query against the dataset and return JSON with columns and rows.
        Only SELECT/CTE queries allowed.
        """
        if not _is_readonly(sql):
            raise ValueError("Only single-statement read-only SELECT/CTE queries are allowed (no semicolons).")

        sql2 = _ensure_limit(sql, top_k)
        cols, rows = run_sql(duckdb_path, sql2)

        # Keep result payload compact
        payload = {"sql_executed": sql2, "columns": cols, "rows": rows[:top_k]}
        return json.dumps(payload, default=str)

    system = SystemMessage(
        content=(
            "You are a careful analytics assistant.\n"
            "You have two tools:\n"
            "- get_schema(): use it when asked about tables/columns/variables.\n"
            "- query_sql(sql): use it to answer data questions with SQL.\n\n"
            "Rules:\n"
            "- Prefer using tools rather than guessing.\n"
            "- Only read-only queries (SELECT / WITH ... SELECT).\n"
            f"- Keep results to <= {top_k} rows.\n"
            "- After using query_sql, explain what you found in plain English.\n"
        )
    )

    # Build message list
    messages = [system]
    for role, content in history[-30:]:
        if role == "user":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))

    messages.append(HumanMessage(content=user_text))

    llm = ChatOpenAI(model=model, temperature=0)
    llm_tools = llm.bind_tools([get_schema, query_sql])

    sql_used: list[str] = []
    # Tool-calling loop
    for _step in range(6):
        ai = llm_tools.invoke(messages)
        messages.append(ai)

        tool_calls = getattr(ai, "tool_calls", None) or []
        if not tool_calls:
            return (ai.content or "(No response text.)", sql_used)

        for tc in tool_calls:
            name = tc["name"]
            args = tc.get("args") or {}
            call_id = tc.get("id")  # needed for ToolMessage

            if name == "get_schema":
                out = get_schema.invoke({})
            elif name == "query_sql":
                out = query_sql.invoke(args)
                try:
                    obj = json.loads(out)
                    if "sql_executed" in obj:
                        sql_used.append(obj["sql_executed"])
                except Exception:
                    pass
            else:
                out = f"Unknown tool: {name}"

            messages.append(ToolMessage(content=str(out), tool_call_id=call_id))

    return ("I hit the tool-call step limit. Try simplifying the question.", sql_used)
