import psycopg2
import sys
from dotenv import load_dotenv
from openai import OpenAI
import os, json, re
from decimal import Decimal
from datetime import date, datetime

load_dotenv()

def _jsonable(x):
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    if isinstance(x, Decimal):
        return str(x)  # keep exact value
    return x

run_as_sql = "--sql" in sys.argv
emit_json = "--json" in sys.argv

mode = "rules"
if "--mode" in sys.argv:
    i = sys.argv.index("--mode")
    mode = sys.argv[i + 1]
if not emit_json:
    print("Mode:", mode)






def nl_to_sql_llm(question: str) -> tuple[str, str]:
    client = OpenAI()
    model = os.getenv("OPENAI_MODEL", "gpt-5-nano")

    schema = """
Tables:
patients(patient_id, full_name, birth_date, sex)
encounters(encounter_id, patient_id, encounter_date, department)
claims(claim_id, encounter_id, payer, amount, status)
""".strip()

    prompt = f"""
Return a JSON object with keys: sql, trace.
- sql: a single PostgreSQL SELECT query (CTEs allowed). No semicolons required.
- trace: one short sentence about what you did.
Return ONLY JSON. No markdown.

Schema:
{schema}

Question: {question}
""".strip()

    resp = client.responses.create(model=model, input=prompt)

    # Get text robustly across SDK variations
    text = getattr(resp, "output_text", None)
    if not text:
        # Fallback: try to reconstruct from response output structure
        try:
            parts = []
            for item in resp.output:
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", "") in ("output_text", "text"):
                        parts.append(getattr(c, "text", "") or "")
            text = "\n".join(parts).strip()
        except Exception:
            text = ""

    text = (text or "").strip()

    # Strip ```json ... ``` fences if present
    fence_match = re.match(r"(?s)^\s*```(?:json)?\s*(.*?)\s*```\s*$", text)
    if fence_match:
        text = fence_match.group(1).strip()

    # If there's extra chatter, extract the first JSON object-looking block
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1].strip()

    # Parse JSON
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM did not return valid JSON. Raw output:\n{text}") from e

    if not isinstance(obj, dict):
        raise ValueError(f"LLM JSON was not an object/dict. Got: {type(obj)} -> {obj}")

    # Accept a couple of common alternate keys defensively
    sql = (obj.get("sql") or obj.get("query") or obj.get("SQL") or "").strip()
    trace = (obj.get("trace") or obj.get("rationale") or obj.get("explanation") or "").strip()

    if not sql:
        raise ValueError(f"LLM JSON missing 'sql'. Full JSON:\n{obj}")

    # Normalize: remove a trailing semicolon, then forbid any remaining semicolons
    sql = re.sub(r"\s*;\s*$", "", sql)
    if ";" in sql:
        raise ValueError("Only one statement is allowed (no semicolons).")

    # Read-only safety: allow SELECT or WITH (CTE)
    if not re.match(r"(?is)^\s*(select|with)\b", sql):
        raise ValueError("Only SELECT/CTE queries are allowed.")

    banned = ["insert", "update", "delete", "drop", "alter", "create", "truncate", "grant", "revoke"]
    if any(re.search(rf"(?is)\b{kw}\b", sql) for kw in banned):
        raise ValueError("Query contains a disallowed keyword (write/DDL).")

    if not trace:
        trace = "Generated SQL from the provided schema and question."

    return (sql, f"LLM: {trace} (model={model})")





def nl_to_sql_rules(question: str) -> tuple[str, str]:
    q = question.lower()
    if "by payer" in q:
        return (
            "SELECT payer, SUM(amount) AS total_amount, COUNT(*) AS claim_count "
            "FROM claims GROUP BY payer ORDER BY total_amount DESC;",
            "Detected intent: aggregation by payer.",
        )
    if "denial" in q or "denied" in q:
        return (
            "SELECT status, COUNT(*) AS n FROM claims GROUP BY status ORDER BY n DESC;",
            "Detected intent: denial/status breakdown.",
        )
    if "by department" in q:
        return (
            "SELECT e.department, SUM(c.amount) AS total_amount, COUNT(*) AS claim_count "
            "FROM claims c "
            "JOIN encounters e ON e.encounter_id = c.encounter_id "
            "GROUP BY e.department "
            "ORDER BY total_amount DESC;",
            "Detected intent: aggregate claims by department (JOIN claims→encounters).",
        )
    if "status" in q:
        return (
            "SELECT status, COUNT(*) AS n FROM claims GROUP BY status ORDER BY n DESC;",
            "Detected intent: claim counts by status.",
        )
    return ("SELECT 'I do not know how to answer that yet' AS message;", "No rule matched.")

def nl_to_sql(question: str, mode: str) -> tuple[str, str]:
    if mode == "rules":
        return nl_to_sql_rules(question)
    if mode == "llm":
        return nl_to_sql_llm(question)
    return ("SELECT 'Unknown mode' AS message;", f"Invalid mode: {mode}")


args = sys.argv[1:]

clean = []
i = 0
while i < len(args):
    if args[i] == "--mode":
        i += 2  # skip flag + its value
        continue
    if args[i] == "--json":
        i += 1  # skip flag
        continue
    if args[i] == "--sql":
        i += 1
        continue
    clean.append(args[i])
    i += 1

question = " ".join(clean).strip() or "total claim amount by payer"
if not emit_json:
    print("Question:", question)




conn = psycopg2.connect(
    host="localhost", port=5432,
    dbname="nl2sql", user="nl2sql", password="nl2sql_password",
)

with conn, conn.cursor() as cur:
    try:
        if run_as_sql:
            sql = question
            trace = "Direct SQL execution (--sql)."
        else:
            sql, trace = nl_to_sql(question, mode)
        cur.execute(sql)
        rows = cur.fetchall()

        if emit_json:
            payload = {
                "mode": mode,
                "question": question,
                "trace": trace,
                "sql": sql,
                "rows": [[_jsonable(v) for v in row] for row in rows],
                "error": None,
            }
            print(json.dumps(payload))
        else:
            print("Trace:", trace)
            print("SQL:", sql)
            print(rows)

    except Exception as e:
        if emit_json:
            payload = {
                "mode": mode,
                "question": question,
                "trace": None,
                "sql": None,
                "rows": [],
                "error": f"{type(e).__name__}: {e}",
            }
            print(json.dumps(payload))
        else:
            print("ERROR:", type(e).__name__, str(e))

