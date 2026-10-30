"""OSM discovery, the cheap old-site prescreen, and the prospects source migration."""
import sqlite3

import pytest

from app.audit.extractor import extract_features
from app.audit.grader import HIGH_TICKET
from app.db.migrate import migrate, connect
from app.sources import from_osm
from app.sources.prescreen import staleness
from app.sources.prospector import ingest_screened


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "p.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def _features(tmp_path, html, url="http://joesplumbing.com/"):
    p = tmp_path / "page.html"
    p.write_text(html, encoding="utf-8")
    return extract_features(p, base_url=url)


OLD_PAGE = """<html><head><title>Joe's Plumbing</title></head><body>
<center><font size=4>Joe's Plumbing</font></center>
<p>Call us at 814-555-0142 for all your plumbing needs in Altoona.</p>
<p>&copy; 2014 Joe's Plumbing</p></body></html>"""

MODERN_PAGE = """<html><head><meta name="viewport" content="width=device-width">
<title>Joe's Plumbing</title></head><body><h1>Joe's Plumbing</h1>
<a href="tel:8145550142">814-555-0142</a><p>&copy; 2026 Joe's Plumbing</p></body></html>"""


def test_old_site_scores_high(tmp_path):
    f = _features(tmp_path, OLD_PAGE)
    score, why = staleness(f, OLD_PAGE, final_url="http://joesplumbing.com/", cert_error=False)
    assert score >= 8
    assert any("viewport" in w for w in why) and "no https" in why and "copyright 2014" in why


def test_modern_site_scores_zero(tmp_path):
    f = _features(tmp_path, MODERN_PAGE, "https://joesplumbing.com/")
    assert staleness(f, MODERN_PAGE, final_url="https://joesplumbing.com/", cert_error=False) == (0, [])


def test_builder_markup_is_not_blamed_on_owner(tmp_path):
    html = MODERN_PAGE.replace("<h1>", "<font size=6><h1>").replace(
        "</body>", "<script src='https://cdn11.editmysite.com/js/jquery-1.8.3.min.js'></script>"
                   "<img src='https://x.weebly.com/a.png'></body>")
    f = _features(tmp_path, html, "https://joesplumbing.com/")
    assert "weebly" in f["platforms"]
    assert staleness(f, html, final_url="https://joesplumbing.com/", cert_error=False)[0] == 0


def test_cert_error_counts_instead_of_https(tmp_path):
    f = _features(tmp_path, MODERN_PAGE, "https://joesplumbing.com/")
    score, why = staleness(f, MODERN_PAGE, final_url="https://joesplumbing.com/", cert_error=True)
    assert score == 3 and why == ["security certificate error"]


def test_osm_candidates_skip_chains_aggregators_and_duplicates():
    els = [
        {"tags": {"name": "Smile Dental", "amenity": "dentist", "website": "smiledental.com",
                  "addr:city": "Altoona", "phone": "+1 814 555 0100"}},
        {"tags": {"name": "Smile Dental 2", "amenity": "dentist", "website": "https://www.smiledental.com/"}},
        {"tags": {"name": "Aspen Dental", "amenity": "dentist", "brand": "Aspen Dental",
                  "website": "https://aspendental.com"}},
        {"tags": {"name": "Car-X", "shop": "car_repair", "website": "https://www.carx.com/des-moines/3345"}},
        {"tags": {"name": "Jiffy", "shop": "car_repair", "website": "https://jiffy.com/locations/altoona"}},
        {"tags": {"name": "Bob's Garage", "shop": "car_repair", "website": "https://www.yelp.com/biz/bobs"}},
        {"tags": {"name": "No Site Roofing", "craft": "roofer"}},
    ]
    cands, skipped = from_osm.candidates_from_elements(els, ["dentist", "auto", "roofer"], "pa")
    assert [c["company"] for c in cands] == ["Smile Dental", "Car-X"]
    assert cands[0]["industry"] == "Dentist" and cands[0]["state"] == "PA" and cands[0]["city"] == "Altoona"
    assert skipped == {"no_website": 1, "chain": 2, "aggregator": 1, "duplicate": 1}


