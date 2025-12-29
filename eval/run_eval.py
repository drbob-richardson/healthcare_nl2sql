import json
import subprocess
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "llm"

def run_json(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout or "").strip().splitlines()
    if not out:
        raise RuntimeError(f"No stdout. stderr:\n{r.stderr}")
    try:
        return json.loads(out[-1])
    except Exception as e:
        raise RuntimeError(f"Last line was not JSON.\nLast line:\n{out[-1]}\n\nFull stdout:\n{r.stdout}\n\nStderr:\n{r.stderr}") from e

def norm(rows):
    return sorted(tuple(r) for r in rows)

with open("eval/questions.jsonl") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue

        ex = json.loads(line)
        qid = ex["id"]
        q = ex["question"]
        expected_sql = ex["expected_sql"]

        exp = run_json([sys.executable, "api/db_smoketest.py", expected_sql, "--sql", "--json"])
        got = run_json([sys.executable, "api/db_smoketest.py", q, "--mode", MODE, "--json"])

        print(f"\n== {qid} ({MODE}) ==")

        if exp.get("error"):
            print("FAIL (expected_sql error):", exp["error"])
            print("expected_sql:", expected_sql)
            continue

        if got.get("error"):
            print("FAIL (model error):", got["error"])
            continue

        # Compare only the number of columns the model returned
        k = len(got["rows"][0]) if got["rows"] else 0
        exp_rows_k = [row[:k] for row in exp["rows"]]
        got_rows_k = [row[:k] for row in got["rows"]]

        ok = norm(exp_rows_k) == norm(got_rows_k)
        print("PASS" if ok else "FAIL")

        if not ok:
            print("Expected:", norm(exp_rows_k))
            print("Got     :", norm(got_rows_k))
            print("SQL:", got.get("sql"))
