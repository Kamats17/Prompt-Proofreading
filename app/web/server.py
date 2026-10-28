"""FastAPI dashboard for prospect / audit / draft review.

Security model: the dashboard binds to 127.0.0.1, but any website open in your
browser can still try to POST to http://127.0.0.1:8765 (CSRF) or reach it via DNS
rebinding. So:
  * TrustedHostMiddleware rejects requests whose Host isn't localhost/127.0.0.1
    (defeats DNS rebinding);
  * every state-changing request must come from the dashboard's own origin
    (Origin header, or Referer when Origin is absent); "null" origins are refused;
  * cached third-party HTML is never served — only screenshot PNGs, via a route.
"""
import json
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.db.migrate import migrate
from app.settings import DB_PATH, DASHBOARD_PORT

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fromjson(value):
    if value is None or value == "":
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


templates.env.filters["fromjson"] = _fromjson


def _allowed_origin(origin: str) -> bool:
    try:
        u = urlparse(origin)
    except ValueError:
        return False
    return (u.scheme in ("http", "https") and u.hostname in ("127.0.0.1", "localhost", "testserver")
            and (u.port in (None, DASHBOARD_PORT, 80)))


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Refuse cross-site state-changing requests (CSRF)."""

    async def dispatch(self, request: Request, call_next):
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            if origin is not None:
                if origin == "null" or not _allowed_origin(origin):
                    return PlainTextResponse("cross-site request refused", status_code=403)
            elif referer is not None:
                if not _allowed_origin(referer):
                    return PlainTextResponse("cross-site request refused", status_code=403)
            # Neither header: a non-browser client (curl, tests). Browsers always
            # send Origin on cross-site form POSTs, so CSRF can't take this branch.
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


def create_app() -> FastAPI:
    migrate(DB_PATH)

    app = FastAPI(title="PromptProofreading Sales Agent")
    app.add_middleware(SameOriginMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from app.web.routes import register_routes
    register_routes(app, templates)
    return app


app = create_app()
