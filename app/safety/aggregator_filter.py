"""Aggregator-domain filter.

Many CSV/XLSX rows have URLs pointing to directory/aggregator sites
(Yelp, RestaurantJi, Facebook business pages, Google Maps, etc.) instead of
the prospect's own website. Auditing those audits the AGGREGATOR's template,
not the prospect — and any draft would reference Yelp's CTA, not the
business's missing CTA.

We block these at ingest. The match is on the registered domain (and well-known
subdomain patterns).
"""
import re

from app.sources._normalize import domain_of, registered_domain

# Public suffix list is overkill — exact-match is enough for the obvious cases.
# Keys are the apex domain (no www, no subdomain).
AGGREGATOR_DOMAINS = frozenset({
    "yelp.com",
    "restaurantji.com",
    "placeid.site",
    "facebook.com",
    "fb.com",
    "instagram.com",
    "google.com",            # google.com/maps/...
    "maps.google.com",
    "goo.gl",
    "foursquare.com",
    "tripadvisor.com",
    "opentable.com",
    "doordash.com",
    "ubereats.com",
    "grubhub.com",
    "seamless.com",
    "menupages.com",
    "allmenus.com",
    "zmenu.com",
    "zomato.com",
    "yellowpages.com",
    "yp.com",
    "bbb.org",
    "manta.com",
    "mapquest.com",
    "bing.com",
    "buyblack.org",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "etsy.com",
    "amazon.com",
    "ebay.com",
    "shopify.com",           # *.myshopify.com handled below
    "wix.com",               # *.wixsite.com handled below
    "weebly.com",
    "squarespace.com",       # *.squarespace.com handled below — actually their custom domains exist; only block bare squarespace.com
    "godaddysites.com",
    "wordpress.com",         # *.wordpress.com handled below; self-hosted WP is fine
    "blogspot.com",
    "carrd.co",
    "linktr.ee",
    "beacons.ai",
    "about.me",
    "calendly.com",
    "eventbrite.com",
    "meetup.com",
    "gofundme.com",
    "kickstarter.com",
    "indiegogo.com",
})

# National franchise / chain domains. A local franchisee cannot buy a website
# rebuild (the site is centrally managed), so these are bad-fit leads — blocked
# the same way as aggregators, with a distinct reason for reporting.
FRANCHISE_DOMAINS = frozenset({
    "mcdonalds.com", "burgerking.com", "wendys.com", "subway.com",
    "tacobell.com", "kfc.com", "pizzahut.com", "dominos.com", "papajohns.com",
    "starbucks.com", "dunkindonuts.com", "dunkin.com", "chipotle.com",
    "chick-fil-a.com", "arbys.com", "sonicdrivein.com", "dairyqueen.com",
    "jimmyjohns.com", "jerseymikes.com", "firehousesubs.com", "wingstop.com",
    "supercuts.com", "greatclips.com", "sportclips.com", "anytimefitness.com",
    "planetfitness.com", "orangetheory.com", "snapfitness.com",
    "jiffylube.com", "midas.com", "meineke.com", "aamco.com",
    "acehardware.com", "truevalue.com", "century21.com", "remax.com",
    "kellerwilliams.com", "hrblock.com", "jacksonhewitt.com", "uhaul.com",
    "servpro.com", "servicemaster.com", "mollymaid.com",
})

# Subdomain-style aggregators where ANY subdomain on the apex is aggregator-hosted.
# Blocks foo.wixsite.com, mysalon.myshopify.com, and directory sites that
# auto-generate a "website" per listing (the business never built or owns it).
AGGREGATOR_HOSTED_SUFFIXES = (
    ".wixsite.com",
    ".myshopify.com",
    ".godaddysites.com",
    ".wordpress.com",
    ".blogspot.com",
    ".weebly.com",
    ".square.site",
    ".webnode.com",
    ".webflow.io",
    ".carrd.co",
    ".linktr.ee",
    ".bandcamp.com",
    ".substack.com",
    ".medium.com",
    ".github.io",
    ".tumblr.com",
    # Directory-generated listing microsites seen in the real lead data:
    ".poi.place",
    ".localoria.com",
    ".menu-world.com",
    ".hollerbuilt.com",
    ".edan.io",
    ".business.site",
    ".restaurantji.com",
)


def _apex(host: str) -> str:
    """Registrable domain (kept for callers that import it)."""
    return registered_domain(host)


def _slug(text: str) -> str:
    text = re.sub(r"['\u2019]", "", (text or "").lower())
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def is_aggregator(url: str) -> tuple[bool, str | None]:
    """Return (True, reason) if the URL host is an aggregator, else (False, None)."""
    if not url:
        return False, None
    host = domain_of(url) or ""
    if not host:
        return False, None

    for suffix in AGGREGATOR_HOSTED_SUFFIXES:
        if host.endswith(suffix) or host == suffix.lstrip("."):
            return True, f"hosted on {suffix.lstrip('.')}"

    apex = registered_domain(host)
    if apex in AGGREGATOR_DOMAINS:
        return True, f"aggregator domain: {apex}"
    if apex in FRANCHISE_DOMAINS:
        return True, f"franchise/chain (centrally managed): {apex}"

    return False, None


def directory_generated_reason(url: str, company: str | None) -> str | None:
    """Catch listing microsites we don't know by name: the business name is the
    SUBDOMAIN of someone else's domain, e.g. joes-corner-diner.somedirectory.com.
    A business's own site has its name in the registrable domain instead."""
    host = domain_of(url) or ""
    apex = registered_domain(host)
    if not host or host == apex or not company:
        return None
    leftmost = host.split(".")[0]
    slug = _slug(company)
    if len(slug) < 5:
        return None
    first_word = slug.split("-")[0]
    name_in_sub = slug in leftmost or leftmost in slug or (
        len(first_word) >= 4 and leftmost.startswith(first_word))
    name_in_apex = first_word in apex.replace("-", "")
    if name_in_sub and not name_in_apex:
        return f"directory-generated subdomain on {apex}"
    return None
