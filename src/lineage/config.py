"""Settings: defaults < YAML file (LINEAGE_CONFIG) < LINEAGE_* env vars.

Moving local -> hosted is meant to be a change here (db_url, embedder), not in code.
"""

from __future__ import annotations

import dataclasses
import getpass
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Settings:
    # Where ALL Lineage state lives: index, people, rules, dictionary, jobs, audit log.
    # Teams: postgresql://... (the client's hosted database, shared by every Lineage server and CI).
    # POC / tests only: sqlite:///<path> on one machine.
    db_url: str = f"sqlite:///{Path.home() / '.lineage' / 'index.db'}"
    # Postgres only: Lineage keeps its tables in this schema, apart from the client's data.
    db_schema: str = "lineage"
    # Postgres only: connections per Lineage server, and a safety limit for any one query.
    db_pool_size: int = 10
    db_statement_timeout_s: int = 60
    # "hash" (zero-dependency, lexical), "st:<model>" (sentence-transformers), "ollama:<model>".
    # Pinned: everyone and CI must use the same value, or the index refuses to mix vectors.
    embedder: str = "hash"
    ollama_url: str = "http://localhost:11434"
    # onboard_project only accepts repos under these roots. Empty = refuse everything.
    allowed_roots: tuple[str, ...] = ()
    max_file_bytes: int = 2_000_000
    # Parser processes for indexing. 0 = one per CPU core (max 8).
    index_workers: int = 0
    ignore_dirs: tuple[str, ...] = (
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
        "dist", "build", "target", "bin", "obj", ".idea", ".vscode", ".lineage",
    )
    ignore_globs: tuple[str, ...] = ("*.min.js", "*.map", "*.lock", "*.svg", "*.ipynb")
    # Identity of the local admin for the CLI and stdio MCP. HTTP callers authenticate with tokens.
    user: str = field(default_factory=lambda: os.environ.get("LINEAGE_USER") or getpass.getuser())

    def resolved_roots(self) -> list[Path]:
        return [Path(r).expanduser().resolve() for r in self.allowed_roots]


_ENV = {
    "LINEAGE_DB_URL": "db_url",
    "LINEAGE_DB_SCHEMA": "db_schema",
    "LINEAGE_DB_POOL_SIZE": "db_pool_size",
    "LINEAGE_EMBEDDER": "embedder",
    "LINEAGE_OLLAMA_URL": "ollama_url",
    "LINEAGE_ALLOWED_ROOTS": "allowed_roots",  # os.pathsep-separated
}


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = path or os.environ.get("LINEAGE_CONFIG")
    values: dict = {}
    if path:
        values = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    known = {f.name for f in dataclasses.fields(Settings)}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"Unknown config keys: {sorted(unknown)}")
    for env, key in _ENV.items():
        if env in os.environ:
            raw = os.environ[env]
            if key == "allowed_roots":
                values[key] = [p for p in raw.split(os.pathsep) if p]
            elif key == "db_pool_size":
                values[key] = int(raw)
            else:
                values[key] = raw
    for key in ("allowed_roots", "ignore_dirs", "ignore_globs"):
        if key in values:
            values[key] = tuple(values[key] or ())
    return Settings(**values)
