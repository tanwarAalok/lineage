"""Human-readable per-project assets: instruction files carrying the project's approved rules,
dictionary and the map of other systems. Rendered fresh on demand (web UI download / assets API)
for teams to commit; Lineage never writes them into repositories itself, where they would go stale.
"""

from __future__ import annotations

import yaml

from lineage.store.base import Store

TOOL_GUIDE = """\
## How to explore this codebase

The **lineage** MCP server indexes this repository and every other system listed above. Prefer
its tools to workspace search, and always use them for anything outside this repository:

- `get_project_context()` - this project's rules, dictionary and the other systems. Call it first.
- `search_code(query)` - searches ALL systems by default (pass `project` to narrow); returns the
  few relevant files, each with its `system_id` and a one-line meaning.
- `read_code(path, project)` - read code of any system, including ones not in this workspace.
- `resolve_term(term)` - what a design-document (HLD/LLD) term means and which code implements it.
  `proposed` code links are unreviewed guesses.
- `suggest_term(term, definition)` - only with the developer's explicit input; a maintainer reviews it.
- Open only the files these tools return.
"""


def render_assets(store: Store, system_id: str) -> dict[str, str]:
    stats = store.project_stats(system_id)
    rules = store.rules([system_id], status="approved")
    terms = store.terms([system_id], status="approved")
    links = [m for m in store.mappings(None, [system_id]) if m.status == "approved"]

    rule_md = "\n".join(f"- **{r['title']}** - {r['body']}" for r in rules) or "- (none yet)"
    term_md = "\n".join(f"- **{t['term']}** - {t['definition']}" for t in terms) or "- (none yet)"
    body = (f"# {system_id}\n\n{_systems(store, system_id)}## Project rules\n\n{rule_md}\n\n## Dictionary\n\n{term_md}\n\n"
            f"{_overview(system_id, stats)}\n{TOOL_GUIDE}")
    glossary = {
        "project": system_id,
        "terms": [
            {"term": t["term"], "definition": t["definition"],
             "code": [{"symbol": m.symbol_name, "path": m.path} for m in links if m.term.lower() == t["term"].lower()]}
            for t in terms
        ],
    }
    return {
        # One body, the file name each agent looks for: Codex/Cursor/most agents, Copilot, Claude, Gemini.
        "AGENTS.md": body,
        "copilot-instructions.md": body,
        "CLAUDE.md": body,
        "GEMINI.md": body,
        "glossary.yaml": yaml.safe_dump(glossary, sort_keys=False, allow_unicode=True),
    }


def _systems(store: Store, system_id: str) -> str:
    """Where this repo sits among the other systems. Agents need this up front: without it they
    answer cross-system questions from this repo alone and conclude the answer is unavailable."""
    def line(s: dict) -> str:
        return f"- `{s['id']}`" + (f" - {s['description']}" if s["description"] else "")

    systems = store.systems(None)
    me = next((s for s in systems if s["id"] == system_id), None)
    others = "\n".join(line(s) for s in systems if s["id"] != system_id) or "- (none)"
    this = f"This repository is **`{system_id}`**" + (f": {me['description']}" if me and me["description"] else "")
    return (f"## Systems\n\n{this.rstrip('.')}.\n\nOther systems, also indexed by Lineage:\n\n{others}\n\n"
            "Their code is **not in this workspace**. For any question about another system (the backend, "
            "an upstream or downstream service, the frontend), do not answer from this repository alone and "
            'never conclude the answer is unavailable: call `search_code` with `project="*"`, then `read_code` '
            "on the hit, passing the hit's `system_id` as `project`.\n\n")


def _overview(system_id: str, stats: dict) -> str:
    langs = ", ".join(f"{k} ({v})" for k, v in stats["languages"].items()) or "none"
    dirs = "\n".join(f"- `{d}/` - {n} files" for d, n in stats["top_dirs"].items())
    hubs = "\n".join(
        f"- `{h['name']}` ({h['kind']}, `{h['path']}`) - called from {h['callers']} places"
        for h in stats["hubs"]
    ) or "- (no call graph for these languages)"
    return (
        f"## Codebase\n\nProject `{system_id}`: {stats['files']} files, {stats['symbols']} symbols.\n"
        f"Languages: {langs}.\n\n"
        f"### Layout\n\n{dirs}\n\n"
        f"### Most-called symbols (change with care)\n\n{hubs}\n"
    )
