import os
import sys
import sqlite3
from pathlib import Path
from datetime import datetime


def _resolve_data_dir() -> Path:
    """Where to keep the SQLite file.

    Frozen by PyInstaller → %APPDATA%\\FinanceSorter (Program Files is read-only
    for normal users, so we cannot put the DB next to the .exe). Dev mode keeps
    the existing ./data/ next to the source so nothing about run.bat changes.
    """
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "FinanceSorter"
    return Path(__file__).parent / "data"


DB_PATH = _resolve_data_dir() / "finance.db"

# Seed only the two structurally required topics. Everything else is added
# through the UI. Salary topic is the one undeletable bucket; one default
# category lives under it carrying is_primary_salary=1 so salary-period
# filtering has a row to anchor on out of the gate. The Savings topic is
# excluded from totals so transfers between own accounts don't pollute the
# income/expense tiles.
DEFAULT_TOPICS = [
    # (name, color, exclude_from_totals, is_fixed, is_salary_topic)
    ("Salaris", "#10b981", 0, 0, 1),
    ("Sparen",  "#14b8a6", 1, 0, 0),
]
DEFAULT_CATEGORIES = [
    # (topic_name, category_name, is_primary_salary)
    ("Salaris", "Salaris",             1),
    ("Sparen",  "Spaaroverschrijvingen", 0),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT,
  active_goal TEXT NOT NULL DEFAULT 'general',
  auto_generate_ai INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  color TEXT NOT NULL,
  exclude_from_totals INTEGER NOT NULL DEFAULT 0,
  is_fixed INTEGER NOT NULL DEFAULT 0,
  is_salary_topic INTEGER NOT NULL DEFAULT 0
);

-- Only one row can hold is_salary_topic=1 at a time.
CREATE UNIQUE INDEX IF NOT EXISTS ux_topics_one_salary
  ON topics(is_salary_topic) WHERE is_salary_topic = 1;

CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE RESTRICT,
  name TEXT NOT NULL,
  color TEXT,
  is_primary_salary INTEGER NOT NULL DEFAULT 0,
  UNIQUE(topic_id, name)
);

-- Exactly one category can be the primary-salary anchor across the whole DB.
CREATE UNIQUE INDEX IF NOT EXISTS ux_categories_one_primary_salary
  ON categories(is_primary_salary) WHERE is_primary_salary = 1;

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  name TEXT NOT NULL,
  account TEXT,
  counterparty TEXT,
  code TEXT,
  direction TEXT,
  amount REAL NOT NULL,
  transaction_type TEXT,
  notifications TEXT,
  resulting_balance REAL,
  tag TEXT,
  category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  categorization_source TEXT,
  suggestion_signal_source TEXT,
  suggestion_signal_key TEXT,
  source_file TEXT,
  imported_at TEXT,
  match_parent_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
  is_matched INTEGER NOT NULL DEFAULT 0,
  UNIQUE(date, name, amount, notifications)
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY,
  pattern TEXT NOT NULL,
  field TEXT NOT NULL DEFAULT 'name',
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  word_boundary INTEGER NOT NULL DEFAULT 0,
  amount_min REAL,
  amount_max REAL,
  hits INTEGER DEFAULT 0,
  created_at TEXT
);

-- Records every time the user disagrees with a stored auto/suggested category.
-- Read by the suggestion engine as negative evidence: 1 correction downgrades
-- AUTO→SUGGEST for the same (source, key, category); 2+ suppress entirely.
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY,
  tx_id INTEGER,
  suggested_category_id INTEGER,
  chosen_category_id INTEGER,
  signal_source TEXT,
  signal_key TEXT,
  created_at TEXT
);

-- Cached Gemini summaries keyed by (period_key, mode). period_key encodes the
-- range/mode and the anchor (e.g. "1m:2026-04", "3m:2026-04", "salary:2026-03-25");
-- mode is one of 'digest' / 'forecast' / 'advice'. For advice the period_key
-- carries a ":<goal_id>" suffix so each goal caches independently — that way
-- switching the active goal naturally bypasses any prior advice.
CREATE TABLE IF NOT EXISTS ai_summaries (
  id INTEGER PRIMARY KEY,
  period_key TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'digest',
  period_label TEXT,
  summary TEXT NOT NULL,
  model TEXT,
  created_at TEXT,
  UNIQUE(period_key, mode)
);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id);
CREATE INDEX IF NOT EXISTS idx_categories_topic ON categories(topic_id);
CREATE INDEX IF NOT EXISTS idx_corrections_signal
  ON corrections(signal_source, signal_key, suggested_category_id);
