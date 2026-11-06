"""Load: write validated rows into SQLite.

Tables
------
models              latest clean snapshot, one row per model (replaced each run)
model_snapshots     append-only history of the headline metrics, keyed by run date
raw_*               flattened source tables, for debugging and lineage
rejects             rows that failed validation, with reasons
pipeline_runs       one row per run (counts, source = live | sample)
v_value             convenience view: score per dollar
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .clean import CleanResult
from .schemas import INTEGER_COLS, MODEL_COLUMNS, TEXT_COLS

log = logging.getLogger(__name__)

_CHECKS = {
    "intelligence_index": "BETWEEN 0 AND 100",
    "price_blended_usd_per_m": ">= 0",
    "tokens_per_sec": ">= 0",
    "params_b": "> 0",
}


def _column_ddl() -> str:
    parts = []
    for col in MODEL_COLUMNS:
        sql_type = "INTEGER" if col in INTEGER_COLS else "TEXT" if col in TEXT_COLS else "REAL"
        line = f"{col} {sql_type}"
        if col == "model_key":
            line += " PRIMARY KEY"
        elif col in ("name", "name_key"):
            line += " NOT NULL"
        elif col in _CHECKS:
            line += f" CHECK ({col} IS NULL OR {col} {_CHECKS[col]})"
        parts.append(line)
    return ",\n  ".join(parts)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS models (
  {_column_ddl()}
);
CREATE INDEX IF NOT EXISTS idx_models_release ON models(release_date);
CREATE INDEX IF NOT EXISTS idx_models_creator ON models(creator);

CREATE TABLE IF NOT EXISTS model_snapshots (
  snapshot_date TEXT NOT NULL,
  model_key TEXT NOT NULL,
  intelligence_index REAL,
  price_blended_usd_per_m REAL,
  tokens_per_sec REAL,
  PRIMARY KEY (snapshot_date, model_key)
);

CREATE TABLE IF NOT EXISTS rejects (
  run_id INTEGER NOT NULL,
  model_key TEXT,
  name TEXT,
  errors TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  source TEXT NOT NULL,
  n_aa INTEGER, n_openrouter INTEGER, n_hf INTEGER,
  n_valid INTEGER, n_rejected INTEGER,
  stats_json TEXT
);

DROP VIEW IF EXISTS v_value;
CREATE VIEW v_value AS
SELECT name, creator, release_date, intelligence_index, price_blended_usd_per_m,
       tokens_per_sec, params_b,
       intelligence_index / NULLIF(price_blended_usd_per_m, 0) AS index_points_per_usd
FROM models
WHERE intelligence_index IS NOT NULL AND price_blended_usd_per_m > 0;
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def _py(df: pd.DataFrame) -> list[tuple]:
    """DataFrame -> tuples of plain Python values, NaN/NA -> None."""
    obj = df.astype(object).where(df.notna(), None)
    return [tuple(r) for r in obj.itertuples(index=False, name=None)]


def load_all(
    db_path: Path,
    result: CleanResult,
    valid: pd.DataFrame,
    rejects: pd.DataFrame,
    *,
    source: str,
    started_at: datetime,
) -> int:
    """Write everything in one transaction. Returns the run_id."""
    conn = connect(db_path)
    try:
        with conn:  # commits on success, rolls back on any error
            cols = ", ".join(MODEL_COLUMNS)
            marks = ", ".join("?" for _ in MODEL_COLUMNS)
            conn.execute("DELETE FROM models")
            conn.executemany(f"INSERT INTO models ({cols}) VALUES ({marks})", _py(valid[MODEL_COLUMNS]))

            today = date.today().isoformat()
            snap = valid[["model_key", "intelligence_index", "price_blended_usd_per_m", "tokens_per_sec"]].copy()
            snap.insert(0, "snapshot_date", today)
            conn.executemany(
                "INSERT OR REPLACE INTO model_snapshots VALUES (?, ?, ?, ?, ?)", _py(snap)
            )

            for name, frame in [
                ("raw_artificial_analysis", result.aa),
                ("raw_openrouter", result.openrouter),
                ("raw_huggingface", result.hf),
            ]:
                frame.to_sql(name, conn, if_exists="replace", index=False)

            run_id = conn.execute(
                """INSERT INTO pipeline_runs
                   (started_at, finished_at, source, n_aa, n_openrouter, n_hf, n_valid, n_rejected, stats_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    started_at.isoformat(timespec="seconds"),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    source,
                    len(result.aa), len(result.openrouter), len(result.hf),
                    len(valid), len(rejects),
                    json.dumps(result.stats, default=str),
                ),
            ).lastrowid
            if len(rejects):
                conn.executemany(
                    "INSERT INTO rejects (run_id, model_key, name, errors) VALUES (?, ?, ?, ?)",
                    [(run_id, *row) for row in _py(rejects[["model_key", "name", "errors"]])],
                )
        log.info("loaded %d models into %s (run %d)", len(valid), db_path, run_id)
        return run_id
    finally:
        conn.close()


def read_models(db_path: Path) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query("SELECT * FROM models", conn)
    finally:
        conn.close()
    df["release_date"] = pd.to_datetime(df["release_date"], errors="coerce")
    return df


def read_last_run(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT run_id, finished_at, source, n_aa, n_openrouter, n_hf, n_valid, n_rejected, stats_json "
            "FROM pipeline_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    keys = ["run_id", "finished_at", "source", "n_aa", "n_openrouter", "n_hf", "n_valid", "n_rejected", "stats_json"]
    out = dict(zip(keys, row))
    out["stats"] = json.loads(out.pop("stats_json") or "{}")
    return out

