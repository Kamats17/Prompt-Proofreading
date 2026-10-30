"""Idempotent schema migration. Run before any DB access.

Safe on fresh DBs and on every older layout of this project's DB:
  - plain new columns are added with ALTER TABLE ADD COLUMN;
  - the drafts and prospects tables are rebuilt (copy -> drop -> rename, data
    preserved) whenever their CHECK constraint is older than the current one,
    because SQLite cannot ALTER a CHECK constraint in place;
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

# Markers proving a table has its current CHECK constraint. Bump one when the
# matching list in schema.sql changes.
_DRAFTS_SCHEMA_MARKER = "'bounced'"     # drafts.status
_PROSPECTS_SCHEMA_MARKER = "'osm'"      # prospects.source


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


def _create_sql(schema_text: str, table: str) -> str:
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", schema_text, re.S)
    if not m:
        raise RuntimeError(f"schema.sql: {table} table definition not found")
    return f"CREATE TABLE {table}_new (" + m.group(1) + "\n);"


def _rebuild_if_stale(conn: sqlite3.Connection, schema_text: str, table: str, marker: str) -> bool:
    """Rebuild `table` with the current definition, preserving every row.
    Returns True if a rebuild happened. Caller must have foreign_keys OFF."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not row:
        return False
    if marker in (row[0] or ""):
        return False
    old_cols = _columns(conn, table)
    conn.execute(f"DROP TABLE IF EXISTS {table}_new")
    conn.execute(_create_sql(schema_text, table))
    new_cols = _columns(conn, f"{table}_new")
    shared = [c for c in old_cols if c in new_cols]
    col_list = ", ".join(shared)
    conn.execute(f"INSERT INTO {table}_new ({col_list}) SELECT {col_list} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
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
        _rebuild_if_stale(conn, schema_text, "drafts", _DRAFTS_SCHEMA_MARKER)
        # Columns first: the rebuild copies only the columns both versions share.
        _add_missing_columns(conn, "prospects", _PROSPECT_ADDED_COLUMNS)
        _rebuild_if_stale(conn, schema_text, "prospects", _PROSPECTS_SCHEMA_MARKER)
        _add_missing_columns(conn, "audits", _AUDIT_ADDED_COLUMNS)
        _add_missing_columns(conn, "drafts", _DRAFT_ADDED_COLUMNS)
        conn.execute("COMMIT")
        conn.executescript(schema_text)  # creates any missing tables + base indexes
        _create_dependent_indexes(conn)
        # Rows imported before email_source existed: label spreadsheet emails so the
        # review queue can show they weren't found on the business's own site.
        conn.execute("UPDATE prospects SET email_source = 'csv' WHERE email IS NOT NULL AND email != '' "
                     "AND email_source IS NULL AND source IN ('master_csv', 'xlsx', 'node_discovery')")
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
