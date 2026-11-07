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

from lineage import crs, search
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
CR_STATUSES = ("proposed", "in_progress", "implemented", "cancelled")
CR_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-\d{1,9}$")  # CR-3001, SHOP-12
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
        if (info.get("embedder") or "").startswith("hash"):
            warnings.append("index uses the 'hash' embedder (word matching only): vague, business-language "
                            "requirements rarely find the right code. Set embedder to st:sentence-transformers/"
                            "all-MiniLM-L6-v2 and rebuild the index.")
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

    # --- change requests. A CR spans systems: anyone reads, admins/maintainers register them,
    # developers and agents propose contracts and decisions, the provider's maintainers agree them.

    def save_cr(self, who: Principal, cr_id: str, title: str, summary: str | None = None, status: str = "proposed",
                depends_on: list[str] | tuple = (), references: list[str] | tuple = (), source: str | None = None) -> dict:
        _require_cr_editor(who)
        cr_id, title = _cr_id(cr_id), _text(title, MAX_TERM_CHARS, "title")
        summary, status = _optional(summary, MAX_TEXT_CHARS, "summary"), _choice(status, CR_STATUSES, "status")
        source = _optional(source, MAX_SOURCE_CHARS, "source")
        links = [(_cr_id(x), "depends_on") for x in depends_on] + [(_cr_id(x), "references") for x in references]
        if any(other == cr_id for other, _ in links):
            raise ValueError(f"{cr_id} cannot depend on or reference itself")
        args = {"cr": cr_id, "status": status, "depends_on": list(depends_on), "references": list(references)}
        with self._store(who, "save_cr", args) as store:
            store.save_cr(cr_id, title, summary, status, who.name, source)
            store.set_cr_links(cr_id, links)
        return {"cr": cr_id, "title": title, "status": status, "depends_on": [o for o, k in links if k == "depends_on"],
                "references": [o for o, k in links if k == "references"]}

    def propose_cr(self, who: Principal, cr_id: str, title: str, summary: str, depends_on: list[str] | tuple = (),
                   references: list[str] | tuple = (), source: str | None = None) -> dict:
        """Record a CR that exists in Jira, Confluence or a design document but not yet in Lineage, so its
        requirements and dependencies become shared memory. Anyone may propose; it lands with status
        `proposed` and an admin or maintainer confirms or edits it. The author may revise their own
        proposal until it is confirmed; nobody can overwrite anyone else's CR."""
        cr_id, title = _cr_id(cr_id), _text(title, MAX_TERM_CHARS, "title")
        summary, source = _text(summary, MAX_TEXT_CHARS, "summary"), _optional(source, MAX_SOURCE_CHARS, "source")
        deps = list(dict.fromkeys(_cr_id(x) for x in depends_on))
        refs = list(dict.fromkeys(_cr_id(x) for x in references if _cr_id(x) not in deps))
        # Safety net: CR ids the summary mentions are at least linked as references.
        mentioned = [m.upper() for m in re.findall(self.settings.cr_pattern, summary, re.IGNORECASE)]
        unlisted = [m for m in dict.fromkeys(mentioned) if m != cr_id and m not in deps and m not in refs]
        refs += unlisted
        if cr_id in deps + refs:
            raise ValueError(f"{cr_id} cannot depend on or reference itself")
        links = [(d, "depends_on") for d in deps] + [(r, "references") for r in refs]
        with self._store(who, "propose_cr", {"cr": cr_id, "depends_on": deps, "references": refs, "source": source}) as store:
            existing = store.get_cr(cr_id)
            if existing is not None and not (existing["status"] == "proposed" and existing["created_by"] == who.name):
                raise ValueError(f"{cr_id} is already registered by {existing['created_by']}; read it with get_cr")
            store.save_cr(cr_id, title, summary, "proposed", who.name, source)
            store.set_cr_links(cr_id, links)
        note = ("Recorded as a proposal; an admin or maintainer confirms it. Call get_cr now for the context of "
                "everything it builds on.")
        if unlisted:
            note += (f" The summary mentions {', '.join(unlisted)}, recorded only as reference(s). If {cr_id} is built "
                     f"on top of {'it' if len(unlisted) == 1 else 'them'}, call propose_cr again with depends_on.")
        return {"cr": cr_id, "status": "proposed", "revised": existing is not None, "depends_on": deps,
                "references": refs, "note": note}

    def list_crs(self, who: Principal, status: str | None = None) -> list[dict]:
        status = _choice(status, CR_STATUSES, "status") if status else None
        with self._store(who, "list_crs", {"status": status}) as store:
            rows = store.crs(status)
            return [{"cr": r["id"], "title": r["title"], "status": r["status"], "summary": r["summary"],
                     "depends_on": [l["other_id"] for l in store.cr_links(r["id"])
                                    if l["cr_id"] == r["id"] and l["kind"] == "depends_on"],
                     "updated_at": r["updated_at"]} for r in rows]

    def cr_context(self, who: Principal, cr_id: str, project: str | None = None) -> dict:
        """`project`: the asking repository, whose own drift is listed first with its files."""
        cr_id = _cr_id(cr_id)
        project = validate_system_id(project) if project and project != ALL_PROJECTS else None
        with self._store(who, "cr_context", {"cr": cr_id, "project": project}) as store:
            return crs.build_context(store, cr_id, project)

    def contracts(self, who: Principal, cr_id: str | None = None, provider: str | None = None,
                  name: str | None = None, history: bool = False) -> list[dict]:
        cr_id = _cr_id(cr_id) if cr_id else None
        provider = validate_system_id(provider) if provider else None
        args = {"cr": cr_id, "provider": provider, "name": name, "history": history}
        with self._store(who, "contracts", args) as store:
            return crs.contracts_view(store, cr_id, provider, name, history)

    def propose_contract(self, who: Principal, cr_id: str, provider: str, name: str, spec: str,
                         consumers: str | None = None, source: str | None = None, approve: bool = False) -> dict:
        """A new version of a contract for a CR. Developers and agents propose; the provider's
        maintainers (or an admin) agree it. Earlier versions stay as history."""
        cr_id, provider = _cr_id(cr_id), validate_system_id(provider)
        if approve:
            who.require(provider, "maintainer")
        name, spec = _text(name, MAX_TERM_CHARS, "name"), _text(spec, MAX_TEXT_CHARS, "spec")
        consumers = _optional(consumers, MAX_SOURCE_CHARS, "consumers")
        source = _optional(source, MAX_SOURCE_CHARS, "source")
        status = "agreed" if approve else "proposed"
        with self._store(who, "propose_contract", {"cr": cr_id, "provider": provider, "name": name, "status": status}) as store:
            if store.get_cr(cr_id) is None:
                raise LookupError(f"{cr_id} is not registered; ask an admin or maintainer to register it first")
            version = store.add_contract(cr_id, provider, name, spec, consumers, source, status, who.name)
        return {"cr": cr_id, "provider": provider, "name": name, "version": version, "status": status}

    def review_contract(self, who: Principal, contract_id: int, status: str) -> dict:
        status = _choice(status, ("agreed", "rejected"), "status")
        with self._store(who, "review_contract", {"id": contract_id, "status": status}) as store:
            contract = store.get_contract(int(contract_id))
            if contract is None:
                raise LookupError(f"contract {contract_id} not found")
            who.require(contract["provider"], "maintainer")
            store.review_contract(int(contract_id), status, who.name)
        return {"id": contract_id, "cr": contract["cr_id"], "status": status}

    def log_decision(self, who: Principal, cr_id: str, question: str, decision: str, system: str | None = None,
                     source: str | None = None, approve: bool = False) -> dict:
        cr_id = _cr_id(cr_id)
        system = validate_system_id(system) if system else None
        if approve:
            who.require(system, "maintainer") if system else _require_cr_editor(who)
        question, decision = _text(question, MAX_TEXT_CHARS, "question"), _text(decision, MAX_TEXT_CHARS, "decision")
        source = _optional(source, MAX_SOURCE_CHARS, "source")
        status = "approved" if approve else "proposed"
        with self._store(who, "log_decision", {"cr": cr_id, "system": system, "status": status}) as store:
            if store.get_cr(cr_id) is None:
                raise LookupError(f"{cr_id} is not registered; ask an admin or maintainer to register it first")
            store.add_decision(cr_id, system, question, decision, source, status, who.name)
        return {"cr": cr_id, "status": status}

    def review_decision(self, who: Principal, decision_id: int, status: str) -> dict:
        status = _choice(status, ("approved", "rejected"), "status")
        with self._store(who, "review_decision", {"id": decision_id, "status": status}) as store:
            d = store.get_decision(int(decision_id))
            if d is None:
                raise LookupError(f"decision {decision_id} not found")
            who.require(d["system_id"], "maintainer") if d["system_id"] else _require_cr_editor(who)
            store.review_decision(int(decision_id), status, who.name)
        return {"id": decision_id, "cr": d["cr_id"], "status": status}

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


def _cr_id(value: str) -> str:
    value = (value or "").strip().upper() if isinstance(value, str) else ""
    if not CR_ID_RE.fullmatch(value):
        raise ValueError(f"not a change-request id: {value!r} (expected e.g. CR-3001)")
    return value


def _require_cr_editor(who: Principal) -> None:
    """Registering CRs and approving CR-wide decisions: admins, or maintainers of any project."""
    if not (who.is_admin or who.roles):
        raise PermissionError(f"{who.name} needs to be an admin or a maintainer to do this")


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
