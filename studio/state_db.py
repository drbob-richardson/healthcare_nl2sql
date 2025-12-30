# studio/state_db.py
import sqlite3
from pathlib import Path

DEFAULT_PATH = Path("storage/studio_state.sqlite")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS datasets (
  dataset_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  duckdb_path TEXT NOT NULL,
  dictionary_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_sessions (
  session_id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL,
  title TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(dataset_id) REFERENCES datasets(dataset_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES chat_sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS saved_views (
  view_id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL,
  name TEXT NOT NULL,
  sql TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(dataset_id) REFERENCES datasets(dataset_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS saved_plots (
  plot_id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL,
  name TEXT NOT NULL,
  sql TEXT NOT NULL,
  chart_type TEXT NOT NULL,
  x_col TEXT NOT NULL,
  y_col TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(dataset_id) REFERENCES datasets(dataset_id) ON DELETE CASCADE
);

-- -------------------------
-- Monitoring
-- -------------------------
CREATE TABLE IF NOT EXISTS llm_runs (
  run_id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL,
  session_id TEXT,
  page TEXT NOT NULL,
  model TEXT NOT NULL,
  top_k INTEGER,
  question TEXT NOT NULL,
  response_text TEXT,
  sql_json TEXT,
  latency_ms INTEGER,
  error TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(dataset_id) REFERENCES datasets(dataset_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS llm_feedback (
  feedback_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  rating INTEGER NOT NULL, -- +1, -1
  note TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES llm_runs(run_id) ON DELETE CASCADE
);

-- -------------------------
-- Golden questions / testing
-- -------------------------
CREATE TABLE IF NOT EXISTS eval_questions (
  qid TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL,
  question TEXT NOT NULL,
  expected_sql TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(dataset_id) REFERENCES datasets(dataset_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS eval_results (
  eval_id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL,
  qid TEXT NOT NULL,
  model TEXT NOT NULL,
  top_k INTEGER,
  got_sql TEXT,
  pass INTEGER NOT NULL,
  latency_ms INTEGER,
  error TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(dataset_id) REFERENCES datasets(dataset_id) ON DELETE CASCADE,
  FOREIGN KEY(qid) REFERENCES eval_questions(qid) ON DELETE CASCADE
);

-- Helpful indexes
CREATE INDEX IF NOT EXISTS idx_chat_sessions_dataset_created ON chat_sessions(dataset_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_saved_views_dataset_created ON saved_views(dataset_id, created_at);
CREATE INDEX IF NOT EXISTS idx_saved_plots_dataset_created ON saved_plots(dataset_id, created_at);

CREATE INDEX IF NOT EXISTS idx_llm_runs_dataset_created ON llm_runs(dataset_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_runs_dataset_error ON llm_runs(dataset_id, error);
CREATE INDEX IF NOT EXISTS idx_llm_feedback_run ON llm_feedback(run_id);

CREATE INDEX IF NOT EXISTS idx_eval_questions_dataset_created ON eval_questions(dataset_id, created_at);
CREATE INDEX IF NOT EXISTS idx_eval_results_dataset_created ON eval_results(dataset_id, created_at);
"""

def connect(db_path: Path = DEFAULT_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
