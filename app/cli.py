"""Single CLI entrypoint for the sales agent (`pp-agent`).

Daily use:
  daily [--audit N] [--draft N]   sync Gmail -> follow-ups -> audit -> draft -> summary
  serve                           dashboard at http://127.0.0.1:8765 (review queue: /queue)

Pipeline steps (what `daily` runs):
  sync                            mark sent/replied/bounced/opted-out from Gmail (read-only)
  followups [--limit N]           draft touch 2/3 replies for unanswered threads
  audit [<id>] [--limit N] [--allow-no-email] [--force-fetch]
  draft [<id>] [--grade A|B] [--limit N] [--name NAME] [--allow-no-email]

Leads:
  ingest manual <urls...> | master-csv | xlsx <path> | xlsx-dir [<dir>] | node-discovery ...
  call-list [--path CSV]          phone list of businesses with NO website (manual calls)
  mockup [<id>] [--limit N]       before/after phone mockup (default: everyone who replied)
  dnc add --domain/--email/--company [--reason] | dnc list

Setup & reports:
  init | doctor | status | stats | export <draft_id> [-o file.eml]
  gmail-auth [--read] [--force]   compose token (drafts) / read-only token (sync)
"""
import argparse
import sys
from pathlib import Path

from app.db.migrate import migrate, connect
from app.db.models import DncEntry
from app.db.repo import add_dnc, list_dnc
from app.settings import (
    DB_PATH, existing_prospect_csv, existing_prospects_xlsx_dir, EXPORTS_DIR,
    DASHBOARD_HOST, DASHBOARD_PORT, DAILY_NEW_CAP, MODEL_AUDIT,
)

MAX_AUDIT_ATTEMPTS = 3
RETRY_COOLDOWN = "-24 hours"


def _ensure_db():
    migrate(DB_PATH)
    return connect(DB_PATH)


