"""Fetch cache validity: a cached FAILURE is never served (Change 3)."""
import json
import time

from app.audit.fetcher import _is_fresh, CACHE_TTL_SECONDS


def _meta(tmp_path, **kw):
    meta = {"fetched_at_epoch": time.time(), "ok": True, "status": 200}
    meta.update(kw)
    mp = tmp_path / "meta.json"
    hp = tmp_path / "page.html"
    mp.write_text(json.dumps(meta), encoding="utf-8")
    return mp, hp


def test_fresh_ok_with_html(tmp_path):
    mp, hp = _meta(tmp_path)
    hp.write_text("<html></html>", encoding="utf-8")
    assert _is_fresh(mp, hp) is True


def test_failure_meta_not_fresh(tmp_path):
    mp, hp = _meta(tmp_path, ok=False, error="timeout")
    hp.write_text("<html></html>", encoding="utf-8")
    assert _is_fresh(mp, hp) is False


def test_missing_html_not_fresh(tmp_path):
    mp, hp = _meta(tmp_path)  # html not written
    assert _is_fresh(mp, hp) is False


def test_stale_not_fresh(tmp_path):
    mp, hp = _meta(tmp_path, fetched_at_epoch=time.time() - CACHE_TTL_SECONDS - 10)
    hp.write_text("<html></html>", encoding="utf-8")
    assert _is_fresh(mp, hp) is False
