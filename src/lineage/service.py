"""Use cases shared by the MCP server, the web UI and the CLI.

Every method takes the calling Principal; authorization, validation and audit live here once.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path

from lineage import search
from lineage.assets import render_assets
from lineage.auth import Principal, hash_token, new_token
from lineage.config import Settings
from lineage.indexer import Progress, index_repo
from lineage.ingest.embed import Embedder, make_embedder
from lineage.ingest.parse import decode, read_indexable
from lineage.security import is_secret_file, redact, resolve_repo_path, validate_system_id
from lineage.store.base import Store, TermMapping, open_store

log = logging.getLogger(__name__)

MAX_QUERY_CHARS = 2000
MAX_TERM_CHARS = 200
MAX_TEXT_CHARS = 8000
MAX_SOURCE_CHARS = 500
STATUSES = ("proposed", "approved", "rejected")
KINDS = ("term", "rule", "mapping")
ALL_PROJECTS = "*"
OTHER_SYSTEMS_GUIDE = (
    "Code of the other systems is NOT in this workspace; Lineage is the only way to see it. When a "
    "question involves another system (e.g. 'the backend', an upstream or downstream service, the "
    "frontend), never conclude the answer is unavailable: search_code with project='*' (or that "
    "project's id), then read_code on the hit's path with its system_id as project.")
MAX_READ_LINES = 300
USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
HEARTBEAT_S = 60
STALE_AFTER = timedelta(minutes=5)  # a job silent this long belongs to a server that died
INSTANCE = f"{socket.gethostname()}:{os.getpid()}"  # which server runs a job, for operators


class Lineage:
    def __init__(self, settings: Settings):
        self.settings = settings

    @cached_property
    def embedder(self) -> Embedder:  # loaded once; models are expensive
        return make_embedder(self.settings.embedder, self.settings.ollama_url)

    def open_store(self) -> Store:
        s = self.settings
        return open_store(s.db_url, s.db_schema, s.db_pool_size, s.db_statement_timeout_s)

    def _store(self, who: Principal, action: str, args: dict) -> Store:
        store = self.open_store()
        store.audit(who.name, action, args)
        return store

    def db_check(self) -> dict:
        """Admin diagnostics: is the configured database reachable, secure and ready?"""
        with self.open_store() as store:
            info = store.diagnostics()
        warnings = []
        if info["database"].startswith("sqlite"):
            warnings.append("SQLite is for POCs and tests; teams should use a hosted Postgres (db_url=postgresql://...)")
        if info.get("database") == "postgres":
            host = info["connection"].split("@", 1)[1].split(":", 1)[0]
            if not info["tls"] and host not in ("localhost", "127.0.0.1", "::1"):
                warnings.append("connection is not encrypted; use sslmode=require or verify-full")
            if info["round_trip_ms"] > 20:
                warnings.append(f"{info['round_trip_ms']} ms per round trip: run Lineage servers and CI close to the database")
        return {**info, "warnings": warnings}

    # --- identity

    def local_principal(self) -> Principal:
        """CLI and stdio MCP: whoever can run this process already holds the DB credentials."""
        return Principal(self.settings.user, is_admin=True)

    def authenticate(self, token: str | None) -> Principal | None:
        if not token or len(token) > 200:
            return None
        with self.open_store() as store:
            user = store.user_by_token(hash_token(token))
        return Principal(user["name"], user["is_admin"], user["roles"]) if user else None

    # --- projects and indexing

    def list_projects(self, who: Principal) -> list[dict]:
        with self._store(who, "list_projects", {}) as store:
            rows = store.systems(who.scope())
            jobs = {}
            for j in store.jobs(None, limit=200):
                jobs.setdefault(j["system_id"], j)
        return [{"id": r["id"], "description": r["description"], "files": r["files"], "indexed_at": r["indexed_at"],
                 "role": who.role(r["id"]), "last_job": _job_summary(jobs.get(r["id"]))} for r in rows]

    def set_description(self, who: Principal, project: str, description: str | None) -> dict:
        project = self._readable(who, project, "maintainer")
        description = _optional(description, MAX_SOURCE_CHARS, "description")
        with self._store(who, "set_description", {"project": project, "description": description}) as store:
            store.set_description(project, description)
        return {"project": project, "description": description}

    def index_project(self, who: Principal, repo_path: str, project: str | None = None,
                      progress: Progress | None = None) -> dict:
        """Index in the calling thread (CLI, CI). The web UI and agents use start_index_job."""
        root, project = self._authorize_index(who, repo_path, project)
        with self._store(who, "index_project", {"repo_path": str(root), "project": project}) as store:
            return self._run_index(store, root, project, progress)

    def start_index_job(self, who: Principal, repo_path: str, project: str | None = None) -> dict:
        """Queue indexing in the background and return the job at once: large repos take minutes."""
        root, project = self._authorize_index(who, repo_path, project)
        with self._store(who, "start_index_job", {"repo_path": str(root), "project": project}) as store:
            job_id = store.create_job(project, "index", who.name, INSTANCE)
        if job_id is None:  # the database enforces this across every server
            raise ValueError(f"project {project!r} is already being indexed")
        threading.Thread(target=self._job, args=(job_id, root, project), name=f"index-{project}", daemon=True).start()
        return {"job_id": job_id, "project": project, "status": "queued"}

    def reindex(self, who: Principal, project: str) -> dict:
        project = self._readable(who, project, "maintainer")
        with self.open_store() as store:
            rows = store.systems([project])
        if not rows:
            raise LookupError(f"project {project!r} not found")
        return self.start_index_job(who, rows[0]["root"], project)

    def jobs(self, who: Principal, project: str) -> list[dict]:
        project = self._readable(who, project)
        with self.open_store() as store:
            return store.jobs([project])

    def recover_jobs(self) -> None:
        """At server start: jobs whose server stopped heartbeating can never finish. Jobs of other
        live servers sharing the database keep running."""
        cutoff = (datetime.now(timezone.utc) - STALE_AFTER).isoformat(timespec="seconds")
        with self.open_store() as store:
            if n := store.interrupt_stale_jobs(cutoff):
                log.warning("marked %d abandoned index job(s) as interrupted; re-run them", n)

    def _authorize_index(self, who: Principal, repo_path: str, project: str | None) -> tuple[Path, str]:
        """New project: admin only. Re-indexing an existing one: its maintainers too."""
        root = resolve_repo_path(repo_path, self.settings.resolved_roots())
        project = validate_system_id(project or _slug(root.name))
        with self.open_store() as store:
            exists = store.system_exists(project)
        if exists:
            who.require(project, "maintainer")
        else:
            who.require_admin()
        return root, project

    def _run_index(self, store: Store, root: Path, project: str, progress: Progress | None) -> dict:
        # Nothing is written into the repository: copies there go stale when rules change, and agents
        # then read the stale copy. Fresh instruction files come from the web UI / assets API.
        return index_repo(root, project, self.settings, self.embedder, store, progress).to_dict()

    def _job(self, job_id: int, root: Path, project: str) -> None:
        stop = threading.Event()

        def beat() -> None:  # proves liveness even through long silent phases (index builds, slow models)
            while not stop.wait(HEARTBEAT_S):
                try:
                    with self.open_store() as s:
                        s.heartbeat(job_id)
                except Exception:
                    log.warning("heartbeat for job %s failed", job_id, exc_info=True)

        threading.Thread(target=beat, name=f"heartbeat-{job_id}", daemon=True).start()
        try:
            with self.open_store() as store:
                store.update_job(job_id, status="running")
                try:
                    result = self._run_index(store, root, project, lambda p: store.update_job(job_id, progress=p))
                    store.update_job(job_id, status="done", progress=result)
                except Exception as e:
                    log.exception("index job %s failed", job_id)
                    store.update_job(job_id, status="failed", error=f"{type(e).__name__}: {e}")
        finally:
            stop.set()

    def _readable(self, who: Principal, project: str, role: str = "developer") -> str:
        project = validate_system_id(project)
        who.require(project, role)
        return project

    def _scope(self, who: Principal, project: str | None) -> list[str] | None:
        if project == ALL_PROJECTS:  # cross-system questions: every project the caller may read
            return who.scope()
        return [self._readable(who, project)] if project else who.scope()

    # --- retrieval

    def search_code(self, who: Principal, query: str, top_n: int = 8, project: str | None = None) -> list[dict]:
        query = _text(query, MAX_QUERY_CHARS, "query")
        scope = self._scope(who, project)
        with self._store(who, "search_code", {"query": query, "top_n": top_n, "project": project}) as store:
            return [asdict(h) for h in search.search_code(store, self.embedder, query, scope, top_n)]

    def resolve_term(self, who: Principal, term: str, project: str | None = None) -> dict:
        term = _text(term, MAX_TERM_CHARS, "term")
        scope = self._scope(who, project)
        with self._store(who, "resolve_term", {"term": term, "project": project}) as store:
            return search.resolve_term(store, self.embedder, term, scope)

    def project_context(self, who: Principal, project: str) -> dict:
        project = self._readable(who, project)
        with self._store(who, "project_context", {"project": project}) as store:
            systems = store.systems(who.scope())
            return {
                "project": project,
                "description": next((s["description"] for s in systems if s["id"] == project), None),
                "rules": [{"title": r["title"], "rule": r["body"]} for r in store.rules([project], "approved")],
                "dictionary": [{"term": t["term"], "definition": t["definition"]}
                               for t in store.terms([project], status="approved")],
                "other_systems": [{"project": s["id"], "description": s["description"]}
                                  for s in systems if s["id"] != project],
                "other_systems_access": OTHER_SYSTEMS_GUIDE,
            }

    def read_code(self, who: Principal, path: str, project: str, start_line: int | None = None,
                  end_line: int | None = None) -> dict:
        """Code from any project's checkout on the server, so agents can read upstream systems that
        are not open in their editor. Only files Lineage indexed (never ignored or secret files),
        never outside the project root, secrets redacted, at most MAX_READ_LINES lines."""
        project = self._readable(who, project)
        path = _text(path, 1000, "path").replace("\\", "/").lstrip("/")
        args = {"project": project, "path": path, "start_line": start_line, "end_line": end_line}
        with self._store(who, "read_code", args) as store:
            rows = store.systems([project])
            if not rows or not store.is_indexed(project, path):
                raise LookupError(f"{path!r} is not an indexed file of {project!r}; use a path from search_code")
        root = Path(rows[0]["root"]).resolve()
        file = (root / path).resolve()
        if not file.is_relative_to(root) or is_secret_file(path):
            raise PermissionError(f"{path!r} cannot be read")
        data = read_indexable(file, self.settings.max_file_bytes)
        if data is None:
            raise LookupError(f"{path!r} is no longer readable on the server; re-index {project!r}")
        lines = redact(decode(data))[0].splitlines()
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        return {"project": project, "path": path, "start_line": start, "end_line": end, "total_lines": len(lines),
                "code": "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))}

    def assets(self, who: Principal, project: str) -> dict[str, str]:
        project = self._readable(who, project)
        with self._store(who, "assets", {"project": project}) as store:
            return render_assets(store, project)

    # --- dictionary, rules, code links

    def terms(self, who: Principal, project: str) -> list[dict]:
        project = self._readable(who, project)
        with self._store(who, "terms", {"project": project}) as store:
            return store.terms([project])

    def save_term(self, who: Principal, project: str, term: str, definition: str, approve: bool = False,
                  source: str | None = None) -> dict:
        """Developers (and every agent call) propose; a maintainer or admin with approve=True writes directly.
        `source` says where the definition came from, so reviewers can judge requirements that may be wrong."""
        project = self._readable(who, project, "maintainer" if approve else "developer")
        term, definition = _text(term, MAX_TERM_CHARS, "term"), _text(definition, MAX_TEXT_CHARS, "definition")
        source = _optional(source, MAX_SOURCE_CHARS, "source")
        status = "approved" if approve else "proposed"
        with self._store(who, "save_term", {"project": project, "term": term, "status": status}) as store:
            if not store.save_term(project, term, definition, status, who.name, source):
                raise ValueError(f"{term!r} is already approved; ask a maintainer to change it")
        return {"project": project, "term": term, "status": status}

    def link_term(self, who: Principal, project: str, term: str, symbol: str, path: str) -> dict:
        """Suggest which code implements a term. Lands as a proposal for maintainers to review."""
        project = self._readable(who, project)
        term = _text(term, MAX_TERM_CHARS, "term")
        symbol, path = _text(symbol, MAX_TERM_CHARS, "symbol"), _text(path, 1000, "path")
        with self._store(who, "link_term", {"project": project, "term": term, "symbol": symbol, "path": path}) as store:
            store.save_mapping(TermMapping(project, term, symbol, path, "proposed", "human", None, who.name, ""))
        return {"project": project, "term": term, "symbol": symbol, "path": path, "status": "proposed"}

    def mappings(self, who: Principal, project: str) -> list[dict]:
        project = self._readable(who, project)
        with self._store(who, "mappings", {"project": project}) as store:
            return [asdict(m) for m in store.mappings(None, [project])]

    def rules(self, who: Principal, project: str) -> list[dict]:
        project = self._readable(who, project)
        with self._store(who, "rules", {"project": project}) as store:
            return store.rules([project])

    def save_rule(self, who: Principal, project: str, title: str, body: str, rule_id: int | None = None,
                  approve: bool = False, source: str | None = None) -> dict:
        """Developers propose new rules. Editing an existing rule, or approving, is for maintainers."""
        need = "maintainer" if (approve or rule_id is not None) else "developer"
        project = self._readable(who, project, need)
        title, body = _text(title, MAX_TERM_CHARS, "title"), _text(body, MAX_TEXT_CHARS, "rule")
        source = _optional(source, MAX_SOURCE_CHARS, "source")
        status = "approved" if approve else "proposed"
        with self._store(who, "save_rule", {"project": project, "title": title, "id": rule_id}) as store:
            if not store.save_rule(project, title, body, status, who.name, rule_id, source):
                raise LookupError(f"rule {rule_id} not found in {project}")
        return {"project": project, "title": title, "status": status}

    def review(self, who: Principal, project: str, kind: str, item_id: int, status: str) -> dict:
        project = self._readable(who, project, "maintainer")
        kind, status = _choice(kind, KINDS, "kind"), _choice(status, STATUSES, "status")
        with self._store(who, "review", {"project": project, "kind": kind, "id": item_id, "status": status}) as store:
            if not store.review(kind, project, int(item_id), status, who.name):
                raise LookupError(f"{kind} {item_id} not found in {project}")
        return {"project": project, "kind": kind, "id": item_id, "status": status}

    def delete(self, who: Principal, project: str, kind: str, item_id: int) -> dict:
        project = self._readable(who, project, "maintainer")
        kind = _choice(kind, KINDS, "kind")
        with self._store(who, "delete", {"project": project, "kind": kind, "id": item_id}) as store:
            if not store.delete_item(kind, project, int(item_id)):
                raise LookupError(f"{kind} {item_id} not found in {project}")
        return {"deleted": True}

    # --- people (admin). Everyone is a developer everywhere; admins grant maintainer per project.

    def maintainers(self, who: Principal, project: str) -> list[dict]:
        project = self._readable(who, project)
        with self._store(who, "maintainers", {"project": project}) as store:
            return store.members(project)

    def set_maintainer(self, who: Principal, user: str, project: str, maintainer: bool) -> dict:
        who.require_admin()
        project = validate_system_id(project)
        with self._store(who, "set_maintainer", {"user": user, "project": project, "maintainer": maintainer}) as store:
            if not store.set_role(user, project, "maintainer" if maintainer else None):
                raise LookupError(f"unknown user {user!r} or project {project!r}")
        return {"user": user, "project": project, "role": "maintainer" if maintainer else "developer"}

    def users(self, who: Principal) -> list[dict]:
        who.require_admin()
        with self._store(who, "users", {}) as store:
            return store.users()

    def create_user(self, who: Principal, name: str, is_admin: bool = False) -> dict:
        """Returns the user's token. It is shown once and never stored in plain text."""
        who.require_admin()
        if not USER_RE.fullmatch(name or ""):
            raise ValueError("user name: 1-64 of letters, digits, '.', '_', '@', '-'")
        token = new_token()
        with self._store(who, "create_user", {"name": name, "is_admin": is_admin}) as store:
            if any(u["name"] == name for u in store.users()):
                raise ValueError(f"user {name!r} already exists")
            store.create_user(name, is_admin, hash_token(token))
        return {"name": name, "is_admin": is_admin, "token": token}

    def rotate_token(self, who: Principal, name: str) -> dict:
        who.require_admin()
        token = new_token()
        with self._store(who, "rotate_token", {"name": name}) as store:
            if not store.set_token(name, hash_token(token)):
                raise LookupError(f"unknown user {name!r}")
        return {"name": name, "token": token}

    def set_disabled(self, who: Principal, name: str, disabled: bool) -> dict:
        who.require_admin()
        if name == who.name and disabled:
            raise ValueError("you cannot disable yourself")
        with self._store(who, "set_disabled", {"name": name, "disabled": disabled}) as store:
            if not store.set_disabled(name, disabled):
                raise LookupError(f"unknown user {name!r}")
        return {"name": name, "disabled": disabled}


def _job_summary(job: dict | None) -> dict | None:
    if job is None:
        return None
    return {k: job[k] for k in ("id", "status", "progress", "error", "created_at", "finished_at")}


def _text(value: str, limit: int, name: str) -> str:
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{name} longer than {limit} characters")
    return value


def _optional(value: str | None, limit: int, name: str) -> str | None:
    return _text(value, limit, name) if isinstance(value, str) and value.strip() else None


def _choice(value: str, allowed: tuple[str, ...], name: str) -> str:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}")
    return value


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-.")[:64] or "project"
