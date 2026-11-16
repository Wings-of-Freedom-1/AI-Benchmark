"""Load: write validated rows into Supabase (PostgreSQL) or SQLite.

Backend selection is by ``db_path``:

* ``db_path=None`` and ``SUPABASE_DB_URL`` set → PostgreSQL (Supabase)
* ``db_path=<Path>`` → SQLite at that path (used for offline/demo runs)

Tables (identical in both backends)
-----------------------------------
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
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .clean import CleanResult
from .schemas import INTEGER_COLS, MODEL_COLUMNS, TEXT_COLS

try:
    import psycopg2
    _HAS_PG = True
    DB_ERRORS: tuple = (sqlite3.Error, psycopg2.Error)
except ImportError:  # sqlite-only installs still work
    psycopg2 = None  # type: ignore[assignment]
    _HAS_PG = False
    DB_ERRORS = (sqlite3.Error,)

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
        if col in INTEGER_COLS:
            sql_type = "INTEGER"
        elif col in TEXT_COLS:
            sql_type = "TEXT"
        else:
            sql_type = "DOUBLE PRECISION"  # matches SQLite's 8-byte REAL
        line = f"{col} {sql_type}"
        if col == "model_key":
            line += " PRIMARY KEY"
        elif col in ("name", "name_key"):
            line += " NOT NULL"
        elif col in _CHECKS:
            line += f" CHECK ({col} IS NULL OR {col} {_CHECKS[col]})"
        parts.append(line)
    return ",\n  ".join(parts)


_COMMON_DDL = f"""
CREATE TABLE IF NOT EXISTS models (
  {_column_ddl()}
);
CREATE INDEX IF NOT EXISTS idx_models_release ON models(release_date);
CREATE INDEX IF NOT EXISTS idx_models_creator ON models(creator);

CREATE TABLE IF NOT EXISTS model_snapshots (
  snapshot_date TEXT NOT NULL,
  model_key TEXT NOT NULL,
  intelligence_index DOUBLE PRECISION,
  price_blended_usd_per_m DOUBLE PRECISION,
  tokens_per_sec DOUBLE PRECISION,
  PRIMARY KEY (snapshot_date, model_key)
);

CREATE TABLE IF NOT EXISTS rejects (
  run_id INTEGER NOT NULL,
  model_key TEXT,
  name TEXT,
  errors TEXT
);
"""

_PG_RUNS = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
  run_id SERIAL PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  source TEXT NOT NULL,
  n_aa INTEGER, n_openrouter INTEGER, n_hf INTEGER,
  n_valid INTEGER, n_rejected INTEGER,
  stats_json TEXT
);
"""

_SQLITE_RUNS = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  source TEXT NOT NULL,
  n_aa INTEGER, n_openrouter INTEGER, n_hf INTEGER,
  n_valid INTEGER, n_rejected INTEGER,
  stats_json TEXT
);
"""

_VIEW = """
DROP VIEW IF EXISTS v_value;
CREATE VIEW v_value AS
SELECT name, creator, release_date, intelligence_index, price_blended_usd_per_m,
       tokens_per_sec, params_b,
       intelligence_index / NULLIF(price_blended_usd_per_m, 0) AS index_points_per_usd
FROM models
WHERE intelligence_index IS NOT NULL AND price_blended_usd_per_m > 0;
"""

SCHEMA_PG = _COMMON_DDL + _PG_RUNS + _VIEW
SCHEMA_SQLITE = _COMMON_DDL + _SQLITE_RUNS + _VIEW


def _supabase_url() -> str | None:
    return os.environ.get("SUPABASE_DB_URL")


def connect(db_path: Path | None):
    """Open a DB connection and ensure the schema exists.

    * ``db_path=None`` → Supabase via ``SUPABASE_DB_URL`` (raises if not configured).
    * ``db_path=<Path>`` → SQLite at that path (created if missing).
    """
    if db_path is None:
        url = _supabase_url()
        if not url:
            raise RuntimeError(
                "Supabase mode requested but SUPABASE_DB_URL is not set. "
                "Either set SUPABASE_DB_URL in .env, or pass an explicit db_path."
            )
        if not _HAS_PG:
            raise RuntimeError(
                "SUPABASE_DB_URL is set but psycopg2 is not installed. "
                "Install it with: pip install psycopg2-binary"
            )
        conn = psycopg2.connect(url)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_PG)
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        return conn

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQLITE)
    return conn


def _is_pg(conn) -> bool:
    return _HAS_PG and isinstance(conn, psycopg2.extensions.connection)


def _native(v: Any) -> Any:
    """Convert pandas/numpy scalars to native Python types both drivers accept."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            out = v.item()
            return None if isinstance(out, float) and pd.isna(out) else out
        except (ValueError, AttributeError):
            pass
    return v


def _py(df: pd.DataFrame) -> list[tuple]:
    """DataFrame -> tuples of plain Python values, NaN/NA/NaT -> None."""
    obj = df.astype(object).where(df.notna(), None)
    return [tuple(_native(v) for v in r) for r in obj.itertuples(index=False, name=None)]


