"""SQL shared by every relational store. Dialects override connection, DDL, full-text search,
vector search and bulk inserts; everything else is portable SQL written once here.

Queries use `?` placeholders; dialects translate. Rows come back as dicts.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any, Sequence

from lineage.models import Chunk, ParsedFile
from lineage.store.base import ChunkRow, Scope, TermMapping

SCHEMA_VERSION = 5
FIRST_SHARED_VERSION = 3  # older schemas only ever existed in local pre-release POCs

# Shared databases hold people's rules and dictionaries, so schema changes are migrations, never
# rebuilds. MIGRATIONS[v] upgrades v -> v+1. COMMON_DDL always describes the latest schema.
MIGRATIONS: dict[int, list[str]] = {
    3: [
        # Jobs gain a heartbeat so several servers can share one database safely.
        "update jobs set status = 'interrupted' where status in ('queued', 'running')",
        "alter table jobs add column heartbeat_at text",
        "alter table jobs add column owner text",
        "create unique index if not exists jobs_one_active on jobs(system_id)"
        " where status in ('queued', 'running')",
    ],
    # What each system is ("backend: orders, pricing"), so agents can map "the backend" to a project.
    4: ["alter table systems add column description text"],
}

# {id} = auto-increment primary key, {ref} = integer foreign key type, per dialect.
COMMON_DDL = """
create table if not exists meta(key text primary key, value text not null);
create table if not exists systems(id text primary key, name text not null, root text not null, description text);
create table if not exists files(
    id {id},
    system_id text not null references systems(id) on delete cascade,
    path text not null, hash text not null, language text not null, summary text not null,
    indexed_at text not null,
    unique(system_id, path));
create table if not exists symbols(
    id {id},
    file_id {ref} not null references files(id) on delete cascade,
    system_id text not null, name text not null, kind text not null,
    start_line integer not null, end_line integer not null, signature text, summary text);
create index if not exists symbols_name on symbols(system_id, name);
create index if not exists symbols_file on symbols(file_id);
-- Callee stored by name: survives re-indexing of the callee's file. Resolved at query time.
create table if not exists edges(
    src_symbol {ref} not null references symbols(id) on delete cascade,
    dst_name text not null, type text not null);
create index if not exists edges_src on edges(src_symbol);
create index if not exists edges_dst on edges(dst_name);
-- term_key = lower(term): portable case-insensitive uniqueness.
create table if not exists term_mappings(
    id {id}, system_id text not null references systems(id) on delete cascade,
    term text not null, term_key text not null, symbol_name text not null, path text not null,
    status text not null check(status in ('proposed', 'approved', 'rejected')),
    origin text not null check(origin in ('inferred', 'human')),
    score real, author text, updated_at text not null,
    unique(system_id, term_key, symbol_name, path));
-- Project dictionary. `source` records where a definition came from (HLD section, meeting,
-- person) because requirements are scattered and not always right.
create table if not exists terms(
    id {id}, system_id text not null references systems(id) on delete cascade,
    term text not null, term_key text not null, definition text not null, source text,
    status text not null check(status in ('proposed', 'approved', 'rejected')),
    author text not null, reviewer text, updated_at text not null,
    unique(system_id, term_key));
create table if not exists rules(
    id {id}, system_id text not null references systems(id) on delete cascade,
    title text not null, body text not null, source text,
    status text not null check(status in ('proposed', 'approved', 'rejected')),
    author text not null, reviewer text, updated_at text not null);
create table if not exists users(
    id {id}, name text not null unique, is_admin integer not null default 0,
    token_hash text not null unique, disabled integer not null default 0, created_at text not null);
-- Everyone is a developer on every project; this table only records elevations.
create table if not exists memberships(
    user_id {ref} not null references users(id) on delete cascade,
    system_id text not null references systems(id) on delete cascade,
    role text not null check(role in ('maintainer')),
    primary key(user_id, system_id));
create table if not exists jobs(
    id {id}, system_id text not null, kind text not null,
    status text not null check(status in ('queued', 'running', 'done', 'failed', 'interrupted')),
    requested_by text not null, created_at text not null, started_at text, finished_at text,
    progress text, error text, heartbeat_at text, owner text);
create index if not exists jobs_system on jobs(system_id, id);
-- One active index job per project, enforced by the database across every Lineage server.
create unique index if not exists jobs_one_active on jobs(system_id) where status in ('queued', 'running');
create table if not exists audit_log(
    id {id}, ts text not null, actor text not null, tool text not null, args text not null);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scope_clause(column: str, systems: Scope) -> tuple[str, list]:
    if systems is None:
        return "", []
    if not systems:
        return " and 1=0", []  # empty scope sees nothing
    return f" and {column} in ({','.join('?' * len(systems))})", list(systems)