"""


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_exists(conn, table, column):
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def init_db():
    with get_connection() as conn:
        conn.executescript(SCHEMA)

        # Repair transactions whose date got stored as DD-MM-YYYY or DD/MM/YYYY
        # instead of ISO YYYY-MM-DD (early ASN imports).
        conn.execute(
            """UPDATE transactions
               SET date = substr(date, 7, 4) || '-' || substr(date, 4, 2) || '-' || substr(date, 1, 2)
               WHERE length(date) = 10
                 AND substr(date, 3, 1) IN ('-', '/')
                 AND substr(date, 6, 1) IN ('-', '/')"""
        )

        # Old auto-detector dismissals table is no longer used.
        conn.execute("DROP TABLE IF EXISTS subscription_dismissals")

        # Migration: ai_summaries gains a `mode` column and its UNIQUE constraint
        # moves from period_key alone to (period_key, mode). SQLite can't drop
        # the inline UNIQUE constraint in place, so we rebuild the table when
        # we detect the old shape. Existing rows are preserved as 'digest'.
        if not _column_exists(conn, "ai_summaries", "mode"):
            conn.executescript(
                """
                CREATE TABLE ai_summaries_new (
                  id INTEGER PRIMARY KEY,
                  period_key TEXT NOT NULL,
                  mode TEXT NOT NULL DEFAULT 'digest',
                  period_label TEXT,
                  summary TEXT NOT NULL,
                  model TEXT,
                  created_at TEXT,
                  UNIQUE(period_key, mode)
                );
                INSERT INTO ai_summaries_new
                  (period_key, mode, period_label, summary, model, created_at)
                  SELECT period_key, 'digest', period_label, summary, model, created_at
                    FROM ai_summaries;
                DROP TABLE ai_summaries;
                ALTER TABLE ai_summaries_new RENAME TO ai_summaries;
                """
            )

        # Migration: users gains `active_goal` (default 'general') and
        # `auto_generate_ai` (default 1). ALTER ADD COLUMN is safe here — no
        # unique constraint changes — so a rebuild isn't needed.
        if not _column_exists(conn, "users", "active_goal"):
            conn.execute(
                "ALTER TABLE users ADD COLUMN active_goal TEXT NOT NULL DEFAULT 'general'"
            )
        if not _column_exists(conn, "users", "auto_generate_ai"):
            conn.execute(
                "ALTER TABLE users ADD COLUMN auto_generate_ai INTEGER NOT NULL DEFAULT 1"
            )

        # Bootstrap seed topics + categories on first run.
        topic_count = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        if topic_count == 0:
            conn.executemany(
                """INSERT INTO topics
                     (name, color, exclude_from_totals, is_fixed, is_salary_topic)
                   VALUES (?, ?, ?, ?, ?)""",
                DEFAULT_TOPICS,
            )
            for topic_name, cat_name, is_primary in DEFAULT_CATEGORIES:
                conn.execute(
                    """INSERT INTO categories (topic_id, name, is_primary_salary)
                       SELECT id, ?, ? FROM topics WHERE name = ?""",
                    (cat_name, is_primary, topic_name),
                )

        # Safety net: if the salary topic exists but its primary-salary anchor
        # got deleted (e.g. user manually mucked with the DB), recreate one.
        salary_topic = conn.execute(
            "SELECT id FROM topics WHERE is_salary_topic = 1"
        ).fetchone()
        if salary_topic:
            has_primary = conn.execute(
                "SELECT 1 FROM categories WHERE is_primary_salary = 1"
            ).fetchone()
            if not has_primary:
                # Promote the first category in the salary topic, or create one.
                first = conn.execute(
                    "SELECT id FROM categories WHERE topic_id = ? ORDER BY id LIMIT 1",
                    (salary_topic["id"],),
                ).fetchone()
                if first:
                    conn.execute(
                        "UPDATE categories SET is_primary_salary = 1 WHERE id = ?",
                        (first["id"],),
                    )
                else:
                    conn.execute(
                        """INSERT INTO categories (topic_id, name, is_primary_salary)
                           VALUES (?, 'Salaris', 1)""",
                        (salary_topic["id"],),
                    )

        conn.commit()
