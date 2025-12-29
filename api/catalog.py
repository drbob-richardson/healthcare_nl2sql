# api/catalog.py
def fetch_catalog(conn) -> dict:
    """Return {table_name: [col1, col2, ...]} for the public schema."""
    q = """
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
    ORDER BY table_name, ordinal_position;
    """
    out = {}
    with conn.cursor() as cur:
        cur.execute(q)
        for table, col in cur.fetchall():
            out.setdefault(table, []).append(col)
    return out