class SqlStore:
    AGG = "group_concat"  # string aggregate function

    # ----- dialect hooks
    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Any: raise NotImplementedError
    def _executemany(self, sql: str, rows: list[Sequence[Any]]) -> None: raise NotImplementedError
    def _tx(self) -> AbstractContextManager:
        """Groups statements atomically (nests as a savepoint). Connections are in autocommit
        mode, so a single statement needs no _tx: over a network that saves two round trips."""
        raise NotImplementedError
    def _create_vector_index(self, dim: int) -> None: raise NotImplementedError
    def _insert_chunks(self, system_id: str, rows: list[tuple]) -> None:
        """rows: (file_id, symbol_id | None, start_line, end_line, text, vector)."""
        raise NotImplementedError
    def _delete(self, system_id: str, paths: Sequence[str]) -> None:
        for path in paths:
            self._execute("delete from files where system_id = ? and path = ?", (system_id, path))
    def begin_bulk(self, large: bool) -> None:
        """Called before indexing; `large` = thousands of new files. Index rows are derived data
        (rebuilt from the repo by hash), so dialects may trade durability or index upkeep for speed."""
    def end_bulk(self) -> None:
        """Always called after indexing, also on failure."""
    def _insert_ids(self, table: str, cols: Sequence[str], rows: list[Sequence[Any]]) -> list[int]:
        """Insert rows, returning their ids in order. Dialects may batch."""
        sql = f"insert into {table}({', '.join(cols)}) values ({', '.join('?' * len(cols))}) returning id"
        return [self._one(sql, r)["id"] for r in rows]
    def keyword_search(self, terms: Sequence[str], systems: Scope, limit: int) -> list[tuple[int, float]]:
        raise NotImplementedError
    def vector_search(self, vector: list[float], systems: Scope, limit: int) -> list[tuple[int, float]]:
        raise NotImplementedError

    # ----- helpers
    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        return list(self._execute(sql, params).fetchall())

    def _one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        return self._execute(sql, params).fetchone()

    def _count(self, sql: str, params: Sequence[Any] = ()) -> int:
        return self._execute(sql, params).rowcount

    def _meta(self, key: str) -> str | None:
        row = self._one("select value from meta where key = ?", (key,))
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._execute("insert into meta(key, value) values (?, ?)"
                      " on conflict(key) do update set value = excluded.value", (key, value))

    def _migrate(self) -> None:
        """Bring the schema to SCHEMA_VERSION. Callers hold a lock/transaction around this."""
        found = self._meta("schema_version")
        if found is None:  # fresh database: COMMON_DDL already created the latest schema
            self._set_meta("schema_version", str(SCHEMA_VERSION))
            return
        version = int(found)
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema v{version} is newer than this Lineage (v{SCHEMA_VERSION}); "
                               "upgrade Lineage on this machine.")
        if version < FIRST_SHARED_VERSION:
            raise RuntimeError(f"Schema v{version} is from a pre-release local POC; delete that local "
                               "database file and re-index.")
        while version < SCHEMA_VERSION:
            for statement in MIGRATIONS[version]:
                self._execute(statement)
            version += 1
            self._set_meta("schema_version", str(version))

    # ----- index lifecycle

    def bind_embedder(self, model_id: str, dim: int) -> None:
        with self._tx():
            bound = self._meta("embedder")
            if bound is None:
                self._set_meta("embedder", model_id)
                self._set_meta("dim", str(dim))
                self._create_vector_index(int(dim))
            elif bound != model_id or self._meta("dim") != str(dim):
                raise RuntimeError(
                    f"Index was built with embedder {bound!r}, not {model_id!r}. Everyone (and CI) must "
                    "use the same embedder; rebuild the index to switch.")

    def ensure_system(self, system_id: str, name: str, root: str) -> None:
        self._execute("insert into systems(id, name, root) values (?, ?, ?)"
                      " on conflict(id) do update set name = excluded.name, root = excluded.root",
                      (system_id, name, root))

    def is_indexed(self, system_id: str, path: str) -> bool:
        return self._one("select 1 as x from files where system_id = ? and path = ?", (system_id, path)) is not None

    def file_hashes(self, system_id: str) -> dict[str, str]:
        return {r["path"]: r["hash"] for r in self._all("select path, hash from files where system_id = ?", (system_id,))}

    def replace_files(self, system_id: str, batch: list[tuple[ParsedFile, list[list[float]]]]) -> None:
        """Write a batch of files in one transaction with a handful of round trips, whatever the
        batch size: hosted databases are a network hop away. A crash loses at most this batch,
        which the next run re-indexes by hash."""
        if not batch:
            return
        with self._tx():
            self._delete(system_id, [p.path for p, _ in batch])
            file_ids = self._insert_ids(
                "files", ("system_id", "path", "hash", "language", "summary", "indexed_at"),
                [(system_id, p.path, p.hash, p.language, p.summary, now()) for p, _ in batch])
            symbol_rows = [(fid, system_id, s.name, s.kind, s.start_line, s.end_line, s.signature, s.summary)
                           for fid, (p, _) in zip(file_ids, batch) for s in p.symbols]
            all_symbol_ids = iter(self._insert_ids(
                "symbols", ("file_id", "system_id", "name", "kind", "start_line", "end_line", "signature", "summary"),
                symbol_rows))
            edges, chunks = [], []
            for fid, (p, vectors) in zip(file_ids, batch):
                assert len(vectors) == len(p.chunks)
                sids = [next(all_symbol_ids) for _ in p.symbols]
                edges += [(sid, callee) for sid, s in zip(sids, p.symbols) for callee in s.calls]
                chunks += [(fid, sids[c.symbol] if c.symbol is not None else None, c.start_line, c.end_line, c.text, v)
                           for c, v in zip(p.chunks, vectors)]
            if edges:
                self._executemany("insert into edges(src_symbol, dst_name, type) values (?, ?, 'calls')", edges)
            self._insert_chunks(system_id, chunks)

    def delete_files(self, system_id: str, paths: Sequence[str]) -> None:
        with self._tx():
            self._delete(system_id, paths)

    # ----- retrieval

    def chunks(self, chunk_ids: Sequence[int]) -> dict[int, ChunkRow]:
        if not chunk_ids:
            return {}
        rows = self._all(
            "select c.id, c.system_id, f.path, f.summary as fsum, c.start_line, c.end_line,"
            " s.name, s.kind, s.summary as ssum"
            " from chunks c join files f on f.id = c.file_id left join symbols s on s.id = c.symbol_id"
            f" where c.id in ({','.join('?' * len(chunk_ids))})", list(chunk_ids))
        return {r["id"]: ChunkRow(r["id"], r["system_id"], r["path"], r["fsum"], r["start_line"], r["end_line"],
                                  r["name"], r["kind"], r["ssum"]) for r in rows}

    def project_stats(self, system_id: str) -> dict:
        languages = {r["language"]: r["n"] for r in self._all(
            "select language, count(*) as n from files where system_id = ? group by language order by n desc",
            (system_id,))}
        counts: dict[str, int] = {}
        for r in self._all("select path from files where system_id = ?", (system_id,)):
            top = r["path"].split("/", 1)[0] if "/" in r["path"] else "."
            counts[top] = counts.get(top, 0) + 1
        top_dirs = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:15])
        # ponytail: calls resolve by bare name, so same-named methods share callers. Needs
        # import/receiver-aware resolution before check_impact (Phase 1) relies on it.
        hubs = self._all(
            "select s.name, min(s.kind) as kind, f.path, count(distinct e.src_symbol) as callers from edges e"
            " join symbols s on s.name = e.dst_name and s.system_id = ?"
            " join symbols src on src.id = e.src_symbol and src.system_id = ?"
            " join files f on f.id = s.file_id"
            " group by s.name, f.path order by callers desc limit 15", (system_id, system_id))
        symbols = self._one("select count(*) as n from symbols where system_id = ?", (system_id,))["n"]
        return {"files": sum(languages.values()), "symbols": symbols, "languages": languages,
                "top_dirs": top_dirs, "hubs": hubs}

    def systems(self, systems: Scope) -> list[dict]:
        clause, params = scope_clause("s.id", systems)
        return self._all(
            "select s.id, s.name, s.root, s.description, count(f.id) as files, max(f.indexed_at) as indexed_at"
            f" from systems s left join files f on f.system_id = s.id where 1=1{clause}"
            " group by s.id, s.name, s.root, s.description order by s.id", params)

    def set_description(self, system_id: str, description: str | None) -> bool:
        return self._count("update systems set description = ? where id = ?", (description, system_id)) > 0

    def system_exists(self, system_id: str) -> bool:
        return self._one("select 1 as x from systems where id = ?", (system_id,)) is not None

    # ----- term mappings

    def mappings(self, term: str | None, systems: Scope) -> list[TermMapping]:
        clause, params = scope_clause("system_id", systems)
        if term is not None:
            clause, params = " and term_key = ?" + clause, [term.lower(), *params]
        rows = self._all(
            "select system_id, term, symbol_name, path, status, origin, score, author, updated_at, id"
            f" from term_mappings where 1=1{clause}"
            " order by term_key, case status when 'approved' then 0 when 'proposed' then 1 else 2 end, score desc",
            params)
        return [TermMapping(**r) for r in rows]

    def save_mapping(self, m: TermMapping) -> None:
        self._execute(
            "insert into term_mappings(system_id, term, term_key, symbol_name, path, status, origin, score,"
            " author, updated_at) values (?,?,?,?,?,?,?,?,?,?)"
            " on conflict(system_id, term_key, symbol_name, path) do update set"
            "  status = excluded.status, origin = excluded.origin, score = excluded.score,"
            "  author = excluded.author, updated_at = excluded.updated_at"
            # Reviews always land; a proposal only replaces a proposal, and an inferred
            # guess never replaces a human suggestion.
            " where excluded.status != 'proposed' or (term_mappings.status = 'proposed'"
            "  and (excluded.origin = 'human' or term_mappings.origin = 'inferred'))",
            (m.system_id, m.term, m.term.lower(), m.symbol_name, m.path, m.status, m.origin, m.score,
             m.author, m.updated_at or now()))

    # ----- people and access

    def create_user(self, name: str, is_admin: bool, token_hash: str) -> None:
        self._execute("insert into users(name, is_admin, token_hash, created_at) values (?,?,?,?)",
                      (name, int(is_admin), token_hash, now()))

    def set_token(self, name: str, token_hash: str) -> bool:
        return self._count("update users set token_hash = ? where name = ?", (token_hash, name)) > 0

    def set_disabled(self, name: str, disabled: bool) -> bool:
        return self._count("update users set disabled = ? where name = ?", (int(disabled), name)) > 0

    def user_by_token(self, token_hash: str) -> dict | None:
        rows = self._all(  # one query: runs on every request, and hosted databases are a round trip away
            "select u.name, u.is_admin, m.system_id, m.role from users u"
            " left join memberships m on m.user_id = u.id where u.token_hash = ? and u.disabled = 0", (token_hash,))
        if not rows:
            return None
        return {"name": rows[0]["name"], "is_admin": bool(rows[0]["is_admin"]),
                "roles": {r["system_id"]: r["role"] for r in rows if r["system_id"]}}

    def users(self) -> list[dict]:
        return self._all(
            "select u.name, u.is_admin, u.disabled, u.created_at,"
            f" {self.AGG}(m.system_id, ', ') as maintains"
            " from users u left join memberships m on m.user_id = u.id"
            " group by u.id, u.name, u.is_admin, u.disabled, u.created_at order by u.name")

    def set_role(self, user: str, system_id: str, role: str | None) -> bool:
        uid = self._one("select id from users where name = ?", (user,))
        if uid is None or not self.system_exists(system_id):
            return False
        if role is None:
            self._execute("delete from memberships where user_id = ? and system_id = ?", (uid["id"], system_id))
        else:
            self._execute("insert into memberships(user_id, system_id, role) values (?,?,?)"
                          " on conflict(user_id, system_id) do update set role = excluded.role",
                          (uid["id"], system_id, role))
        return True

    def members(self, system_id: str) -> list[dict]:
        return self._all("select u.name, m.role from memberships m join users u on u.id = m.user_id"
                         " where m.system_id = ? order by u.name", (system_id,))

    # ----- project dictionary and rules

    def terms(self, systems: Scope, term: str | None = None, status: str | None = None) -> list[dict]:
        clause, params = scope_clause("system_id", systems)
        if term is not None:
            clause, params = clause + " and term_key = ?", params + [term.lower()]
        if status is not None:
            clause, params = clause + " and status = ?", params + [status]
        return self._all("select id, system_id, term, definition, source, status, author, reviewer, updated_at"
                         f" from terms where 1=1{clause} order by term_key", params)

    def save_term(self, system_id: str, term: str, definition: str, status: str, author: str,
                  source: str | None = None) -> bool:
        return self._count(
            "insert into terms(system_id, term, term_key, definition, source, status, author, reviewer, updated_at)"
            " values (?,?,?,?,?,?,?,?,?)"
            " on conflict(system_id, term_key) do update set term = excluded.term,"
            "  definition = excluded.definition, source = excluded.source, status = excluded.status,"
            "  author = excluded.author, reviewer = excluded.reviewer, updated_at = excluded.updated_at"
            " where excluded.status = 'approved' or terms.status != 'approved'",
            (system_id, term, term.lower(), definition, source, status, author,
             author if status != "proposed" else None, now())) > 0

    def rules(self, systems: Scope, status: str | None = None) -> list[dict]:
        clause, params = scope_clause("system_id", systems)
        if status is not None:
            clause, params = clause + " and status = ?", params + [status]
        return self._all("select id, system_id, title, body, source, status, author, reviewer, updated_at"
                         f" from rules where 1=1{clause} order by status, id", params)

    def save_rule(self, system_id: str, title: str, body: str, status: str, author: str,
                  rule_id: int | None = None, source: str | None = None) -> bool:
        if rule_id is None:
            self._execute(
                "insert into rules(system_id, title, body, source, status, author, reviewer, updated_at)"
                " values (?,?,?,?,?,?,?,?)",
                (system_id, title, body, source, status, author, author if status != "proposed" else None, now()))
            return True
        return self._count(
            "update rules set title = ?, body = ?, source = ?, status = ?, reviewer = ?, updated_at = ?"
            " where id = ? and system_id = ?",
            (title, body, source, status, author, now(), rule_id, system_id)) > 0

    # Whitelist: table names never come from request input.
    _REVIEWABLE = {"term": "terms", "rule": "rules", "mapping": "term_mappings"}

    def review(self, kind: str, system_id: str, item_id: int, status: str, reviewer: str) -> bool:
        who = "author" if kind == "mapping" else "reviewer"
        return self._count(
            f"update {self._REVIEWABLE[kind]} set status = ?, {who} = ?, updated_at = ? where id = ? and system_id = ?",
            (status, reviewer, now(), item_id, system_id)) > 0

    def delete_item(self, kind: str, system_id: str, item_id: int) -> bool:
        return self._count(f"delete from {self._REVIEWABLE[kind]} where id = ? and system_id = ?",
                           (item_id, system_id)) > 0

    # ----- background jobs

    def create_job(self, system_id: str, kind: str, requested_by: str, owner: str) -> int | None:
        """None when the project already has an active job (on any server sharing this database)."""
        if self._one("select id from jobs where system_id = ? and status in ('queued', 'running')", (system_id,)):
            return None
        try:
            return self._insert_ids(
                "jobs", ("system_id", "kind", "status", "requested_by", "created_at", "heartbeat_at", "owner"),
                [(system_id, kind, "queued", requested_by, now(), now(), owner)])[0]
        except Exception as e:  # lost a race with another server: the unique index said no
            if "jobs_one_active" in str(e) or "UNIQUE" in str(e).upper():
                return None
            raise

    def update_job(self, job_id: int, status: str | None = None, progress: dict | None = None,
                   error: str | None = None) -> None:
        sets, params = ["heartbeat_at = ?"], [now()]
        if status:
            sets.append("status = ?")
            params.append(status)
            if status == "running":
                sets.append("started_at = ?")
                params.append(now())
            elif status in ("done", "failed", "interrupted"):
                sets.append("finished_at = ?")
                params.append(now())
        if progress is not None:
            sets.append("progress = ?")
            params.append(json.dumps(progress))
        if error is not None:
            sets.append("error = ?")
            params.append(error[:2000])
        self._execute(f"update jobs set {', '.join(sets)} where id = ?", (*params, job_id))

    def jobs(self, systems: Scope, limit: int = 20) -> list[dict]:
        clause, params = scope_clause("system_id", systems)
        rows = self._all(f"select * from jobs where 1=1{clause} order by id desc limit ?", (*params, limit))
        for r in rows:
            r["progress"] = json.loads(r["progress"]) if r["progress"] else None
        return rows

    def heartbeat(self, job_id: int) -> None:
        self._execute("update jobs set heartbeat_at = ? where id = ?", (now(), job_id))

    def interrupt_stale_jobs(self, older_than: str) -> int:
        """Jobs whose server stopped heartbeating (crashed, killed) can never finish."""
        return self._count("update jobs set status = 'interrupted', finished_at = ?"
                           " where status in ('queued', 'running') and (heartbeat_at is null or heartbeat_at < ?)",
                           (now(), older_than))

    # ----- operations

    def diagnostics(self) -> dict:
        counts = {t: self._one(f"select count(*) as n from {t}")["n"]
                  for t in ("systems", "files", "chunks", "users", "terms", "rules")}
        return {**self._server_info(), "schema_version": self._meta("schema_version"),
                "embedder": self._meta("embedder"), "rows": counts}

    def _server_info(self) -> dict:
        return {}

    # ----- audit

    def audit(self, user: str, tool: str, args: dict) -> None:
        self._execute("insert into audit_log(ts, actor, tool, args) values (?,?,?,?)",
                      (now(), user, tool, json.dumps(args, default=str)[:4000]))