def _out(msg: str = "") -> None:
    """Print that never crashes on a cp1252 console or a redirected log file."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


def cmd_init(args):
    migrate(DB_PATH)
    _out(f"DB ready at {DB_PATH}")


def cmd_ingest(args):
    conn = _ensure_db()
    sub = args.ingest_sub
    if sub == "manual":
        from app.sources.from_manual import ingest_manual
        ids = ingest_manual(conn, "\n".join(args.urls), company=args.company,
                            industry=args.industry, state=args.state)
        _out(f"ingested {len(ids)} prospect(s): {ids}")
    elif sub == "master-csv":
        from app.sources.from_master_csv import ingest_master_csv
        path = Path(args.path) if args.path else existing_prospect_csv()
        if not path.exists():
            _out(f"missing CSV: {path}")
            sys.exit(2)
        _out(str(ingest_master_csv(conn, path, limit=args.limit)))
    elif sub == "xlsx":
        from app.sources.from_xlsx import ingest_xlsx
        _out(str(ingest_xlsx(conn, Path(args.path), limit=args.limit)))
    elif sub == "xlsx-dir":
        from app.sources.from_xlsx import ingest_xlsx_dir
        directory = Path(args.directory) if args.directory else existing_prospects_xlsx_dir()
        _out(str(ingest_xlsx_dir(conn, directory, limit_per_file=args.limit)))
    elif sub == "node-discovery":
        from app.sources.from_node_discovery import run_node_discovery
        _out(str(run_node_discovery(conn, location=args.location,
                                    business_type=args.type, limit=args.limit)))


# ---- audit / draft (bulk-safe) ---------------------------------------------------

def run_bulk_audit(conn, *, limit: int, allow_no_email: bool = False,
                   force_fetch: bool = False, quiet: bool = False) -> dict:
    """Audit the next `limit` eligible prospects. Never aborts on one bad lead;
    stops immediately on a Claude login failure."""
    from app.audit.pipeline import audit_prospect
    rows = conn.execute(
        f"""SELECT p.id, p.company FROM prospects p
            WHERE NOT EXISTS (SELECT 1 FROM audits a WHERE a.prospect_id = p.id)
              AND p.blocked_reason IS NULL
              AND (p.node_rating IN ('A', 'B') OR p.node_rating IS NULL)
              AND p.audit_attempts < {MAX_AUDIT_ATTEMPTS}
              AND (p.last_audit_error_at IS NULL
                   OR p.last_audit_error_at < datetime('now', '{RETRY_COOLDOWN}'))
            ORDER BY (CASE WHEN p.email IS NOT NULL AND p.email != '' THEN 0
                           WHEN p.contact_name IS NOT NULL AND p.contact_name != '' THEN 1
                           ELSE 2 END), p.audit_attempts, p.id
            LIMIT ?""", (limit,)).fetchall()
    stats = {"selected": len(rows), "audited": 0, "blocked": 0, "failed": 0, "grades": {}}
    if not quiet:
        _out(f"auditing {len(rows)} prospect(s)...")
    for r in rows:
        try:
            res = audit_prospect(conn, r["id"], force_fetch=force_fetch, allow_no_email=allow_no_email)
        except Exception as e:  # one bad lead never kills the batch
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            try:
                from app.db.repo import record_audit_failure
                record_audit_failure(conn, r["id"], res["error"])
            except Exception:
                pass
        if res.get("ok"):
            stats["audited"] += 1
            stats["grades"][res["final_grade"]] = stats["grades"].get(res["final_grade"], 0) + 1
            outcome = f"grade {res['final_grade']} (score {res['icp_score']})"
        elif res.get("blocked"):
            stats["blocked"] += 1
            outcome = f"blocked: {res['blocked']} {res.get('reason') or ''}"
        else:
            stats["failed"] += 1
            outcome = f"error: {res.get('error')}"
        if not quiet:
            _out(f"  #{r['id']} {r['company']} -> {outcome}"[:220])
        if res.get("error_kind") == "auth":
            stats["aborted"] = "Claude login expired - run `claude login` in a terminal"
            _out(f"STOPPED: {stats['aborted']}")
            break
    return stats


def run_bulk_draft(conn, *, grades: tuple[str, ...], limit: int, allow_no_email: bool = False,
                   quiet: bool = False) -> dict:
    from app.draft.pipeline import draft_for_prospect
    ph = ",".join("?" for _ in grades)
    rows = conn.execute(
        f"""SELECT p.id, p.company FROM prospects p
            JOIN audits a ON a.id = (SELECT id FROM audits WHERE prospect_id = p.id
                                     ORDER BY id DESC LIMIT 1)
            WHERE a.final_grade IN ({ph}) AND p.blocked_reason IS NULL
              AND NOT EXISTS (SELECT 1 FROM drafts d
                              WHERE d.prospect_id = p.id AND d.status != 'rejected')
              AND ((p.email IS NOT NULL AND p.email != '') OR ?)
            ORDER BY a.icp_score DESC, p.id ASC LIMIT ?""",
        (*grades, 1 if allow_no_email else 0, limit)).fetchall()
    stats = {"selected": len(rows), "drafted": 0, "qa_pass": 0, "skipped": 0, "failed": 0}
    if not quiet:
        _out(f"drafting {len(rows)} prospect(s)...")
    for r in rows:
        try:
            res = draft_for_prospect(conn, r["id"], allow_no_email=allow_no_email)
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        if res.get("ok"):
            stats["drafted"] += 1
            stats["qa_pass"] += 1 if res.get("qa_pass") else 0
            outcome = "OK (QA pass)" if res.get("qa_pass") else f"QA blocked: {res.get('blockers')}"
        elif res.get("skipped"):
            stats["skipped"] += 1
            outcome = f"skipped: {res['skipped']}"
        else:
            stats["failed"] += 1
            outcome = f"error: {res.get('error')}"
        if not quiet:
            _out(f"  #{r['id']} {r['company']} -> {outcome}"[:220])
        if res.get("error_kind") == "auth":
            stats["aborted"] = "Claude login expired - run `claude login` in a terminal"
            _out(f"STOPPED: {stats['aborted']}")
            break
    return stats


def cmd_audit(args):
    conn = _ensure_db()
    if args.prospect_id:
        from app.audit.pipeline import audit_prospect
        _out(str(audit_prospect(conn, args.prospect_id, force_fetch=args.force_fetch,
                                allow_no_email=args.allow_no_email)))
        return
    _out(str(run_bulk_audit(conn, limit=args.limit, allow_no_email=args.allow_no_email,
                            force_fetch=args.force_fetch)))


def cmd_draft(args):
    conn = _ensure_db()
    if args.prospect_id:
        from app.draft.pipeline import draft_for_prospect
        _out(str(draft_for_prospect(conn, args.prospect_id, contact_name=args.name,
                                    allow_no_email=args.allow_no_email)))
        return
    grades = ("A", "B") if args.grade == "AB" else (args.grade,)
    _out(str(run_bulk_draft(conn, grades=grades, limit=args.limit,
                            allow_no_email=args.allow_no_email)))


# ---- Gmail sync / follow-ups / daily ------------------------------------------------

def _sync(conn) -> dict:
    from app.draft.gmail_sync import sync_gmail
    from app.auth.gmail_oauth import GmailAuthError
    try:
        return sync_gmail(conn)
    except GmailAuthError as e:
        return {"skipped": str(e)}


def build_pending_mockups(conn, *, limit: int = 20, quiet: bool = False) -> dict:
    """Mockups for prospects who replied (deterministic, no Claude call)."""
    from app.mockup.builder import build_mockup, mockup_paths
    rows = conn.execute(
        """SELECT DISTINCT p.id, p.company FROM prospects p JOIN drafts d ON d.prospect_id = p.id
           WHERE d.status = 'replied' AND p.blocked_reason IS NULL ORDER BY p.id LIMIT ?""",
        (limit * 3,)).fetchall()
    stats = {"built": 0, "failed": 0, "already": 0}
    for r in rows:
        if stats["built"] >= limit:
            break
        if mockup_paths(r["id"])["png"].exists():
            stats["already"] += 1
            continue
        res = build_mockup(conn, r["id"])
        stats["built" if res.get("ok") else "failed"] += 1
        if not quiet:
            _out(f"  #{r['id']} {r['company']} -> {res.get('png_path') or res.get('error')}")
    return stats


def cmd_mockup(args):
    conn = _ensure_db()
    if args.prospect_id:
        from app.mockup.builder import build_mockup
        res = build_mockup(conn, args.prospect_id, render=not args.no_render)
        _out(f"{'OK' if res.get('ok') else 'FAILED'}: {res.get('png_path') or res.get('html_path') or res.get('error')}")
        for c in res.get("changes", []):
            _out(f"  - {c['title']}")
        return
    _out(str(build_pending_mockups(conn, limit=args.limit)))


def cmd_sync(args):
    _out(str(_sync(_ensure_db())))


def cmd_followups(args):
    from app.draft.followups import create_followups
    _out(str(create_followups(_ensure_db(), limit=args.limit)))


def cmd_daily(args):
    """The whole routine in one command. Each stage reports; only a Claude login
    failure stops the run (nothing after it could succeed)."""
    from app.draft.followups import create_followups
    from app.draft.voice import compliance_warnings
    conn = _ensure_db()
    _out("== 1/4 Gmail sync (sent / replies / opt-outs / bounces)")
    s = _sync(conn)
    _out(f"   {s}")
    _out("== 2/4 Follow-ups")
    f = create_followups(conn, limit=args.followups)
    _out(f"   {f}")
    _out(f"== 3/4 Audit up to {args.audit} new leads")
    a = run_bulk_audit(conn, limit=args.audit, quiet=not args.verbose)
    _out(f"   {a}")
    m = build_pending_mockups(conn, quiet=True)
    d = {"skipped": "audit stopped"}
    if not a.get("aborted"):
        _out(f"== 4/4 Draft up to {args.draft} A/B leads")
        d = run_bulk_draft(conn, grades=("A", "B"), limit=args.draft, quiet=not args.verbose)
        _out(f"   {d}")
    pending = conn.execute("SELECT COUNT(*) FROM drafts WHERE status='pending' AND qa_pass=1").fetchone()[0]
    blocked = conn.execute("SELECT COUNT(*) FROM drafts WHERE status='pending' AND qa_pass=0").fetchone()[0]
    _out("")
    _out(f"READY TO REVIEW: {pending} draft(s) passing QA, {blocked} needing edits.")
    _out(f"Replies to handle: {s.get('replied', 0)} · opt-outs recorded: {s.get('opted_out', 0)} "
         f"· bounces: {s.get('bounced', 0)} · mockups built for replies: {m.get('built', 0)}")
    for w in compliance_warnings():
        _out(f"COMPLIANCE: {w}")
    _out(f"Open http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/queue  (run `pp-agent serve` if it isn't running)")


# ---- reports --------------------------------------------------------------------

def cmd_status(args):
    from app.db.repo import first_touch_pushes_today
    from app.draft.voice import compliance_warnings
    conn = _ensure_db()
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    _out(f"DB: {DB_PATH}")
    _out(f"Prospects total: {q('SELECT COUNT(*) FROM prospects')[0][0]}")
    _out("\nBy source:")
    for row in q("SELECT source, COUNT(*) FROM prospects GROUP BY source"):
        _out(f"  {row[0]:<16} {row[1]}")
    same_site = q("SELECT canonical_domain, COUNT(*) FROM prospects WHERE canonical_domain IS NOT NULL "
                  "GROUP BY canonical_domain HAVING COUNT(*) > 1")
    if same_site:
        _out(f"  !! {len(same_site)} site(s) with more than one prospect - check they're really "
             f"different businesses: {[r[0] for r in same_site[:5]]}")
    _out("\nBlocked prospects:")
    for row in q("SELECT blocked_reason, COUNT(*) FROM prospects WHERE blocked_reason IS NOT NULL "
                 "GROUP BY blocked_reason ORDER BY 2 DESC"):
        _out(f"  {row[0]:<22} {row[1]}")
    failing = q(f"SELECT COUNT(*) FROM prospects WHERE blocked_reason IS NULL AND audit_attempts > 0 "
                f"AND NOT EXISTS (SELECT 1 FROM audits a WHERE a.prospect_id = prospects.id)")[0][0]
    _out(f"  (failing audits, will retry: {failing})")
    _out("\nAudits by grade:")
    for row in q("SELECT final_grade, COUNT(*) FROM audits GROUP BY final_grade"):
        _out(f"  {row[0] or '(none)':<10} {row[1]}")
    _out("\nDrafts by status (touch):")
    for row in q("SELECT status, touch, COUNT(*), SUM(qa_pass) FROM drafts GROUP BY status, touch "
                 "ORDER BY touch, status"):
        _out(f"  {row[0]:<10} t{row[1]}  {row[2]} (qa_pass={row[3] or 0})")
    dups = q("SELECT prospect_id, touch, COUNT(*) FROM drafts WHERE status != 'rejected' "
             "GROUP BY prospect_id, touch HAVING COUNT(*) > 1")
    if dups:
        _out(f"\n!! {len(dups)} prospect(s) with duplicate live drafts - reject the extras: {dups[:5]}")
    with_email = q("SELECT COUNT(*) FROM prospects WHERE email IS NOT NULL AND email != ''")[0][0]
    harvested = q("SELECT COUNT(*) FROM prospects WHERE email_source LIKE 'harvested%'")[0][0]
    _out(f"\nProspects with email: {with_email} (found on their sites: {harvested})")
    _out(f"New emails pushed today: {first_touch_pushes_today(conn)} / {DAILY_NEW_CAP}")
    _out(f"DNC entries: {q('SELECT COUNT(*) FROM dnc_list')[0][0]}")
    for w in compliance_warnings():
        _out(f"COMPLIANCE: {w}")
    _out("\nRecent events (last 10):")
    for row in q("SELECT created_at, event_type, prospect_id FROM events ORDER BY id DESC LIMIT 10"):
        _out(f"  {row[0]}  {row[1]:<26} prospect={row[2]}")


def cmd_stats(args):
    """Outcome rates by industry, finding type, subject, and touch. At ~20 sends a
    day, read these for large differences only; small ones are noise for months."""
    conn = _ensure_db()
    base = """SELECT {dim} AS k,
                     SUM(d.status IN ('sent','replied','opted_out','bounced')) AS sent,
                     SUM(d.status = 'replied') AS replied,
                     SUM(d.status = 'opted_out') AS opted_out,
                     SUM(d.status = 'bounced') AS bounced
              FROM drafts d JOIN prospects p ON p.id = d.prospect_id
              WHERE d.status IN ('pushed','sent','replied','opted_out','bounced')
              GROUP BY k ORDER BY sent DESC"""
    for label, dim in (("Industry", "COALESCE(p.industry,'?')"),
                       ("Finding type", "COALESCE(d.finding_type,'?')"),
                       ("Subject", "COALESCE(d.subject_variant,'?')"),
                       ("Touch", "d.touch")):
        _out(f"\n{label}:")
        rows = conn.execute(base.format(dim=dim)).fetchall()
        if not rows:
            _out("  (no sent emails yet)")
        for r in rows:
            sent = r["sent"] or 0
            rate = f"{(r['replied'] or 0) / sent:.0%}" if sent else "-"
            _out(f"  {str(r['k'])[:40]:<40} sent {sent:>4}  replied {r['replied'] or 0:>3} ({rate})"
                 f"  opted-out {r['opted_out'] or 0:>3}  bounced {r['bounced'] or 0:>3}")
    rej = conn.execute("SELECT COALESCE(reject_category,'?'), COUNT(*) FROM drafts "
                       "WHERE status='rejected' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    if rej:
        _out("\nWhy you rejected drafts:")
        for r in rej:
            _out(f"  {r[0]:<16} {r[1]}")


def cmd_call_list(args):
    from app.sources.call_list import build_call_list
    path = Path(args.path) if args.path else existing_prospect_csv()
    if not path.exists():
        _out(f"missing CSV: {path}")
        sys.exit(2)
    _out(str(build_call_list(_ensure_db(), path, EXPORTS_DIR)))


def cmd_dnc(args):
    conn = _ensure_db()
    if args.dnc_sub == "add":
        add_dnc(conn, DncEntry(company=args.company, domain=args.domain, email=args.email,
                               reason=args.reason, added_by="cli"))
        _out("added")
    elif args.dnc_sub == "list":
        for e in list_dnc(conn):
            _out(f"#{e.id}  {e.company or '-'} | {e.domain or '-'} | {e.email or '-'} | {e.reason or '-'}")


def cmd_serve(args):
    import uvicorn
    uvicorn.run("app.web.server:app", host=DASHBOARD_HOST, port=DASHBOARD_PORT,
                reload=args.reload, log_level="info")


def cmd_gmail_auth(args):
    from app.auth.gmail_oauth import get_credentials
    creds = get_credentials(force_login=args.force, read=args.read, interactive=True)
    kind = "read-only (sync)" if args.read else "compose (drafts)"
    _out(f"OK - {kind} token saved. Valid={creds.valid}, has_refresh={bool(creds.refresh_token)}")


def cmd_doctor(args):
    """Pre-flight: claude login (live probe), deps, Playwright, Gmail tokens, DB, compliance."""
    import importlib.util
    import shutil
    import subprocess
    from app.settings import (FIRECRAWL_API_KEY, GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH,
                              GMAIL_READ_TOKEN_PATH)
    _out("PromptProofreading Sales Agent - doctor\n")
    issues = 0
    claude_path = shutil.which("claude")
    if claude_path:
        try:
            ver = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                                 timeout=15).stdout.strip()
        except Exception:
            ver = "(version probe failed)"
        _out(f"  claude CLI         found at {claude_path} ({ver})")
        if not args.skip_claude:
            from app._claude_client import probe
            ok, detail = probe(MODEL_AUDIT)
            _out(f"  claude login+model {'OK' if ok else 'FAIL'} ({MODEL_AUDIT}): {detail}")
            issues += 0 if ok else 1
    else:
        _out("  claude CLI         MISSING - install Claude Code")
        issues += 1
    _out(f"  FIRECRAWL_API_KEY  {'OK' if FIRECRAWL_API_KEY else 'optional (only for node-discovery)'}")
    _out("\nPython deps:")
    for mod in ["claude_agent_sdk", "fastapi", "uvicorn", "googleapiclient", "google_auth_oauthlib",
                "playwright", "bs4", "lxml", "pandas", "openpyxl", "dotenv"]:
        ok = importlib.util.find_spec(mod) is not None
        _out(f"  {mod:<24} {'OK' if ok else 'MISSING - pip install -e .'}")
        issues += 0 if ok else 1
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            try:
                p.chromium.launch(headless=True).close()
                _out("\nPlaywright Chromium: OK")
            except Exception as e:
                _out(f"\nPlaywright Chromium: MISSING - run `playwright install chromium` ({e})")
                issues += 1
    except ImportError:
        _out("\nPlaywright Chromium: skipped (playwright not installed)")
    _out(f"\nGmail credentials.json    {'OK' if GMAIL_CREDENTIALS_PATH.exists() else 'MISSING'}")
    issues += 0 if GMAIL_CREDENTIALS_PATH.exists() else 1
    _out(f"Gmail compose token       {'OK' if GMAIL_TOKEN_PATH.exists() else 'MISSING - run pp-agent gmail-auth'}")
    _out(f"Gmail read token (sync)   {'OK' if GMAIL_READ_TOKEN_PATH.exists() else 'not set up - run pp-agent gmail-auth --read'}")
    _out(f"\nDB present: {'OK' if DB_PATH.exists() else 'MISSING - run pp-agent init'}")
    if DB_PATH.exists():
        conn = _ensure_db()
        _out(f"DB prospects: {conn.execute('SELECT COUNT(*) FROM prospects').fetchone()[0]}")
    from app.draft.voice import compliance_warnings
    for w in compliance_warnings():
        _out(f"\nCOMPLIANCE: {w}")
    _out(f"\n{'PASS' if issues == 0 else f'{issues} issue(s) - see above'}")
    if issues:
        sys.exit(1)


def cmd_export(args):
    """Export a draft as .eml (same MIME, incl. compliance footer, as Gmail gets)."""
    import base64
    from app.draft.gmail_client import _build_mime
    conn = _ensure_db()
    d = conn.execute("""SELECT d.subject, d.body_text, d.body_html, p.email, p.company
                        FROM drafts d JOIN prospects p ON p.id = d.prospect_id WHERE d.id = ?""",
                     (args.draft_id,)).fetchone()
    if not d:
        _out(f"draft #{args.draft_id} not found")
        sys.exit(2)
    raw = _build_mime(d["email"] or "unknown@example.com", d["company"], d["subject"],
                      d["body_text"], d["body_html"])
    out = Path(args.output) if args.output else Path(f"draft_{args.draft_id}.eml")
    out.write_bytes(base64.urlsafe_b64decode(raw.encode("ascii")))
    _out(f"wrote {out} ({out.stat().st_size} bytes)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pp-agent", description="PromptProofreading sales agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create DB").set_defaults(func=cmd_init)

    ing = sub.add_parser("ingest", help="ingest prospects from a source")
    ing.set_defaults(func=cmd_ingest)
    ing_sub = ing.add_subparsers(dest="ingest_sub", required=True)
    p_manual = ing_sub.add_parser("manual")
    p_manual.add_argument("urls", nargs="+")
    p_manual.add_argument("--company")
    p_manual.add_argument("--industry")
    p_manual.add_argument("--state")
    p_csv = ing_sub.add_parser("master-csv")
    p_csv.add_argument("--path")
    p_csv.add_argument("--limit", type=int)
    p_xlsx = ing_sub.add_parser("xlsx")
    p_xlsx.add_argument("path")
    p_xlsx.add_argument("--limit", type=int)
    p_xdir = ing_sub.add_parser("xlsx-dir")
    p_xdir.add_argument("directory", nargs="?")
    p_xdir.add_argument("--limit", type=int)
    p_node = ing_sub.add_parser("node-discovery")
    p_node.add_argument("--location", required=True)
    p_node.add_argument("--type", default="restaurants")
    p_node.add_argument("--limit", type=int, default=50)

    aud = sub.add_parser("audit", help="audit one prospect or the next batch")
    aud.set_defaults(func=cmd_audit)
    aud.add_argument("prospect_id", type=int, nargs="?")
    aud.add_argument("--limit", type=int, default=20)
    aud.add_argument("--force-fetch", action="store_true")
    aud.add_argument("--allow-no-email", action="store_true",
                     help="audit even when no email can be found (spends a Claude call)")

    drf = sub.add_parser("draft", help="draft one prospect or the next batch")
    drf.set_defaults(func=cmd_draft)
    drf.add_argument("prospect_id", type=int, nargs="?")
    drf.add_argument("--grade", default="AB", choices=["A", "B", "AB"])
    drf.add_argument("--limit", type=int, default=10)
    drf.add_argument("--name")
    drf.add_argument("--allow-no-email", action="store_true")

    sub.add_parser("sync", help="reconcile with Gmail (read-only)").set_defaults(func=cmd_sync)
    fu = sub.add_parser("followups", help="draft follow-ups for unanswered threads")
    fu.set_defaults(func=cmd_followups)
    fu.add_argument("--limit", type=int, default=20)

    dly = sub.add_parser("daily", help="sync -> follow-ups -> audit -> draft -> summary")
    dly.set_defaults(func=cmd_daily)
    dly.add_argument("--audit", type=int, default=25)
    dly.add_argument("--draft", type=int, default=15)
    dly.add_argument("--followups", type=int, default=20)
    dly.add_argument("--verbose", "-v", action="store_true")

    mk = sub.add_parser("mockup", help="before/after phone mockup (one prospect, or all replies)")
    mk.set_defaults(func=cmd_mockup)
    mk.add_argument("prospect_id", type=int, nargs="?")
    mk.add_argument("--limit", type=int, default=20)
    mk.add_argument("--no-render", action="store_true", help="write the HTML only, skip the PNG")

    cl = sub.add_parser("call-list", help="phone list of businesses with no website")
    cl.set_defaults(func=cmd_call_list)
    cl.add_argument("--path")

    dnc = sub.add_parser("dnc")
    dnc.set_defaults(func=cmd_dnc)
    dnc_sub = dnc.add_subparsers(dest="dnc_sub", required=True)
    p_dnc_add = dnc_sub.add_parser("add")
    p_dnc_add.add_argument("--company")
    p_dnc_add.add_argument("--domain")
    p_dnc_add.add_argument("--email")
    p_dnc_add.add_argument("--reason")
    dnc_sub.add_parser("list")

    srv = sub.add_parser("serve", help="run dashboard")
    srv.set_defaults(func=cmd_serve)
    srv.add_argument("--reload", action="store_true")

    gauth = sub.add_parser("gmail-auth", help="run Gmail OAuth flow")
    gauth.set_defaults(func=cmd_gmail_auth)
    gauth.add_argument("--force", action="store_true")
    gauth.add_argument("--read", action="store_true", help="authorize the read-only sync token")

    sub.add_parser("status", help="pipeline state").set_defaults(func=cmd_status)
    sub.add_parser("stats", help="reply/opt-out rates by segment").set_defaults(func=cmd_stats)
    doc = sub.add_parser("doctor", help="pre-flight check")
    doc.set_defaults(func=cmd_doctor)
    doc.add_argument("--skip-claude", action="store_true", help="skip the live Claude login probe")

    exp = sub.add_parser("export", help="export draft as .eml file")
    exp.set_defaults(func=cmd_export)
    exp.add_argument("draft_id", type=int)
    exp.add_argument("--output", "-o")
    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
