"""`lineage index|search|resolve|projects|user|maintainer|serve`.

The CLI talks to the DB file directly, so it runs as the local admin: use it to bootstrap the
first admin, then manage everything else from the web UI. Logs go to stderr; stdout stays clean
for stdio MCP.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from lineage.config import load_settings

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lineage")
    p.add_argument("--config", help="YAML config file (default: $LINEAGE_CONFIG)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    idx = sub.add_parser("index", help="index a repo (incremental) and write .lineage/ assets")
    idx.add_argument("repo_path")
    idx.add_argument("--project")

    s = sub.add_parser("search", help="hybrid code search")
    s.add_argument("query")
    s.add_argument("--top-n", type=int, default=8)
    s.add_argument("--project")

    r = sub.add_parser("resolve", help="map a design term to code")
    r.add_argument("term")
    r.add_argument("--project")

    sub.add_parser("projects", help="list indexed projects")

    u = sub.add_parser("user", help="manage people (prints new tokens once)")
    usub = u.add_subparsers(dest="action", required=True)
    ua = usub.add_parser("add")
    ua.add_argument("name")
    ua.add_argument("--admin", action="store_true")
    usub.add_parser("token").add_argument("name")
    usub.add_parser("list")

    m = sub.add_parser("maintainer", help="make a user maintainer of a project (everyone else is a developer)")
    m.add_argument("user")
    m.add_argument("project")
    m.add_argument("--remove", action="store_true")

    db = sub.add_parser("db", help="database operations")
    db.add_subparsers(dest="action", required=True).add_parser(
        "check", help="connect, migrate if needed, and report TLS, versions, latency and row counts")

    srv = sub.add_parser("serve", help="run the MCP server (HTTP also serves the web UI)")
    srv.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    srv.add_argument("--tls-cert")
    srv.add_argument("--tls-key")
    srv.add_argument("--insecure-http", action="store_true",
                     help="allow plain HTTP on a non-loopback host (tokens travel unencrypted)")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)

    if args.cmd == "serve":
        return _serve(p, args, settings)

    from lineage.service import Lineage
    app = Lineage(settings)
    me = app.local_principal()
    try:
        if args.cmd == "index":
            log = logging.getLogger("lineage.index")
            result = app.index_project(me, args.repo_path, args.project, progress=lambda p: log.info(
                "%s/%s files (%s indexed, %s unchanged, %s failed) %ss", p["indexed"] + p["unchanged"] + p["skipped"]
                + p["failed"], p["total"], p["indexed"], p["unchanged"], p["failed"], p["seconds"]))
        elif args.cmd == "search":
            result = app.search_code(me, args.query, args.top_n, args.project)
        elif args.cmd == "resolve":
            result = app.resolve_term(me, args.term, args.project)
        elif args.cmd == "projects":
            result = app.list_projects(me)
        elif args.cmd == "db":
            result = app.db_check()
        elif args.cmd == "maintainer":
            result = app.set_maintainer(me, args.user, args.project, not args.remove)
        elif args.action == "add":
            result = app.create_user(me, args.name, args.admin)
        elif args.action == "token":
            result = app.rotate_token(me, args.name)
        else:
            result = app.users(me)
    except (ValueError, PermissionError, LookupError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


def _serve(p: argparse.ArgumentParser, args, settings) -> int:
    from lineage.server import build_server, http_app
    if args.transport == "stdio":
        build_server(settings, http=False)[0].run("stdio")
        return 0

    tls = bool(args.tls_cert and args.tls_key)
    if args.host not in LOOPBACK and not tls and not args.insecure_http:
        p.error("non-loopback HTTP needs --tls-cert/--tls-key (or a TLS proxy and --insecure-http)")
    import uvicorn
    from lineage.web import register_web
    mcp, app = build_server(settings, http=True)
    register_web(mcp, app)
    if settings.db_url.startswith("sqlite:"):
        logging.getLogger("lineage").warning(
            "serving a team from SQLite: fine for a POC, but use a hosted Postgres (db_url=postgresql://...) "
            "so every member shares one database")
    scheme = "https" if tls else "http"
    logging.getLogger("lineage").info("web UI at %s://%s:%s/  MCP at /mcp", scheme, args.host, args.port)
    uvicorn.run(http_app(mcp, app, host=args.host), host=args.host, port=args.port,
                ssl_certfile=args.tls_cert, ssl_keyfile=args.tls_key, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
