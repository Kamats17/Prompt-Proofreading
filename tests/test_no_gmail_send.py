"""SECURITY TEST — fails if any Gmail send path appears anywhere in source.

The agent must NEVER send email. Google's gmail.compose scope DOES permit
sending (drafts.send and messages.send), so this test — not the OAuth scope — is
the guarantee. It checks two ways:
  1. regex: messages().send, drafts().send, gmail_send / gmail.send, .send( after
     a Gmail-ish expression
  2. AST: any call whose attribute is `send` on a chain that touches
     users()/drafts()/messages()
"""
import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [PROJECT_ROOT / "app", PROJECT_ROOT / "tests", PROJECT_ROOT / "node_discovery",
             PROJECT_ROOT / "scripts"]
EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", "data", "auth", ".venv", "venv", "node_modules"}
SELF = Path(__file__).resolve()

FORBIDDEN_PATTERNS = [
    re.compile(r"messages\(\)\s*\.\s*send"),
    re.compile(r"drafts\(\)\s*\.\s*send"),
    re.compile(r"\bgmail[_.]send\b", re.IGNORECASE),
    re.compile(r"users\(\)[\s\S]{0,80}\.send\s*\("),
    re.compile(r"\.send\s*\(\s*userId\s*="),
]
_GMAIL_CHAIN = {"users", "drafts", "messages", "threads"}


def _targets() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        if d.exists():
            files.extend(d.rglob("*.py"))
    files.extend(p for p in PROJECT_ROOT.glob("*.py") if p.is_file())
    return [f for f in files
            if not any(part in EXCLUDE_DIR_NAMES for part in f.parts) and f.resolve() != SELF]


def _chain_names(node: ast.AST) -> set[str]:
    names = set()
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
            node = node.value
        else:
            break
    return names


def _ast_hits(path: Path, text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send" and _chain_names(node.func.value) & _GMAIL_CHAIN):
            hits.append(f"{path}:{node.lineno} AST send() on Gmail chain")
    return hits


def _scan() -> list[str]:
    hits: list[str] = []
    for path in _targets():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat in FORBIDDEN_PATTERNS:
                if pat.search(line):
                    hits.append(f"{path}:{lineno} matched /{pat.pattern}/ -> {line.strip()}")
        for pat in FORBIDDEN_PATTERNS[3:4]:  # multi-line chain pattern over whole file
            for m in pat.finditer(text):
                hits.append(f"{path}: multi-line send chain -> {m.group(0)[:80]!r}")
        hits.extend(_ast_hits(path, text))
    return hits


def test_no_gmail_send_calls_anywhere():
    hits = _scan()
    assert not hits, "FORBIDDEN gmail send path detected:\n" + "\n".join(hits)


def test_tripwire_catches_drafts_send(tmp_path):
    """The guard itself must catch the most natural send call in this codebase."""
    bad = "service.users().drafts().send(userId='me', body={'id': d}).execute()\n"
    assert any(p.search(bad) for p in FORBIDDEN_PATTERNS)
    f = tmp_path / "x.py"
    f.write_text(bad)
    assert _ast_hits(f, bad)


def test_tripwire_catches_multiline_send(tmp_path):
    bad = "svc = service.users()\nsvc.messages().send(\n    userId='me', body={})\n"
    f = tmp_path / "y.py"
    f.write_text(bad)
    assert _ast_hits(f, bad)


if __name__ == "__main__":
    test_no_gmail_send_calls_anywhere()
    print("OK — no gmail send calls anywhere")