def _to_pg_table(conn, df: pd.DataFrame, table: str) -> None:
    """Drop and recreate ``table`` in PostgreSQL from a DataFrame, then insert rows."""
    cur = conn.cursor()
    cur.execute(f'DROP TABLE IF EXISTS "{table}"')
    if len(df.columns) == 0:
        cur.execute(f'CREATE TABLE "{table}" (_empty TEXT)')
        return
    cols = []
    for col in df.columns:
        dtype = df[col].dtype
        if pd.api.types.is_integer_dtype(dtype):
            pg_type = "BIGINT"
        elif pd.api.types.is_float_dtype(dtype):
            pg_type = "DOUBLE PRECISION"
        elif pd.api.types.is_bool_dtype(dtype):
            pg_type = "BOOLEAN"
        elif pd.api.types.is_datetime64_any_dtype(dtype):
            pg_type = "TIMESTAMP WITH TIME ZONE"
        else:
            pg_type = "TEXT"
        cols.append(f'"{col}" {pg_type}')
    cur.execute(f'CREATE TABLE "{table}" ({", ".join(cols)})')
    if len(df) > 0:
        col_list = ", ".join(f'"{c}"' for c in df.columns)
        marks = ", ".join(["%s"] * len(df.columns))
        data = [tuple(_native(v) for v in row) for row in df.itertuples(index=False, name=None)]
        cur.executemany(f'INSERT INTO "{table}" ({col_list}) VALUES ({marks})', data)


def load_all(
    db_path: Path | None,
    result: CleanResult,
    valid: pd.DataFrame,
    rejects: pd.DataFrame,
    *,
    source: str,
    started_at: datetime,
) -> int:
    """Write everything in one transaction. Returns the run_id."""
    conn = connect(db_path)
    is_pg = _is_pg(conn)
    ph = "%s" if is_pg else "?"
    try:
        cur = conn.cursor() if is_pg else conn

        # 1) Replace the models snapshot
        cur.execute("DELETE FROM models")
        cols = ", ".join(MODEL_COLUMNS)
        marks = ", ".join(ph for _ in MODEL_COLUMNS)
        cur.executemany(
            f"INSERT INTO models ({cols}) VALUES ({marks})",
            _py(valid[MODEL_COLUMNS]),
        )

        # 2) Append a snapshot for today (upsert)
        today = date.today().isoformat()
        snap = valid[["model_key", "intelligence_index", "price_blended_usd_per_m", "tokens_per_sec"]].copy()
        snap.insert(0, "snapshot_date", today)
        snap_rows = _py(snap)
        if is_pg:
            cur.executemany(
                "INSERT INTO model_snapshots "
                "(snapshot_date, model_key, intelligence_index, price_blended_usd_per_m, tokens_per_sec) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (snapshot_date, model_key) DO UPDATE SET "
                "intelligence_index = EXCLUDED.intelligence_index, "
                "price_blended_usd_per_m = EXCLUDED.price_blended_usd_per_m, "
                "tokens_per_sec = EXCLUDED.tokens_per_sec",
                snap_rows,
            )
        else:
            cur.executemany(
                "INSERT OR REPLACE INTO model_snapshots VALUES (?, ?, ?, ?, ?)",
                snap_rows,
            )

        # 3) raw_* tables
        for name, frame in [
            ("raw_artificial_analysis", result.aa),
            ("raw_openrouter", result.openrouter),
            ("raw_huggingface", result.hf),
        ]:
            if is_pg:
                _to_pg_table(conn, frame, name)
            else:
                frame.to_sql(name, conn, if_exists="replace", index=False)

        # 4) pipeline_runs (RETURNING run_id for both drivers)
        params = (
            started_at.isoformat(timespec="seconds"),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source,
            len(result.aa), len(result.openrouter), len(result.hf),
            len(valid), len(rejects),
            json.dumps(result.stats, default=str),
        )
        if is_pg:
            cur.execute(
                "INSERT INTO pipeline_runs "
                "(started_at, finished_at, source, n_aa, n_openrouter, n_hf, n_valid, n_rejected, stats_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING run_id",
                params,
            )
            run_id = cur.fetchone()[0]
        else:
            cur.execute(
                "INSERT INTO pipeline_runs "
                "(started_at, finished_at, source, n_aa, n_openrouter, n_hf, n_valid, n_rejected, stats_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params,
            )
            run_id = cur.lastrowid

        # 5) rejects
        if len(rejects):
            cur.executemany(
                f"INSERT INTO rejects (run_id, model_key, name, errors) VALUES ({ph}, {ph}, {ph}, {ph})",
                [(run_id, *row) for row in _py(rejects[["model_key", "name", "errors"]])],
            )

        conn.commit()
        target = "Supabase" if is_pg else str(db_path)
        log.info("loaded %d models into %s (run %d)", len(valid), target, run_id)
        return run_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_models(db_path: Path | None) -> pd.DataFrame:
    conn = connect(db_path)
    is_pg = _is_pg(conn)
    try:
        if is_pg:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM models")
                cols = [d[0] for d in cur.description or []]
                rows = cur.fetchall()
            df = pd.DataFrame(rows, columns=cols)
        else:
            df = pd.read_sql_query("SELECT * FROM models", conn)
    finally:
        conn.close()
    df["release_date"] = pd.to_datetime(df["release_date"], errors="coerce")
    return df


def read_last_run(db_path: Path | None) -> dict:
    conn = connect(db_path)
    is_pg = _is_pg(conn)
    keys = ["run_id", "finished_at", "source", "n_aa", "n_openrouter", "n_hf",
            "n_valid", "n_rejected", "stats_json"]
    sql = ("SELECT run_id, finished_at, source, n_aa, n_openrouter, n_hf, n_valid, n_rejected, stats_json "
           "FROM pipeline_runs ORDER BY run_id DESC LIMIT 1")
    try:
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
        else:
            row = conn.execute(sql).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    out = dict(zip(keys, row))
    out["stats"] = json.loads(out.pop("stats_json") or "{}")
    return out
