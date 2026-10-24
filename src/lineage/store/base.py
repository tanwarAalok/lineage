"""The single storage interface. Every DB access in Lineage goes through this.

Implementations: SQLiteStore (one local file) and PostgresStore (the client's own database).
All state lives in the configured database; Lineage keeps nothing anywhere else.

`systems` arguments are the caller's visibility scope: a sequence of project ids, or None for
unrestricted. Scoping is applied inside the query, never by post-filtering in the agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from lineage.models import ParsedFile

Scope = Sequence[str] | None


@dataclass
class ChunkRow:
    chunk_id: int
    system_id: str
    path: str
    file_summary: str
    start_line: int
    end_line: int
    symbol_name: str | None
    symbol_kind: str | None
    symbol_summary: str | None


@dataclass
class TermMapping:
    system_id: str
    term: str
    symbol_name: str
    path: str
    status: str  # proposed / approved / rejected
    origin: str  # inferred (by the tool) / human
    score: float | None
    author: str | None
    updated_at: str
    id: int | None = None


class Store(Protocol):
    def __enter__(self) -> "Store": ...
    def __exit__(self, *exc) -> None: ...

    # --- index lifecycle
    def bind_embedder(self, model_id: str, dim: int) -> None:
        """First call pins the model; later calls with a different model raise."""
    def ensure_system(self, system_id: str, name: str, root: str) -> None: ...
    def file_hashes(self, system_id: str) -> dict[str, str]: ...
    def is_indexed(self, system_id: str, path: str) -> bool: ...
    def replace_files(self, system_id: str, batch: list[tuple[ParsedFile, list[list[float]]]]) -> None:
        """Write a batch of parsed files (with one vector per chunk) atomically, in few round trips."""
    def delete_files(self, system_id: str, paths: Sequence[str]) -> None: ...
    def begin_bulk(self, large: bool) -> None:
        """Before indexing (large = thousands of new files): a dialect may relax durability or
        defer index upkeep for the derived index rows."""
    def end_bulk(self) -> None:
        """After indexing, also on failure: restore whatever begin_bulk relaxed."""

    # --- retrieval
    def keyword_search(self, terms: Sequence[str], systems: Scope, limit: int) -> list[tuple[int, float]]:
        """(chunk_id, score) best first; higher score is better."""
    def vector_search(self, vector: list[float], systems: Scope, limit: int) -> list[tuple[int, float]]:
        """(chunk_id, distance) best first; lower distance is better."""
    def chunks(self, chunk_ids: Sequence[int]) -> dict[int, ChunkRow]: ...
    def project_stats(self, system_id: str) -> dict: ...
    def systems(self, systems: Scope) -> list[dict]: ...
    def system_exists(self, system_id: str) -> bool: ...
    def set_description(self, system_id: str, description: str | None) -> bool:
        """One line saying what the system is, so agents can tell 'the backend' from 'the BFF'."""

    # --- term mappings (design vocabulary -> code)
    def mappings(self, term: str | None, systems: Scope) -> list[TermMapping]:
        """Mappings for one term (case-insensitive), or all when term is None. Approved first."""
    def save_mapping(self, m: TermMapping) -> None:
        """Upsert by (project, term, symbol, path). A proposal never replaces a reviewed mapping,
        and an inferred guess never replaces a human suggestion."""

    # --- people and access. Tokens arrive here already hashed.
    def create_user(self, name: str, is_admin: bool, token_hash: str) -> None: ...
    def set_token(self, name: str, token_hash: str) -> bool: ...
    def set_disabled(self, name: str, disabled: bool) -> bool: ...
    def user_by_token(self, token_hash: str) -> dict | None:
        """{'name', 'is_admin', 'roles': {project: 'maintainer'}} for an enabled user, else None."""
    def users(self) -> list[dict]: ...
    def set_role(self, user: str, system_id: str, role: str | None) -> bool:
        """role None removes the elevation. False when the user or project does not exist."""
    def members(self, system_id: str) -> list[dict]: ...

    # --- project dictionary and rules. Status: proposed / approved / rejected.
    # Item-level calls always take the project too, so an id from another project never matches.
    def terms(self, systems: Scope, term: str | None = None, status: str | None = None) -> list[dict]: ...
    def save_term(self, system_id: str, term: str, definition: str, status: str, author: str,
                  source: str | None = None) -> bool:
        """Upsert by (project, term). False when a proposal would overwrite an approved entry."""
    def rules(self, systems: Scope, status: str | None = None) -> list[dict]: ...
    def save_rule(self, system_id: str, title: str, body: str, status: str, author: str,
                  rule_id: int | None = None, source: str | None = None) -> bool: ...
    def review(self, kind: str, system_id: str, item_id: int, status: str, reviewer: str) -> bool:
        """kind: term / rule / mapping."""
    def delete_item(self, kind: str, system_id: str, item_id: int) -> bool: ...

    # --- background jobs (indexing)
    def create_job(self, system_id: str, kind: str, requested_by: str, owner: str) -> int | None:
        """None when the project already has an active job, on any server sharing the database."""
    def heartbeat(self, job_id: int) -> None: ...
    def update_job(self, job_id: int, status: str | None = None, progress: dict | None = None,
                   error: str | None = None) -> None: ...
    def jobs(self, systems: Scope, limit: int = 20) -> list[dict]: ...
    def interrupt_stale_jobs(self, older_than: str) -> int:
        """Jobs whose server stopped heartbeating before `older_than` (ISO time) can never finish."""

    # --- operations
    def diagnostics(self) -> dict:
        """Connection, TLS, versions, latency and row counts, for `lineage db check`."""

    # --- audit
    def audit(self, user: str, tool: str, args: dict) -> None: ...


def open_store(db_url: str, schema: str = "lineage", pool_size: int = 10, statement_timeout_s: int = 60) -> Store:
    """Hosted Postgres is the shared, team deployment; SQLite is for POCs and tests."""
    if db_url.startswith("sqlite:///"):
        from lineage.store.sqlite import SQLiteStore
        return SQLiteStore(db_url.removeprefix("sqlite:///"))
    if db_url.startswith(("postgresql://", "postgres://")):
        try:
            from lineage.store.postgres import PostgresStore
        except ImportError as e:
            raise RuntimeError("Postgres support needs `pip install lineage[pg]`") from e
        return PostgresStore(db_url, schema, pool_size, statement_timeout_s)
    raise ValueError("Unsupported db_url; use sqlite:///<path> or postgresql://...")
