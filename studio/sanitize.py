# studio/sanitize.py
import re

def safe_table_name(name: str) -> str:
    """
    Convert a filename stem into a safe DuckDB identifier:
    - lowercase
    - only letters, numbers, underscore
    - cannot start with a number
    """
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = "table"
    if s[0].isdigit():
        s = f"t_{s}"
    return s

