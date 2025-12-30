# Analytics Studio – BYO Data NL2SQL Copilot

A deployed-style analytics application that lets users upload their own datasets,
ask questions in natural language, generate **read-only SQL**, run queries safely,
create plots, and maintain a reproducible analysis history with monitoring.

This project focuses on **productized applied AI**, not just model calls.

**Key Capabilities**
- Upload CSV datasets (multi-table) + optional data dictionary
- Ask questions → generate **read-only SQL** (CTE/SELECT only)
- Execute queries against DuckDB and preview results
- Create, save, and re-run plots
- Conversational chat with dataset-aware memory
- Saved views for reproducible analysis
- **Monitoring & evaluation** (latency, errors, feedback, golden tests)
- Workspace isolation + full reset/delete controls (deployment-safe)

## Architecture

Streamlit UI
│
├── SQLite (State DB)
│   ├── datasets
│   ├── chat_sessions / chat_messages
│   ├── saved_views / saved_plots
│   ├── llm_runs / llm_feedback
│   └── eval_questions / eval_results
│
├── DuckDB (Per-Dataset)
│   └── Uploaded CSV tables
│
├── LLM Agent
│   ├── Schema + dictionary grounding
│   ├── SQL generation (read-only enforced)
│   └── Tool calls captured & logged
│
└── Safety & Ops
    ├── SQL validation (SELECT / CTE only)
    ├── LIMIT wrapping
    ├── Workspace isolation
    └── Monitoring & feedback

Streamlit UI
│
├── SQLite (State DB)
│   ├── datasets
│   ├── chat_sessions / chat_messages
│   ├── saved_views / saved_plots
│   ├── llm_runs / llm_feedback
│   └── eval_questions / eval_results
│
├── DuckDB (Per-Dataset)
│   └── Uploaded CSV tables
│
├── LLM Agent
│   ├── Schema + dictionary grounding
│   ├── SQL generation (read-only enforced)
│   └── Tool calls captured & logged
│
└── Safety & Ops
    ├── SQL validation (SELECT / CTE only)
    ├── LIMIT wrapping
    ├── Workspace isolation
    └── Monitoring & feedback

DuckDB is used strictly for analytics execution.
SQLite is used as an application state database for persistence, monitoring, and evaluation.
This separation allows safe execution, reproducibility, and deployment-style controls.

## Quick Start

```bash
git clone https://github.com/yourname/analytics-copilot-byo-data
cd analytics-copilot-byo-data
pip install -r requirements.txt
export OPENAI_API_KEY=your_key_here
streamlit run app.py
```

### Recommended Demo Flow (2 minutes)

1. Click **Load Demo Dataset**
2. Go to **Chat** → “What tables do I have?”
3. Ask → “Denial rate by department”
4. Switch to **Ask** → generate SQL → run → plot → save
5. Open **History** → re-run saved plot
6. Open **Monitor** → view latency, errors, feedback

## Monitoring & Evaluation

Every LLM interaction is logged locally:
- Prompt & response
- Generated SQL tool calls
- Latency & error tracking
- User feedback (👍 / 👎)
- Dataset & session context

The app includes a lightweight test harness:
- Golden questions with expected SQL
- Pass/fail comparison via result equivalence
- Regression testing across prompt/model changes

## Safety & Guardrails

- Only SELECT / WITH…SELECT SQL is allowed
- DDL / DML statements are blocked
- All queries are wrapped with row limits
- No direct database mutation is possible
- Workspace isolation prevents data leakage

