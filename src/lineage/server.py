"""MCP server. Agents call these tools instead of reading the index or the repo directly.

stdio: a local, single-user process; the caller is the local admin.
HTTP: every tool call authenticates with `Authorization: Bearer <token>`; the default project
comes from the `X-Lineage-Project` header (set per repo in .vscode/mcp.json).
"""

from __future__ import annotations

from typing import Callable, TypeVar

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from lineage.auth import Principal
from lineage.config import Settings
from lineage.service import Lineage

T = TypeVar("T")

# Anticipated failures go back to the agent with their message; anything else stays a
# generic error so internals never leak to the client.
EXPECTED_ERRORS = (ValueError, PermissionError, LookupError, FileNotFoundError, NotADirectoryError, RuntimeError)

INSTRUCTIONS = (
    "Code intelligence for large multi-system codebases. Call get_project_context first for the "
    "project's rules and dictionary, then search_code before opening files. resolve_term maps "
    "design-document vocabulary to code; `proposed` results are unreviewed guesses."
)


def bearer(authorization: str | None) -> str | None:
    if authorization and authorization[:7].lower() == "bearer ":
        return authorization[7:].strip()
    return None


def build_server(settings: Settings, http: bool = False) -> tuple[MCPServer, Lineage]:
    app = Lineage(settings)
    app.recover_jobs()
    _ = app.embedder  # load the model at startup, not on the first call
    mcp = MCPServer("lineage", instructions=INSTRUCTIONS)

    def caller(ctx: Context) -> tuple[Principal, str | None]:
        if not http:
            return app.local_principal(), None
        headers = ctx.headers or {}
        who = app.authenticate(bearer(headers.get("authorization")))
        if who is None:
            raise PermissionError("missing or invalid Lineage token (Authorization: Bearer ...)")
        return who, headers.get("x-lineage-project") or None

    async def run(ctx: Context, fn: Callable[..., T], *args, project: str | None = None, **kw) -> T:
        def call() -> T:
            who, default_project = caller(ctx)
            return fn(who, *args, project=project or default_project, **kw)
        try:
            return await anyio.to_thread.run_sync(call)
        except EXPECTED_ERRORS as e:
            raise ToolError(str(e)) from e

    def need(project: str | None) -> str:
        if not project:
            raise ValueError("no project: pass `project` or set the X-Lineage-Project header")
        return project

    @mcp.tool()
    async def list_projects(ctx: Context) -> list[dict]:
        """Projects you can use, your role on each, and the latest indexing job's status."""
        return await run(ctx, lambda who, project: app.list_projects(who))

    @mcp.tool()
    async def get_project_context(ctx: Context, project: str | None = None) -> dict:
        """The project's approved rules and dictionary. Call this before starting a task and
        follow the rules."""
        return await run(ctx, lambda who, project: app.project_context(who, need(project)), project=project)

    @mcp.tool()
    async def search_code(ctx: Context, query: str, top_n: int = 8, project: str | None = None) -> list[dict]:
        """Hybrid semantic + keyword search. Returns the top files (max 25), each with a one-line
        meaning, matching symbols and the best-matching line range."""
        return await run(ctx, lambda who, project: app.search_code(who, query, top_n, project), project=project)

    @mcp.tool()
    async def resolve_term(ctx: Context, term: str, project: str | None = None) -> dict:
        """What a design (HLD/LLD) term means in this project, and which code implements it.
        Approved entries win; otherwise inferred code links come back as `proposed`."""
        return await run(ctx, lambda who, project: app.resolve_term(who, term, project), project=project)

    @mcp.tool()
    async def suggest_term(ctx: Context, term: str, definition: str, source: str | None = None,
                           symbol: str | None = None, path: str | None = None, project: str | None = None) -> dict:
        """Suggest a dictionary entry (and optionally the code that implements it). Only call this
        with the developer's explicit input. `source` says where it comes from (document + section,
        meeting, person). It is stored as a proposal for a maintainer to review."""
        def go(who: Principal, project: str | None) -> dict:
            result = app.save_term(who, need(project), term, definition, source=source)
            if symbol and path:
                result["code_link"] = app.link_term(who, project, term, symbol, path)
            return result
        return await run(ctx, go, project=project)

    @mcp.tool()
    async def onboard_project(ctx: Context, repo_path: str, project: str | None = None) -> dict:
        """Admins: index a new repository. Maintainers: re-index theirs (incremental: only changed
        files are re-processed). Runs in the background and returns a job; list_projects shows its
        progress. repo_path is a path on the Lineage server, under an allowed root."""
        # Never the header's default project: that would index this repo into another project.
        explicit = project
        return await run(ctx, lambda who, project: app.start_index_job(who, repo_path, explicit))

    return mcp, app
