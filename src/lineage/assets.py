"""Human-readable per-project assets: instruction files carrying the project's approved rules and
dictionary, meant to be reviewed and committed to the target repo.

Rendered on demand (web UI download, `get_project_context`) and written to <repo>/.lineage/ at
onboarding, so an existing AGENTS.md / copilot-instructions.md is never clobbered.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from lineage.store.base import Store

TOOL_GUIDE = """\
## How to explore this codebase

This repo is indexed by the **lineage** MCP server. Use its tools instead of grepping or
opening files to find where things live:

- `get_project_context()` - this project's rules and dictionary. Call it first.
- `search_code(query)` - hybrid semantic + keyword search; returns the few relevant files with
  a one-line meaning each.
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
    body = (f"# {system_id}\n\n## Project rules\n\n{rule_md}\n\n## Dictionary\n\n{term_md}\n\n"
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
        "AGENTS.md": body,
        "copilot-instructions.md": body,
        "glossary.yaml": yaml.safe_dump(glossary, sort_keys=False, allow_unicode=True),
    }


def write_assets(root: Path, system_id: str, store: Store) -> list[str]:
    out_dir = root / ".lineage"
    if out_dir.is_symlink():
        raise PermissionError(f"{out_dir} is a symlink; refusing to write through it")
    out_dir.mkdir(exist_ok=True)
    files = render_assets(store, system_id)
    for name, content in files.items():
        _atomic_write(out_dir / name, content)
    return [str(out_dir / n) for n in files]


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


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
