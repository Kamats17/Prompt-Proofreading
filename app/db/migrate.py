"""Idempotent schema migration. Run before any DB access.

Safe on fresh DBs and on every older layout of this project's DB:
  - plain new columns are added with ALTER TABLE ADD COLUMN;
  - the drafts table is rebuilt (copy -> drop -> rename, data preserved) whenever
    its status CHECK constraint is older than the current one, because SQLite
    cannot ALTER a CHECK constraint in place;
  - indexes that depend on migrated columns are created here, after backfill.
"""
import re
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Columns added after v1.0 (plain columns only; ALTER ADD COLUMN is safe).
_PROSPECT_ADDED_COLUMNS = {
    "canonical_domain": "TEXT",
    "city": "TEXT",
    "email_source": "TEXT",
    "contact_name": "TEXT",
    "blocked_reason": "TEXT",
    "audit_attempts": "INTEGER NOT NULL DEFAULT 0",
    "last_audit_error": "TEXT",
    "last_audit_error_at": "TEXT",
    "last_contacted_at": "TEXT",
    "do_not_contact_until": "TEXT",
}
_DRAFT_ADDED_COLUMNS = {
    "evidence_json": "TEXT",
}
_AUDIT_ADDED_COLUMNS = {
    "node_rating_snapshot": "TEXT",
    "meta_json": "TEXT",
}

# Marker proving the drafts table has the current status CHECK. Bump this when
# the status list in schema.sql changes.
_DRAFTS_SCHEMA_MARKER = "'bounced'"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _add_missing_columns(conn: sqlite3.Connection, table: str, wanted: dict[str, str]) -> None:
    if not _table_exists(conn, table):
        return
    have = set(_columns(conn, table))
    for col, decl in wanted.items():
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _drafts_create_sql(schema_text: str) -> str:
    m = re.search(r"CREATE TABLE IF NOT EXISTS drafts \((.*?)\n\);", schema_text, re.S)
    if not m:
        raise RuntimeError("schema.sql: drafts table definition not found")
    return "CREATE TABLE drafts_new (" + m.group(1) + "\n);"


def _rebuild_drafts_if_stale(conn: sqlite3.Connection, schema_text: str) -> bool:
    """Rebuild drafts with the current definition, preserving every row.
    Returns True if a rebuild happened. Caller must have foreign_keys OFF."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='drafts'"
    ).fetchone()
    if not row:
        return False
    if _DRAFTS_SCHEMA_MARKER in (row[0] or ""):
        return False
    old_cols = _columns(conn, "drafts")
    conn.execute("DROP TABLE IF EXISTS drafts_new")
    conn.execute(_drafts_create_sql(schema_text))
    new_cols = _columns(conn, "drafts_new")
    shared = [c for c in old_cols if c in new_cols]
    col_list = ", ".join(shared)
    conn.execute(f"INSERT INTO drafts_new ({col_list}) SELECT {col_list} FROM drafts")
    conn.execute("DROP TABLE drafts")
    conn.execute("ALTER TABLE drafts_new RENAME TO drafts")
    return True


def _create_dependent_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prospects_domain ON prospects(canonical_domain)")
    # At most ONE live (non-rejected) draft per prospect per touch. This is the
    # database-level backstop for the "never email the same business twice"
    # rule: two concurrent generate clicks / CLI + dashboard races both pass the
    # application check, but only one insert can succeed.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_drafts_live_touch "
            "ON drafts(prospect_id, touch) WHERE status != 'rejected'"
        )
    except sqlite3.IntegrityError:
        # Pre-existing duplicates (from before this guard existed). Leave the DB
        # usable; `pp-agent status` reports them so a human can reject one.
        pass


def migrate(db_path: str | Path) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_text = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        # FK enforcement must be OFF while the drafts table is swapped out.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        _rebuild_drafts_if_stale(conn, schema_text)
        _add_missing_columns(conn, "prospects", _PROSPECT_ADDED_COLUMNS)
        _add_missing_columns(conn, "audits", _AUDIT_ADDED_COLUMNS)
        _add_missing_columns(conn, "drafts", _DRAFT_ADDED_COLUMNS)
        conn.execute("COMMIT")
        conn.executescript(schema_text)  # creates any missing tables + base indexes
        _create_dependent_indexes(conn)
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "data/pp_agent.db"
    migrate(target)
    print(f"Migrated: {target}")
