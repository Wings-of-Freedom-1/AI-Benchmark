# ── SQL console ────────────────────────────────────────────────────────────
# Old code used sqlite3; now use the same PostgreSQL connection as the loader.
import psycopg2
from .config import SUPABASE_DB_URL

def _run_sql(query: str, limit: int = 1000) -> list[dict]:
    """Execute a read-only SELECT against Supabase (or SQLite in offline mode)."""
    if SUPABASE_DB_URL:
        conn = psycopg2.connect(SUPABASE_DB_URL)
        conn.set_session(readonly=True, autocommit=True)
    else:
        import sqlite3
        from .config import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA query_only = ON")

    try:
        with conn.cursor() as cur:
            cur.execute(query)
            if cur.description is None:
                return []
            cols = [desc[0] for desc in cur.description]
            rows = cur.fetchmany(limit)
            return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()
