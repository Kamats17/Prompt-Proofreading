# PromptProofreading Sales Agent

A local sales assistant for [PromptProofreading.com](https://promptproofreading.com). It finds small businesses whose websites lose them customers, checks each site the way a customer on a phone would, and writes a short, specific first email plus follow-ups. **Every email waits in your review queue and is sent by you from Gmail.**

## What it will and won't do

| Guarantee | How it's enforced |
|---|---|
| **Never sends email.** It only creates Gmail drafts. | `app/draft/gmail_client.py` exposes only `drafts.create`. `tests/test_no_gmail_send.py` fails the build on any send call (regex + AST). *Note: Google's `gmail.compose` scope **does** allow sending; the guarantee is the code, not the scope.* |
| **No invented claims.** Every point in an email is backed by an exact quote from the site or a machine-checked fact. | `safety/fabrication_guard.py` re-checks every quote verbatim (case-sensitive) against the saved page and every fact ID against code-computed facts. "Typo" claims must be a known grammar slip or a dictionary-flagged misspelling with a near-miss correction (offline `pyspellchecker`); brand names, dish names, and British spellings are rejected. The QA gate re-checks quotes in the final email. |
| **Never emails the same business twice by mistake.** | One live draft per prospect per touch (database unique index), a 90-day recontact window across email *and* domain, and a re-check at the moment you click Approve. |
| **Respects opt-outs.** | `pp-agent sync` detects "no / unsubscribe" replies and adds the email and domain to the Do-Not-Contact list automatically. There's also an "opted out" button. |
| **Doesn't email the wrong site.** | Blocks directory and listing pages, franchises, redirects to other domains, hijacked/parked domains, and pages that never mention the business. |

## Daily routine (about 10 minutes)

```bash
pp-agent daily        # sync Gmail -> follow-ups -> audit 25 leads -> draft 15 -> summary
pp-agent serve        # then open http://127.0.0.1:8765/queue
```

In the **review queue**, each draft sits next to its evidence and screenshots of the site on desktop and phone. Keys: `J`/`K` next/previous, `A` approve, `R` reject (pick a reason), `O` open the lead. Approved drafts land in Gmail Drafts; read them once more and send. Tip: use Gmail's *Schedule send* to spread sends through the day instead of sending a batch at once. A **daily cap** (`sending.daily_new_cap`, default 20 new emails) protects your domain's reputation.

To run it unattended each morning, see `scripts/run_daily.ps1`.

## Setup (once)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
playwright install chromium
copy config.local.example.json config.local.json   # your postal address + local lead-file paths (git-ignored)
claude auth login               # the agent uses your Claude subscription through the local claude CLI
pp-agent gmail-auth          # compose token: lets it create drafts
pp-agent gmail-auth --read   # read-only token: lets `sync` see sends, replies, and opt-outs
pp-agent init
pp-agent doctor              # live-checks the Claude login and model, Playwright, Gmail tokens, compliance
```

Gmail OAuth: in Google Cloud Console, create an OAuth client ID of type *Desktop app* and save the JSON as `auth/credentials.json`. If your OAuth app is in Google's *Testing* mode, refresh tokens expire after 7 days. Re-run `pp-agent gmail-auth` when the dashboard tells you to, or publish the app.

## How a lead flows

1. **Ingest:** `pp-agent ingest master-csv | xlsx | xlsx-dir | node-discovery | manual <urls>`. The importer skips directory URLs, non-US leads, and junk in the "Contact Name" column. In the current master CSV, 283 of 286 of those values are Restaurantji links.
2. **Audit:** `pp-agent audit`. One headless browser visit captures:
   - the desktop page and a screenshot
   - an **iPhone render** (sideways scrolling, tiny text, whether there is a tap-to-call link, and a phone screenshot)
   - up to 3 contact/about pages
   - whether HTTPS works, and when the site's security certificate expires
   - page weight and load time on the phone render, measured locally (no Google APIs or paid services)

   Code then computes **facts**: `phone_not_tappable`, `pdf_menu`, `third_party_ordering:doordash`, `no_https`, `cert_expired`, `desktop_layout_on_phone`, `heavy_page_mobile`, `slow_main_content`, `stale_copyright`, designer/platform credits, and so on.

   Email is taken only from the business's own domain or a free-mail provider, never from a vendor in the footer. If no email is found, the lead is parked **before** spending a Claude call.

   Claude then returns findings, each tied to a quote or a fact ID. The fabrication guard drops anything it can't verify.
3. **Score** (code, not the model): `icp = 0.6 x site_need + 0.4 x buyer_fit`.
   - Site need comes from the severity of verified findings.
   - Buyer fit is higher for higher-ticket industries, active businesses, and a known owner name. It is lower for agency- or platform-run sites and for leads with no contact route.

   Grade A ≥ 70, B ≥ 50. Only A/B leads get drafted.
4. **Draft:** a 55–105 word email.
   - It opens with the main finding as a customer experiences it, offers a specific free mockup, and ends in a yes/no question.
   - No links in the body. The subject is picked from the finding ("Frank's phone number").
   - The signature and the CAN-SPAM footer (solicitation notice + opt-out) are added in code.
5. **QA gate:** hard blockers stay hard: exact quotes, verified evidence, Do-Not-Contact, recontact window, blocked prospect, grade, compliance settings. Style checks (subject list, length, links, dashes, banned phrases) block model output but become **warnings once you've edited a draft by hand**. A failing draft gets one automatic regeneration with the failed checks fed back.
6. **Approve:** the dashboard re-runs QA against current data, enforces the daily cap, and creates the Gmail draft. If Gmail fails, the draft goes back to pending.
7. **Sync and follow-ups:** `pp-agent sync` (read-only) marks drafts sent, replied, bounced, or opted out. `pp-agent followups` drafts touch 2 (4 business days after the first email) and touch 3 (10 business days) as replies in the same Gmail thread, only when nobody has answered.

## When someone says yes: the mockup

Every first email offers a free mockup. When a prospect replies, `pp-agent daily` (or `pp-agent mockup`) builds it automatically, with no Claude call:

- **Today:** the real iPhone screenshot captured during their audit.
- **Proposed:** a phone-sized redesign of their first screen, using their own logo, main color, business name, headline, phone number (as a tap-to-call button), and hours and address copied word for word from their page, plus the buttons their kind of business needs (Menu / Order / Book / Quote).
- **What I'd change:** a numbered list where every item is tied to a verified fact from the audit (tap-to-call, PDF menu, https, page weight, typos, ...).

Nothing is invented: if their hours aren't on the page, or are too long to show in full, they're left out rather than shortened. The footer labels it a concept mockup, not their live site.

On the prospect's page in the dashboard: check the mockup, edit the suggested reply (it offers two meeting days), and click **Put mockup reply in Gmail Drafts**. It lands as a reply in the same thread with the PNG attached, and you send it from Gmail. The same Do-Not-Contact and opt-out checks apply.

```bash
pp-agent mockup 42          # one prospect
pp-agent mockup             # everyone who replied and doesn't have one yet
```

## Leads without a website

The master CSV has far more businesses with **no website of their own** (only a Restaurantji/Facebook listing) than businesses with one. Email can't reach them, but they're often the best fit for a site build.

```bash
pp-agent call-list     # -> data/exports/call_list_<date>.csv (phone, city, listing, suggested pitch)
```

For manual calls only. Check numbers that may be mobile phones against the National Do Not Call Registry, and never autodial or text.

## Compliance (US CAN-SPAM; not legal advice)

Every email includes a plain solicitation notice, an opt-out line, and the sender's postal address (`compliance.*` in `config.json`), in both the HTML and plain-text parts. Put the address in `config.local.json` (`compliance.sender_postal_address`), which is git-ignored, so it never lands in the repository. `compliance.require_postal_address` is on, so the QA gate refuses to push a draft until an address is set. Opt-outs are honored automatically by `sync`. The agent only prospects US businesses.

## Commands

| Command | What it does |
|---|---|
| `daily [--audit N --draft N -v]` | The whole routine |
| `audit [<id>] [--limit N] [--allow-no-email] [--force-fetch]` | Audit leads. Failing leads back off for 24h, then give up after 3 tries. A Claude login failure stops the batch. |
| `draft [<id>] [--grade A\|B\|AB] [--limit N]` | Draft leads in score order |
| `sync` / `followups` | Gmail reconciliation / follow-up drafting |
| `status` / `stats` | Pipeline state, compliance warnings / reply and opt-out rates by industry, finding, subject, touch |
| `call-list` | Phone list for no-website leads |
| `mockup [<id>]` | Before/after phone mockup (default: everyone who replied) |
| `dnc add/list`, `export <draft_id>` | Do-Not-Contact list; write a draft as `.eml` |
| `doctor [--skip-claude]` | Pre-flight checks |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `STOPPED: Claude login expired` / doctor shows `claude login+model FAIL [auth]` | Run `claude auth login` in a terminal. The desktop app's login is separate from the CLI's. |
| Approve says "Gmail login expired" | `pp-agent gmail-auth` (and `--read` for sync) |
| A good lead shows `blocked: ...` | Open it in the dashboard to see the reason. Use **Unblock** for anything except DNC/opt-out. |
| A lead keeps failing to audit | `status` shows the count. The prospect page shows the last error. Retries are automatic after 24h, up to 3 times. |
| Dashboard returns 403 on a button | It refuses requests that don't come from the dashboard itself. Use it at `http://127.0.0.1:8765`, not through another host name. |

## Layout

```
app/
  cli.py                  pp-agent commands
  settings.py / textnorm.py
  db/                     schema.sql, migrate.py (safe on old DBs), repo.py
  sources/                CSV/XLSX/manual/node ingest, normalization, call list
  audit/                  fetcher (desktop+iPhone+contact pages), extractor (facts),
                          audit_agent (prompt), grader (scoring), pipeline
  safety/                 dnc, fabrication_guard, aggregator_filter (directories/franchises)
  draft/                  generator, voice (subjects/footer), qa_gate, pipeline,
                          gmail_client (drafts only), gmail_sync, followups
  mockup/                 brand (logo/color), builder (before/after PNG), reply (Gmail draft w/ attachment)
  auth/                   gmail_oauth (compose token + separate read-only token)
  web/                    FastAPI dashboard: review queue, detail, prospects, DNC
scripts/run_daily.ps1     unattended morning run
tests/                    run with: python -m pytest -q
```
