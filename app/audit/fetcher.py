"""Headless Playwright fetcher, cached on disk.

One browser launch per prospect captures:
  * desktop render: rendered HTML + above-fold screenshot (1280x800)
  * mobile render (iPhone 13 profile): sideways-scroll / tiny-text / phone-visible
    metrics + mobile screenshot
  * up to N same-site "extra" pages (contact, about, ...) as HTML, for email and
    owner-name discovery
  * https_ok: False when the site only works over plain http

Cache layout:
  data/cache/<host>/<path-hash>/page.html, screenshot.png, mobile.png,
                                 extra_1.html..., meta.json

A failed or partial fetch is never served from cache (`_is_fresh`), so a
transient failure does not lock a prospect out.
"""
import datetime as _dt
import hashlib
import json
import re
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urlparse

from app.settings import CACHE_DIR, PW
from app.sources._normalize import domain_of, registered_domain

CACHE_TTL_SECONDS = 24 * 3600  # 24h
_SETTLE_MS = 1500  # small settle after DOM ready to let above-fold content paint
_EXTRA_PAGE_TIMEOUT_MS = 15000

# Largest Contentful Paint, read after load (buffered entries).
_LCP_JS = """
() => new Promise(resolve => {
  let v = 0;
  try {
    new PerformanceObserver(list => { for (const e of list.getEntries()) v = e.startTime; })
      .observe({type: 'largest-contentful-paint', buffered: true});
  } catch (e) {}
  setTimeout(() => resolve(v), 300);
})
"""


def check_certificate(host: str, timeout: float = 5.0) -> dict:
    """Free, local TLS check: {days_left} or {error, expired}. No external service."""
    if not host:
        return {"error": "no host"}
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                not_after = tls.getpeercert().get("notAfter")
        expires = _dt.datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=_dt.timezone.utc)
        return {"days_left": (expires - _dt.datetime.now(_dt.timezone.utc)).days,
                "expires": expires.date().isoformat()}
    except ssl.SSLCertVerificationError as e:
        msg = str(e)
        return {"error": msg[:200], "expired": "expired" in msg.lower()}
    except (OSError, ValueError) as e:
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}


_MOBILE_METRICS_JS = """
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const overflow = document.documentElement.scrollWidth > vw + 4;
  let total = 0, small = 0;
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = walker.currentNode.textContent.trim();
    if (t.length < 3) continue;
    const el = walker.currentNode.parentElement;
    if (!el) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    total += t.length;
    if (parseFloat(cs.fontSize) < 12) small += t.length;
  }
  const phoneRe = /\\(?\\d{3}\\)?[\\s.-]?\\d{3}[\\s.-]?\\d{4}/;
  let phoneVisible = false, tapVisible = false;
  for (const el of document.querySelectorAll('a, p, span, div, h1, h2, h3, li, button')) {
    const r = el.getBoundingClientRect();
    if (r.top < 0 || r.top > vh || r.width === 0) continue;
    if (el.tagName === 'A' && (el.getAttribute('href') || '').toLowerCase().startsWith('tel:')) tapVisible = true;
    if (el.children.length === 0 && phoneRe.test(el.textContent || '')) phoneVisible = true;
  }
  const telTotal = document.querySelectorAll('a[href^="tel:" i]').length;
  return {horizontal_overflow: overflow, small_text_share: total ? small / total : 0,
          tel_links_total: telTotal,
          phone_visible_first_screen: phoneVisible, tap_to_call_first_screen: tapVisible,
          viewport_width: vw};
}
"""


def _cache_dir_for(url: str) -> Path:
    u = urlparse(url)
    host = domain_of(url) or "unknown"           # IDNA-encoded: café.com != cafè.com
    safe = re.sub(r"[^a-z0-9.\-_]", "_", host) or "unknown"
    # Include a path/query hash so two prospects on the same host never share an entry.
    path_key = (u.path or "/") + (("?" + u.query) if u.query else "")
    h = hashlib.sha1(path_key.encode("utf-8", "replace")).hexdigest()[:10]
    d = Path(CACHE_DIR) / safe / h
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_fresh(meta_path: Path, html_path: Path) -> bool:
    """Usable only if the meta says the fetch succeeded, the HTML still exists,
    and it is within TTL. Cached failures are never served."""
    if not meta_path.exists() or not html_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not meta.get("ok"):
        return False
    fetched_at = meta.get("fetched_at_epoch", 0)
    return (time.time() - fetched_at) < CACHE_TTL_SECONDS


def _result_from_meta(meta: dict, cache: Path, cached: bool) -> dict:
    shot = cache / "screenshot.png"
    mob = cache / "mobile.png"
    return {
        "ok": bool(meta.get("ok")),
        "cached": cached,
        "html_path": str(cache / "page.html") if (cache / "page.html").exists() else None,
        "screenshot_path": str(shot) if shot.exists() else None,
        "mobile_screenshot_path": str(mob) if mob.exists() else None,
        "meta_path": str(cache / "meta.json"),
        "final_url": meta.get("final_url"),
        "status": meta.get("status"),
        "https_ok": meta.get("https_ok"),
        "mobile": meta.get("mobile") or {},
        "cert": meta.get("cert") or {},
        "cert_ignored": bool(meta.get("cert_ignored")),
        "extra_pages": [str(cache / p) for p in meta.get("extra_pages", []) if (cache / p).exists()],
        "error": meta.get("error"),
    }


