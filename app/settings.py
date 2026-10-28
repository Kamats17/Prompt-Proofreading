"""Centralized config loader. Reads config.json + .env into typed constants.

config.json is the public, shareable configuration. Personal values (postal
address, local folder paths, anything you don't want in git) go in
config.local.json next to it; that file is git-ignored and deep-merged on top.
See config.local.example.json."""
import json
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config.local.json"

load_dotenv(PROJECT_ROOT / ".env")



def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


with open(CONFIG_PATH, encoding="utf-8") as f:
    _CFG = json.load(f)
if LOCAL_CONFIG_PATH.exists():
    with open(LOCAL_CONFIG_PATH, encoding="utf-8") as f:
        _CFG = _deep_merge(_CFG, json.load(f))

# Brand
BRAND = _CFG["brand"]
SENDER_NAME = BRAND["sender_name"]
SENDER_FIRST_NAME = BRAND["sender_first_name"]
SENDER_EMAIL = BRAND["sender_email"]
SITE_URL = BRAND["site_url"]
FREE_REVIEW_URL = BRAND["free_review_url"]
INTAKE_URL = BRAND["intake_url"]

# Services
SERVICES = _CFG["services"]
SERVICE_KEYS = {s["key"] for s in SERVICES}

# Models
MODEL_AUDIT = _CFG["models"]["audit"]
MODEL_DRAFT = _CFG["models"]["draft"]

# Thresholds
_T = _CFG["thresholds"]
ICP_MIN_FOR_B = _T["icp_min_for_b"]
ICP_MIN_FOR_A = _T["icp_min_for_a"]
DRAFT_MIN_WORDS = _T["draft_min_words"]
DRAFT_MAX_WORDS = _T["draft_max_words"]
FOLLOWUP_MIN_WORDS = _T.get("followup_min_words", 20)
FOLLOWUP_MAX_WORDS = _T.get("followup_max_words", 70)
FUZZY_DNC_THRESHOLD = _T["fuzzy_dnc_threshold"]

# Voice
SUBJECT_BY_FINDING = _CFG["subject_by_finding"]
BANNED_PHRASES = _CFG["banned_phrases"]
FALLBACK_NAME = _CFG["fallback_name"]

# Sending cadence
_S = _CFG.get("sending", {})
DAILY_NEW_CAP = int(_S.get("daily_new_cap", 20))
FOLLOWUP_BUSINESS_DAYS = list(_S.get("followup_business_days", [4, 10]))
MAX_TOUCHES = int(_S.get("max_touches", 3))
RECONTACT_WINDOW_DAYS = int(_S.get("recontact_window_days", 90))

# Compliance (CAN-SPAM footer)
_C = _CFG.get("compliance", {})
REQUIRE_POSTAL_ADDRESS = bool(_C.get("require_postal_address", False))
SENDER_POSTAL_ADDRESS = _C.get("sender_postal_address", "")
SENDER_IDENTITY_LINE = _C.get("sender_identity_line", "")
OPT_OUT_LINE = _C.get("opt_out_line", "")

# Playwright
PW = _CFG["playwright"]

# Paths
PATHS = _CFG["paths"]
DB_PATH = PROJECT_ROOT / PATHS["db"]
CACHE_DIR = PROJECT_ROOT / PATHS["cache_dir"]
LEADS_IN_DIR = PROJECT_ROOT / PATHS["leads_in_dir"]
GMAIL_CREDENTIALS_PATH = PROJECT_ROOT / PATHS["gmail_credentials"]
GMAIL_TOKEN_PATH = PROJECT_ROOT / PATHS["gmail_token"]
GMAIL_READ_TOKEN_PATH = PROJECT_ROOT / PATHS.get("gmail_read_token", "auth/.gmail_read_token.json")
EXPORTS_DIR = PROJECT_ROOT / PATHS.get("exports_dir", "data/exports")
MOCKUPS_DIR = PROJECT_ROOT / PATHS.get("mockups_dir", "data/mockups")

# Gmail
GMAIL_SCOPE = _CFG["gmail"]["scope"]
GMAIL_READ_SCOPE = _CFG["gmail"].get("read_scope", "https://www.googleapis.com/auth/gmail.readonly")
GMAIL_USER_ID = _CFG["gmail"]["user_id"]

# Dashboard
DASHBOARD_HOST = _CFG["dashboard"]["host"]
DASHBOARD_PORT = _CFG["dashboard"]["port"]

# Secrets from env
# Claude calls go through Claude Agent SDK using the local `claude` CLI session
# (your Claude Max subscription) — no Anthropic API key required.
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")


def _path(key: str, default: str) -> Path:
    p = Path(PATHS.get(key) or default)
    return p if p.is_absolute() else PROJECT_ROOT / p


def existing_prospect_csv() -> Path:
    """Master prospect CSV (set paths.master_csv in config.local.json)."""
    return _path("master_csv", "data/leads_in/master.csv")


def existing_prospects_xlsx_dir() -> Path:
    return _path("xlsx_dir", "data/leads_in")


def node_prospecting_dir() -> Path:
    """Folder of the separate Node prospecting tool (paths.node_prospecting_dir)."""
    return _path("node_prospecting_dir", "../prospecting")