def test_every_category_label_counts_as_high_ticket():
    for key, _, label in from_osm.CATEGORIES.values():
        assert any(k in label.lower() for k in HIGH_TICKET), label


def test_query_is_scoped_to_the_state():
    q = from_osm.build_query("Blair County", "pa", ["dentist", "hvac"])
    assert 'area["ISO3166-2"="US-PA"]' in q and '"name"="Blair County"' in q
    assert 'nwr["amenity"~"^dentist$"][~"^(website|contact:website|url)$"~"."](area.a);' in q
    assert 'nwr["craft"~"^hvac$"]' in q


def test_statewide_query_has_no_county_filter():
    q = from_osm.build_query(from_osm.STATEWIDE, "oh", ["plumber"])
    assert 'area["ISO3166-2"="US-OH"]->.a;' in q and "boundary" not in q


CAND = {"company": "Joe's Plumbing", "url": "http://joesplumbing.com", "industry": "Plumber",
        "city": "Altoona", "state": "PA", "phone": "(814) 555-0142"}


def test_old_site_with_email_becomes_a_lead(conn):
    res = {"ok": True, "email": "joe@joesplumbing.com", "email_source": "harvested_own_domain",
           "stale": 5, "why": ["no https", "copyright 2014"]}
    assert ingest_screened(conn, CAND, res) == "lead"
    row = conn.execute("SELECT * FROM prospects").fetchone()
    assert row["source"] == "osm" and row["email"] == "joe@joesplumbing.com"
    assert row["blocked_reason"] is None and "copyright 2014" in row["node_issues_json"]


def test_old_site_without_email_goes_to_call_list(conn):
    res = {"ok": True, "email": None, "stale": 4, "why": ["no https"]}
    assert ingest_screened(conn, CAND, res) == "call_list"
    assert conn.execute("SELECT blocked_reason FROM prospects").fetchone()[0] == "no_email"


def test_current_dead_and_unreachable_sites_are_not_ingested(conn):
    assert ingest_screened(conn, CAND, {"ok": True, "email": "a@joesplumbing.com",
                                        "stale": 2, "why": []}) == "looks_current"
    assert ingest_screened(conn, CAND, {"ok": False, "error": "http 404"}) == "dead_or_moved"
    assert ingest_screened(conn, {**CAND, "phone": None},
                           {"ok": True, "email": None, "stale": 5, "why": []}) == "no_contact"
    assert conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 0


def test_migration_rebuilds_old_prospects_check_and_keeps_rows(tmp_path):
    db = tmp_path / "old.db"
    migrate(db)
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA foreign_keys = OFF")
    sql = raw.execute("SELECT sql FROM sqlite_master WHERE name='prospects'").fetchone()[0]
    raw.execute("ALTER TABLE prospects RENAME TO p_tmp")
    raw.execute(sql.replace(", 'osm'", ""))
    raw.execute("DROP TABLE p_tmp")
    raw.execute("INSERT INTO prospects (company, url, source) VALUES ('Old', 'https://old.com', 'manual')")
    raw.commit()
    raw.close()
    migrate(db)
    c = connect(db)
    c.execute("INSERT INTO prospects (company, url, source) VALUES ('New', 'https://new.com', 'osm')")
    assert [r[0] for r in c.execute("SELECT company FROM prospects ORDER BY id")] == ["Old", "New"]
    assert c.execute("SELECT name FROM sqlite_master WHERE name='idx_prospects_domain'").fetchone()
    c.close()


def test_web_results_are_filtered_like_osm(monkeypatch):
    from app.sources import from_websearch
    reply = """Here you go:
[{"name": "Kimmen Plumbing", "website": "kimmenplumbing.com", "city": "Altoona", "phone": "814-555-0101"},
 {"name": "Kimmen again", "website": "https://www.kimmenplumbing.com/about"},
 {"name": "Yelp listing", "website": "https://www.yelp.com/biz/x"},
 {"name": "Roto", "website": "https://www.rotorooter.com/locations/altoona"},
 {"name": "", "website": "https://noname.com"}]"""
    calls = []
    monkeypatch.setattr(from_websearch, "call_claude",
                        lambda **kw: calls.append(kw) or reply)
    cands, st = from_websearch.discover_web("Blair County", "pa", ["plumber"])
    assert [c["url"] for c in cands] == ["https://kimmenplumbing.com"]
    assert cands[0]["industry"] == "Plumber" and cands[0]["state"] == "PA"
    assert st == {"web_found": 5, "web_rejected": 3, "web_errors": 0}
    assert calls[0]["tools"] == ("WebSearch",)


