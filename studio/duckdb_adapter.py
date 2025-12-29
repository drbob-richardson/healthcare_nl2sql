# studio/duckdb_adapter.py
from __future__ import annotations

import duckdb
from pathlib import Path

# One connection per DuckDB file (per Streamlit process)
_CONNS: dict[str, duckdb.DuckDBPyConnection] = {}


def get_conn(db_path: str | Path, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    p = str(db_path)
    con = _CONNS.get(p)
    if con is None:
        con = duckdb.connect(p, read_only=read_only, config={})
        _CONNS[p] = con
    return con


def close_conn(db_path: str | Path) -> None:
    p = str(db_path)
    con = _CONNS.pop(p, None)
    if con is not None:
        try:
            con.close()
        except Exception:
            pass


def run_sql(db_path: str | Path, sql: str):
    con = get_conn(db_path, read_only=True)
    res = con.execute(sql)
    cols = [c[0] for c in res.description] if res.description else []
    rows = res.fetchall()
    return cols, rows


def list_tables(db_path: str | Path) -> list[str]:
    con = get_conn(db_path, read_only=True)
    rows = con.execute("SHOW TABLES").fetchall()
    return [r[0] for r in rows]


def describe_table(db_path: str | Path, table: str) -> list[tuple]:
    con = get_conn(db_path, read_only=True)
    return con.execute(f"DESCRIBE {table}").fetchall()


def schema_text(db_path: str | Path) -> str:
    tables = list_tables(db_path)
    lines = []
    for t in tables:
        desc = describe_table(db_path, t)
        cols = [r[0] for r in desc]
        lines.append(f"{t}({', '.join(cols)})")
    return "\n".join(lines)
