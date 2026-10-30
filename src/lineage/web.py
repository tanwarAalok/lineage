"""Web UI + JSON API, served next to /mcp by the HTTP server.

Auth is a bearer token on every API call (the page keeps it in sessionStorage), so there are no
cookies and therefore no CSRF surface. The page renders all data with textContent and is served
under a strict CSP.
"""

from __future__ import annotations

import json
import logging
from importlib.resources import files
from typing import Any, Callable

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from lineage.auth import Principal
from lineage.server import bearer
from lineage.service import Lineage

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
}
ASSET_TYPES = {"AGENTS.md": "text/markdown", "copilot-instructions.md": "text/markdown", "CLAUDE.md": "text/markdown",
               "GEMINI.md": "text/markdown", "glossary.yaml": "text/yaml"}

Handler = Callable[[Principal, dict, dict], Any]


def register_web(mcp: MCPServer, app: Lineage) -> None:
    static_dir = files("lineage") / "static"
    for path, (name, ctype) in STATIC.items():
        _static(mcp, path, (static_dir / name).read_bytes(), ctype)

    def route(path: str, method: str, fn: Handler) -> None:
        async def handler(request: Request) -> Response:
            try:
                body = await _json_body(request) if method == "POST" else {}
                who = await anyio.to_thread.run_sync(app.authenticate, bearer(request.headers.get("authorization")))
                if who is None:
                    return _error(401, "missing or invalid token")
                result = await anyio.to_thread.run_sync(fn, who, dict(request.path_params), body)
                return result if isinstance(result, Response) else JSONResponse(result, headers=SECURITY_HEADERS)
            except PermissionError as e:
                return _error(403, str(e))
            except LookupError as e:
                return _error(404, str(e))
            except (ValueError, TypeError, FileNotFoundError, NotADirectoryError) as e:
                return _error(400, str(e))
            except Exception:
                log.exception("API error on %s %s", method, path)
                return _error(500, "internal error")
        mcp.custom_route(path, methods=[method], include_in_schema=False)(handler)

    P = "/api/projects/{project}"
    route("/api/me", "GET", lambda who, p, b: {"name": who.name, "is_admin": who.is_admin, "maintains": sorted(who.roles)})
    route("/api/projects", "GET", lambda who, p, b: app.list_projects(who))
    route("/api/projects", "POST", lambda who, p, b: app.start_index_job(who, _s(b, "repo_path"), b.get("project") or None))
    route(P + "/description", "POST", lambda who, p, b: app.set_description(who, p["project"], b.get("description")))
    route(P + "/reindex", "POST", lambda who, p, b: app.reindex(who, p["project"]))
    route(P + "/jobs", "GET", lambda who, p, b: app.jobs(who, p["project"]))
    route(P + "/terms", "GET", lambda who, p, b: app.terms(who, p["project"]))
    route(P + "/terms", "POST", lambda who, p, b: app.save_term(
        who, p["project"], _s(b, "term"), _s(b, "definition"), approve=bool(b.get("approve")), source=b.get("source")))
    route(P + "/rules", "GET", lambda who, p, b: app.rules(who, p["project"]))
    route(P + "/rules", "POST", lambda who, p, b: app.save_rule(
        who, p["project"], _s(b, "title"), _s(b, "body"), _int(b.get("id")), approve=bool(b.get("approve")),
        source=b.get("source")))
    route(P + "/mappings", "GET", lambda who, p, b: app.mappings(who, p["project"]))
    route(P + "/mappings", "POST", lambda who, p, b: app.link_term(
        who, p["project"], _s(b, "term"), _s(b, "symbol"), _s(b, "path")))
    route(P + "/review", "POST", lambda who, p, b: app.review(
        who, p["project"], _s(b, "kind"), _int(b.get("id"), required=True), _s(b, "status")))
    route(P + "/delete", "POST", lambda who, p, b: app.delete(
        who, p["project"], _s(b, "kind"), _int(b.get("id"), required=True)))
    route(P + "/maintainers", "GET", lambda who, p, b: app.maintainers(who, p["project"]))
    route(P + "/maintainers", "POST", lambda who, p, b: app.set_maintainer(
        who, _s(b, "user"), p["project"], bool(b.get("maintainer"))))
    route(P + "/assets/{name}", "GET", lambda who, p, b: _asset(app, who, p["project"], p["name"]))
    route("/api/users", "GET", lambda who, p, b: app.users(who))
    route("/api/users", "POST", lambda who, p, b: app.create_user(who, _s(b, "name"), bool(b.get("is_admin"))))
    route("/api/users/{name}/token", "POST", lambda who, p, b: app.rotate_token(who, p["name"]))
    route("/api/users/{name}/disabled", "POST", lambda who, p, b: app.set_disabled(who, p["name"], bool(b.get("disabled"))))


def _static(mcp: MCPServer, path: str, content: bytes, ctype: str) -> None:
    async def handler(request: Request) -> Response:
        return Response(content, media_type=ctype, headers=SECURITY_HEADERS)
    mcp.custom_route(path, methods=["GET"], include_in_schema=False)(handler)


def _asset(app: Lineage, who: Principal, project: str, name: str) -> Response:
    if name not in ASSET_TYPES:  # whitelist: the name never reaches a path
        raise LookupError(f"unknown asset {name!r}")
    content = app.assets(who, project)[name]
    return Response(content, media_type=ASSET_TYPES[name] + "; charset=utf-8",
                    headers={**SECURITY_HEADERS, "Content-Disposition": f'attachment; filename="{name}"'})


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ValueError("request body too large")
    data = json.loads(raw or b"{}")
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _s(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} is required")
    return value


def _int(value: Any, required: bool = False) -> int | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
        raise ValueError("id must be a positive integer")
    return int(value)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=SECURITY_HEADERS)
