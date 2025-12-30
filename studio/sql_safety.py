# studio/sql_safety.py
from __future__ import annotations
import re

BANNED = [
    # write/ddl
    "insert", "update", "delete", "merge",
    "create", "alter", "drop", "truncate",
    # duckdb / db control-ish
    "attach", "detach", "copy", "export", "import",
    "load", "install", "pragma", "set",
    # permissions
    "grant", "revoke",
]

def validate_readonly_sql(sql: str) -> str:
    """Return cleaned SQL if safe; raise ValueError if not."""
    if not sql or not sql.strip():
        raise ValueError("SQL is empty.")

    s = sql.strip()

    # Allow user to end with semicolon, but only one statement total
    s = re.sub(r"\s*;\s*$", "", s)
    if ";" in s:
        raise ValueError("Only one statement allowed (no semicolons).")

    # Must start with SELECT or WITH
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        raise ValueError("Only SELECT / WITH…SELECT queries are allowed.")

    # Block common dangerous keywords anywhere
    for kw in BANNED:
        if re.search(rf"(?is)\b{kw}\b", s):
            raise ValueError(f"Disallowed keyword detected: {kw}")

    return s

def wrap_with_limit(sql: str, limit: int) -> str:
    """Wrap a safe query as a subquery with a LIMIT."""
    safe = validate_readonly_sql(sql)
    lim = int(limit)
    if lim <= 0:
        lim = 200
    return f"SELECT * FROM ({safe}) q LIMIT {lim}"