def test_web_search_failure_does_not_stop_discovery(monkeypatch):
    from app.sources import from_websearch

    def boom(**kw):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(from_websearch, "call_claude", boom)
    assert from_websearch.discover_web("X", "PA", ["plumber", "hvac"])[1]["web_errors"] == 2


def test_only_web_search_can_be_enabled():
    import asyncio
    from app._claude_client import _query_text
    with pytest.raises(ValueError):
        asyncio.run(_query_text(system="s", user="u", tools=("Bash",)))


def test_sources_merge_one_per_domain(monkeypatch):
    from app.sources import prospector
    monkeypatch.setattr(prospector.from_osm, "discover", lambda a, s, t: (
        [{"company": "Kimmen Plumbing & Heating", "url": "https://kimmenplumbing.com", "phone": "1"}],
        {"found": 1}))
    monkeypatch.setattr(prospector.from_websearch, "discover_web", lambda a, s, t: (
        [{"company": "Kimmen", "url": "https://www.kimmenplumbing.com"},
         {"company": "Barton's", "url": "https://bartonsplumbingandheating.com"}], {"web_found": 2}))
    cands, st = prospector.discover("Blair County", "PA", ["plumber"], ("osm", "web"), log=lambda m: None)
    assert [c["company"] for c in cands] == ["Kimmen Plumbing & Heating", "Barton's"]


def test_prescreen_rejects_hijacked_domain(monkeypatch, tmp_path):
    from app.sources import prescreen as ps

    class R:
        status_code, url = 200, "https://lancasterdentalarts.com/"
        text = ("<html><head><title>Buy cialis online cheap</title></head><body>"
                + "<p>cialis viagra casino slot togel online pharmacy</p>" * 20 + "</body></html>")
    monkeypatch.setattr(ps, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ps, "_get", lambda url: (R(), False, None))
    res = ps.prescreen("https://lancasterdentalarts.com", "Lancaster Dental Arts")
    assert not res["ok"] and "hijacked" in res["error"]


def test_audit_shards_split_leads_without_overlap(conn):
    from app.cli import _next_audit_rows
    for n in range(6):
        conn.execute("INSERT INTO prospects (company, url, source, email) VALUES (?, ?, 'osm', ?)",
                     (f"Biz {n}", f"https://biz{n}.com", f"a@biz{n}.com"))
    conn.commit()
    shards = [{r["id"] for r in _next_audit_rows(conn, 10, (i, 3))} for i in range(3)]
    assert all(len(s) == 2 for s in shards)
    assert set().union(*shards) == {r["id"] for r in _next_audit_rows(conn, 10)}


def test_colleges_and_government_are_not_prospects(conn):
    res = {"ok": True, "email": "studentwellness@dickinson.edu", "stale": 6, "why": ["no https"],
           "final_url": "https://www.dickinson.edu/wellness"}
    assert ingest_screened(conn, {**CAND, "url": "https://www.dickinson.edu/wellness"}, res) == "institution"
    res2 = {"ok": True, "email": "clerk@altoonapa.gov", "stale": 6, "why": []}
    assert ingest_screened(conn, CAND, res2) == "institution"
    assert conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 0


def test_web_designer_free_mail_is_not_the_owner():
    from app.audit.extractor import choose_contact_email
    email, why = choose_contact_email(["jonpinhorn.typedesign@gmail.com"],
                                      "https://stallerfamilypractice.com", company="Staller Family Practice")
    assert email is None
    assert choose_contact_email(["gravitadesigns@gmail.com"], "https://gravita.com",
                                company="Gravita Designs")[0] == "gravitadesigns@gmail.com"
    assert choose_contact_email(["smithplumbing@gmail.com"], "https://smithplumbing.com",
                                company="Smith Plumbing")[0] == "smithplumbing@gmail.com"
