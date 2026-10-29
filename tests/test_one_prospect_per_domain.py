"""One prospect per business across lead sources, without merging different
businesses that share a platform, and without overwriting existing details."""
import pytest

from app.db.migrate import migrate, connect
from app.db.models import Prospect
from app.db.repo import upsert_prospect, get_prospect
from app.safety.aggregator_filter import is_aggregator
from app.sources._normalize import normalize_url


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "t.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def _add(conn, company, url, **kw):
    return upsert_prospect(conn, Prospect(company=company, url=normalize_url(url),
                                          source=kw.pop("source", "manual"), **kw))


def test_same_site_different_paths_is_one_prospect(conn):
    a = _add(conn, "Joe's Pizza", "http://www.joespizza.com/", source="master_csv")
    b = _add(conn, "Joe's Pizza", "https://joespizza.com/home", source="node_discovery",
             email="joe@joespizza.com")
    assert a == b
    rows = conn.execute("SELECT url, email, source FROM prospects").fetchall()
    assert len(rows) == 1
    assert rows[0]["url"] == "https://joespizza.com"          # first-seen URL kept
    assert rows[0]["email"] == "joe@joespizza.com"           # missing data filled in
    assert rows[0]["source"] == "master_csv"


def test_homepage_merges_even_if_names_differ(conn):
    a = _add(conn, "Joespizza", "joespizza.com")              # domain-derived placeholder name
    b = _add(conn, "Joe's Famous Pizza", "joespizza.com/menu")
    assert a == b
    assert get_prospect(conn, a).company == "Joe's Famous Pizza"  # real name replaces placeholder


def test_different_businesses_on_one_domain_stay_separate(conn):
    # Two real businesses with sub-pages on the same (non-platform) domain.
    a = _add(conn, "Main Street Dental", "https://smallbizsites.net/main-street-dental")
    b = _add(conn, "Riverside Auto", "https://smallbizsites.net/riverside-auto")
    assert a != b


def test_subdomains_stay_separate(conn):
    a = _add(conn, "A", "joespizza.com")
    b = _add(conn, "B", "order.joespizza.com")
    c = _add(conn, "C", "bobsbbq.com")
    assert len({a, b, c}) == 3


@pytest.mark.parametrize("url", [
    "https://www.vagaro.com/joesbarber", "https://booksy.com/en-us/123_joes",
    "https://www.styleseat.com/m/v/bella", "https://www.toasttab.com/tonys-pizza",
    "https://order.online/store/rosas-tacos-123", "https://www.chownow.com/order/1/locations/2",
])
def test_booking_and_ordering_platforms_are_blocked(url):
    blocked, why = is_aggregator(normalize_url(url))
    assert blocked and "platform" in why


def test_merge_never_overwrites_existing_details(conn):
    a = _add(conn, "Joe's Pizza", "joespizza.com", email="joe@joespizza.com",
             phone="(412) 555-0101", contact_name="Joe")
    _add(conn, "Joe's Pizza", "joespizza.com/about", email="someone@else.com",
         phone="(412) 555-9999", contact_name="Stranger")
    _add(conn, "Joe's Pizza", "joespizza.com", email="reimport@changed.com")  # same URL re-import
    p = get_prospect(conn, a)
    assert (p.email, p.phone, p.contact_name) == ("joe@joespizza.com", "(412) 555-0101", "Joe")
    assert p.email_source is None or p.email_source != "reimport"