def fetch_page(url: str, *, force: bool = False, wait_until: str | None = None,
               mobile: bool = True, crawl: bool = True,
               _https_failed: bool = False, _ignore_cert: bool = False) -> dict:
    """Fetch and cache the page.

    `crawl`: also save up to 3 same-site contact/about pages as extra_N.html.
    Returns {ok, html_path, screenshot_path, mobile_screenshot_path, final_url,
             status, https_ok, mobile, extra_pages, error, cached}.
    """
    cache = _cache_dir_for(url)
    html_path = cache / "page.html"
    meta_path = cache / "meta.json"

    if not force and _is_fresh(meta_path, html_path):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not crawl or meta.get("crawled"):
            return _result_from_meta(meta, cache, cached=True)

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return {"ok": False, "error": "playwright not installed; run `pip install playwright && playwright install chromium`"}

    error: str | None = None
    final_url = url
    status: int | None = None
    html = ""
    mobile_metrics: dict = {}
    extra_saved: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": PW["viewport_width"], "height": PW["viewport_height"]},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                    ignore_https_errors=_ignore_cert,
                )
                page = context.new_page()
                try:
                    resp = page.goto(url, wait_until=wait_until or PW["wait_until"],
                                     timeout=PW["timeout_ms"])
                    if resp:
                        status = resp.status
                        final_url = page.url or resp.url or url
                    page.wait_for_timeout(_SETTLE_MS)
                    html = page.content()
                    page.screenshot(path=str(cache / "screenshot.png"), full_page=False)
                except PWTimeout:
                    error = "timeout"
                    try:
                        html = page.content()
                    except Exception:
                        pass

                # Extra same-site pages (contact/about) — only on the final host.
                if html and not error and crawl:
                    from app.audit.extractor import crawl_candidates_from_html
                    final_reg = registered_domain(final_url)
                    n = 0
                    for extra_url in crawl_candidates_from_html(html, final_url):
                        if n >= 3 or registered_domain(extra_url) != final_reg:
                            continue
                        try:
                            page.goto(extra_url, wait_until="domcontentloaded",
                                      timeout=_EXTRA_PAGE_TIMEOUT_MS)
                            page.wait_for_timeout(600)
                            n += 1
                            name = f"extra_{n}.html"
                            (cache / name).write_text(page.content(), encoding="utf-8",
                                                      errors="replace")
                            extra_saved.append(name)
                        except Exception:
                            continue
                context.close()

                # Mobile render.
                if html and not error and mobile:
                    try:
                        mctx = browser.new_context(**p.devices["iPhone 13"], ignore_https_errors=_ignore_cert)
                        mpage = mctx.new_page()
                        sizes = {"bytes": 0, "requests": 0}

                        def _count(req):
                            try:
                                sizes["bytes"] += max(0, req.sizes().get("responseBodySize", 0))
                                sizes["requests"] += 1
                            except Exception:
                                pass
                        mpage.on("requestfinished", _count)
                        t0 = time.monotonic()
                        mpage.goto(final_url, wait_until="load", timeout=PW["timeout_ms"])
                        load_s = time.monotonic() - t0
                        mpage.wait_for_timeout(_SETTLE_MS)
                        mobile_metrics = mpage.evaluate(_MOBILE_METRICS_JS) or {}
                        lcp_ms = mpage.evaluate(_LCP_JS) or 0
                        mobile_metrics.update({
                            "page_bytes": sizes["bytes"], "requests": sizes["requests"],
                            "load_seconds": round(load_s, 1),
                            "lcp_seconds": round(lcp_ms / 1000, 1) if lcp_ms else None,
                        })
                        mpage.screenshot(path=str(cache / "mobile.png"), full_page=False)
                        mctx.close()
                    except Exception as e:  # mobile is best-effort
                        mobile_metrics = {"error": str(e)[:200]}
            finally:
                browser.close()
    except Exception as e:
        error = (str(e).strip().splitlines() or [type(e).__name__])[0][:300]

    # A bad certificate (usually expired) is itself the finding: visitors get a
    # full-page browser warning. Load past it once so the site can still be
    # audited; check_certificate below records why it failed.
    if error and "ERR_CERT" in error and not _ignore_cert:
        return fetch_page(url, force=True, wait_until=wait_until, mobile=mobile, crawl=crawl,
                          _https_failed=_https_failed, _ignore_cert=True)

    # Retry once over http:// for TLS / connection failures — legacy small-business
    # sites (our best ICP) are often http-only or have broken certificates.
    if error and url.startswith("https://") and not _https_failed and any(
        m in error for m in ("ERR_SSL", "ERR_CERT", "SSL", "ERR_CONNECTION", "net::ERR")
    ):
        return fetch_page("http://" + url[len("https://"):], force=True, wait_until=wait_until,
                          mobile=mobile, crawl=crawl, _https_failed=True)

    if html:
        html_path.write_text(html, encoding="utf-8", errors="replace")

    ok = bool(html) and not error and (status is None or status < 400)
    if not ok and status is not None and status >= 400:
        error = error or f"http {status}"

    meta = {
        "url_requested": url,
        "final_url": final_url,
        "status": status,
        "https_ok": (not _https_failed) and final_url.startswith("https://"),
        "cert": check_certificate(domain_of(url) or "") if html else {},
        "fetched_at_epoch": time.time(),
        "error": error,
        "ok": ok,
        "html_size": len(html),
        "mobile": mobile_metrics,
        "extra_pages": extra_saved,
        "crawled": bool(crawl),
        "wait_until": wait_until or PW["wait_until"],
        "cert_ignored": _ignore_cert,
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return _result_from_meta(meta, cache, cached=False)
