import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_mcp_stdio_roundtrip(tmp_path):
    repo = tmp_path / "repos" / "svc"
    repo.mkdir(parents=True)
    (repo / "orders.py").write_text('"""Order intake."""\n\ndef place_order(cart):\n    return cart\n', encoding="utf-8")
    env = {**os.environ, "LINEAGE_DB_URL": f"sqlite:///{tmp_path / 'i.db'}",
           "LINEAGE_ALLOWED_ROOTS": str(tmp_path / "repos"), "LINEAGE_EMBEDDER": "hash"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "lineage.cli", "serve"], env=env)

    async def scenario():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            assert {"list_projects", "get_project_context", "search_code", "resolve_term",
                    "suggest_term", "onboard_project"} <= names

            assert not (await s.call_tool("onboard_project", {"repo_path": str(repo)})).is_error
            for _ in range(100):  # indexing runs as a background job
                projects = (await s.call_tool("list_projects", {})).structured_content["result"]
                if projects and projects[0]["last_job"]["status"] == "done":
                    break
                await anyio.sleep(0.05)
            assert projects[0]["last_job"]["status"] == "done"
            hits = (await s.call_tool("search_code", {"query": "place order"})).structured_content["result"]
            assert hits[0]["path"] == "orders.py"

            ctx = await s.call_tool("get_project_context", {"project": "svc"})
            assert json.loads(ctx.content[0].text) == {"project": "svc", "rules": [], "dictionary": []}

            denied = await s.call_tool("onboard_project", {"repo_path": str(tmp_path)})
            assert denied.is_error and "allowed_roots" in denied.content[0].text

    anyio.run(scenario)
