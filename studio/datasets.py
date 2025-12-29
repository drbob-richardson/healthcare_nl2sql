# studio/datasets.py
from pathlib import Path
from studio.duckdb_adapter import close_conn, list_tables, describe_table

import duckdb


def load_csvs_to_duckdb(duckdb_path: Path, table_to_csv: dict[str, Path]) -> None:
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)

    # Important: close any cached read-only connection before writing/loading
    close_conn(duckdb_path)

    con = duckdb.connect(str(duckdb_path), read_only=False, config={})
    try:
        for table, csv_path in table_to_csv.items():
            con.execute(f"DROP TABLE IF EXISTS {table}")
            con.execute(
                f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto('{csv_path.as_posix()}')"
            )
    finally:
        con.close()


# Re-export these for app/app.py convenience
def list_tables_safe(duckdb_path: Path) -> list[str]:
    return list_tables(duckdb_path)

def describe_table_safe(duckdb_path: Path, table: str) -> list[tuple]:
    return describe_table(duckdb_path, table)
