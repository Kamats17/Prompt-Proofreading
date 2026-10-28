"""Run the existing Node prospecting agent as a subprocess and ingest its XLSX output.

The Node agent lives at:
  paths.node_prospecting_dir in config.local.json

It writes Excel files to:
  <agent>/leads/Prospects-[location]-[date].xlsx

After the run completes, we ingest the most recent leads/*.xlsx with source='node_discovery'.
"""
import sqlite3
import subprocess
import time
from pathlib import Path

from app.settings import node_prospecting_dir
from app.sources.from_xlsx import ingest_xlsx


def _latest_leads_xlsx(prospecting_dir: Path) -> Path | None:
    leads_dir = prospecting_dir / "leads"
    if not leads_dir.exists():
        return None
    candidates = sorted((f for f in leads_dir.glob("*.xlsx") if not f.name.startswith("~$")),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def run_node_discovery(conn: sqlite3.Connection,
                       *, location: str, business_type: str = "restaurants",
                       limit: int = 50, timeout_s: int = 1800) -> dict:
    """Spawn `node prospect.js --location ... --type ... --limit ...`, then ingest output XLSX.

    Returns {'returncode', 'stdout_tail', 'imported', 'xlsx_path'}.
    """
    proj = node_prospecting_dir()
    entry = proj / "prospect.js"
    if not entry.exists():
        return {"error": f"prospect.js not found at {entry}"}
    started = time.time()
    cmd = ["node", str(entry),
           "--location", location,
           "--type", business_type,
           "--limit", str(limit)]
    try:
        proc = subprocess.run(cmd, cwd=str(proj), capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"error": "node discovery timed out"}
    out_tail = (proc.stdout or "")[-1000:]
    if proc.returncode != 0:
        return {"error": f"node returncode {proc.returncode}", "stdout_tail": out_tail,
                "stderr_tail": (proc.stderr or "")[-1000:]}
    xlsx = _latest_leads_xlsx(proj)
    # Only accept a file this run actually produced — never silently re-ingest a
    # stale leads file from a previous run (5s clock-skew margin).
    if not xlsx or xlsx.stat().st_mtime < started - 5:
        return {"error": "node run produced no new XLSX", "stdout_tail": out_tail}
    stats = ingest_xlsx(conn, xlsx, source="node_discovery")
    return {"returncode": 0, "stdout_tail": out_tail,
            "xlsx_path": str(xlsx), **stats}
