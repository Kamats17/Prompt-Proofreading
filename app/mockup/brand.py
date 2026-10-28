"""Pull a prospect's own branding (logo, main color) out of their cached homepage.

Nothing is invented: if no logo is found the mockup uses a text wordmark of the
business name; if no usable brand color is found it uses a neutral palette for
the industry.
"""
import colorsys
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})")

INDUSTRY_FALLBACK = {
    "restaurant": "#B23A2B", "cafe": "#8A5A2B", "diner": "#B23A2B", "pizza": "#B23A2B",
    "barber": "#1F3A5F", "salon": "#8E3B6E", "spa": "#2F6F6B", "dent": "#1F6FA8",
    "clinic": "#1F6FA8", "auto": "#2B2B2B", "plumb": "#1F5FA8", "hvac": "#1F5FA8",
}
DEFAULT_COLOR = "#1F3A5F"


def _to_rgb(hex_or_tuple) -> tuple[int, int, int]:
    if isinstance(hex_or_tuple, tuple):
        return hex_or_tuple
    h = hex_or_tuple.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _luminance(rgb) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _is_brandish(rgb) -> bool:
    """Reject whites, blacks, and grays — keep colors with real saturation."""
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    return s >= 0.25 and 0.12 <= l <= 0.85


def readable_on_white(rgb) -> tuple[int, int, int]:
    """Darken until white text on it has decent contrast (>= 4.5:1)."""
    r, g, b = rgb
    for _ in range(20):
        if (1.05) / (_luminance((r, g, b)) + 0.05) >= 4.5:
            break
        r, g, b = int(r * 0.88), int(g * 0.88), int(b * 0.88)
    return r, g, b


def _colors_from_html(html: str, soup: BeautifulSoup) -> list[tuple[int, int, int]]:
    theme = soup.find("meta", attrs={"name": re.compile("^theme-color$", re.I)})
    found: list[tuple[int, int, int]] = []
    if theme and _HEX.match((theme.get("content") or "").strip()):
        found += [_to_rgb(theme["content"].strip())] * 25      # strong signal
    css = " ".join(t.get_text(" ") for t in soup.find_all("style"))
    css += " " + " ".join(el.get("style", "") for el in soup.find_all(style=True))
    found += [_to_rgb("#" + m) for m in _HEX.findall(css)]
    found += [tuple(min(255, int(x)) for x in m) for m in _RGB.findall(css)]
    return found


def brand_color(html: str, soup: BeautifulSoup, industry: str | None) -> tuple[str, str]:
    """(hex color safe for white text, source) — source is 'site' or 'industry'."""
    counts = Counter(c for c in _colors_from_html(html, soup) if _is_brandish(c))
    if counts:
        return _hex(readable_on_white(counts.most_common(1)[0][0])), "site"
    ind = (industry or "").lower()
    for key, color in INDUSTRY_FALLBACK.items():
        if key in ind:
            return color, "industry"
    return DEFAULT_COLOR, "industry"


_NOT_THEIR_LOGO = ("instagram", "facebook", "twitter", "tiktok", "youtube", "yelp", "google",
                   "tripadvisor", "doordash", "grubhub", "ubereats", "pinterest", "linkedin",
                   "social", "icon", "badge", "award", "visa", "mastercard", "paypal", "wifi")


def find_logo(soup: BeautifulSoup, base_url: str) -> str | None:
    """Absolute http(s) URL of the site's logo image, if one is clearly marked."""
    def score(img) -> int:
        blob = " ".join([img.get("src") or "", img.get("alt") or "", img.get("id") or "",
                         " ".join(img.get("class") or [])]).lower()
        if any(k in blob for k in _NOT_THEIR_LOGO):
            return -1
        s = 0
        if "logo" in blob:
            s += 3
        if img.find_parent(["header", "nav"]):
            s += 1
        if (img.get("src") or "").lower().endswith((".svg", ".png", ".webp")):
            s += 1
        return s
    candidates = sorted((i for i in soup.find_all("img") if i.get("src")), key=score, reverse=True)
    for img in candidates[:1]:
        if score(img) < 3:
            return None
        url = urljoin(base_url, img["src"].strip())
        if url.startswith(("http://", "https://")):
            return url
    return None


def extract_brand(html_path: str | Path, base_url: str, industry: str | None) -> dict:
    html = Path(html_path).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    color, source = brand_color(html, soup, industry)
    return {"color": color, "color_source": source, "logo_url": find_logo(soup, base_url)}
