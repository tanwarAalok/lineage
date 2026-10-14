# Lineage

Code intelligence server for large, legacy, multi-system codebases. Agents (Copilot agent mode,
Claude, others) call MCP tools to get a project's rules and dictionary and to find the few files
that matter, instead of scanning the repo. A web UI next to the MCP endpoint lets the admin set
up projects and rules, and lets everyone suggest dictionary entries.

Platform-independent: no Git hosting, SSO or cloud service required. Runs anywhere Python runs.

## Your data stays yours

Every piece of state (code index, embeddings, people, rules, dictionary, jobs, audit log) lives
in **one database you choose**:

| `db_url` | Use |
| --- | --- |
| `postgresql://user:pw@host/db` | the client's own Postgres. Lineage keeps its tables in one schema (`db_schema`, default `lineage`) beside your data. Needs the `pgvector` extension (a DBA runs `CREATE EXTENSION vector;` once if Lineage's role may not). |
| `sqlite:///path/index.db` | POC and tests only: one machine, not shared |

Lineage has no database of its own and no telemetry. Code is embedded locally (`hash`, a local
`sentence-transformers` model, or your own Ollama) and never leaves your infrastructure.

## Hosted deployment (the normal setup)

```
 developers' editors ──HTTPS + token──▶ Lineage server(s) ──TLS──▶ hosted Postgres (client-owned)
 CI (lineage index) ───────────────────────────────────────TLS──▶        "lineage" schema
```

- **Members never get database credentials.** They reach the data only through a Lineage server,
  which applies roles. Only Lineage servers and CI hold `LINEAGE_DB_URL`; anyone with it is an admin.
- **Any managed Postgres 14+ with pgvector 0.6+** works: AWS RDS/Aurora, Azure Database for
  PostgreSQL (allow-list `vector` in `azure.extensions`), Google Cloud SQL / AlloyDB, Supabase, Neon,
  or self-hosted. Connect directly or through a session-mode pooler (transaction pooling breaks
  per-connection settings).
- **TLS is required by default** for any non-local host (`sslmode=require`); use
  `?sslmode=verify-full&sslrootcert=...` to also verify the server. Credentials come from the
  environment (`LINEAGE_DB_URL`, or a password-less URL plus `PGPASSWORD` / `.pgpass`).
- **Several Lineage servers can share one database** (HA, or one per team): one index job per
  project is enforced by the database, jobs heartbeat, and a restarting server only cleans up
  jobs whose server went silent.
- **Upgrades migrate in place.** Rules, dictionary and people are never rebuilt; a database newer
  than the running Lineage is refused rather than modified.
- **Backups**: the `lineage` schema is all there is. Your normal database backups cover it.

### What the DBA runs once

```sql
CREATE EXTENSION IF NOT EXISTS vector;                 -- needs a privileged role
CREATE ROLE lineage_app LOGIN PASSWORD '...';
CREATE SCHEMA lineage AUTHORIZATION lineage_app;      -- Lineage then needs no other privilege
```

Then on the Lineage server: `lineage db check` connects, applies migrations, and reports TLS,
versions, round-trip time and row counts, with warnings for anything that needs attention.

Measured through a proxy adding a 31 ms round trip (a cross-region link): indexing 1,500 files
went from 19 s to 23 s, a no-change re-index from 0.6 s to 0.9 s, and search was 133 ms.
Run Lineage servers and CI in the same region as the database.

## Who does what

| Role | Who | Can |
| --- | --- | --- |
| **admin** | created with `lineage user add <name> --admin` | onboard projects, set rules and dictionary, add people, choose maintainers |
| **maintainer** | optional, per project, chosen by an admin | approve/reject suggestions, edit rules, re-index |
| **developer** | everyone else, automatically, on every project | use any project from their repo; suggest dictionary entries, rules and code links |

Suggestions from developers, and everything agents submit, land as `proposed`; agents only ever
receive approved rules and dictionary entries. Every rule and entry records its **source**
(document + section, meeting, person): requirements are scattered and sometimes wrong, so
reviewers can see where a claim came from before approving it.

## MCP tools

| Tool | What it does |
| --- | --- |
| `get_project_context(project?)` | Approved rules + dictionary. Agents call this first |
| `search_code(query, top_n=8, project?)` | Hybrid keyword + vector search, ranked per file with a one-line meaning |
| `resolve_term(term, project?)` | A design term's definition and the code implementing it; unreviewed code links come back `proposed` |
| `suggest_term(term, definition, source?, symbol?, path?, project?)` | A proposal for maintainers to review |
| `list_projects()` | Projects, your role, latest indexing job |
| `onboard_project(repo_path, project?)` | Admin: index a new repo (background job). Maintainer: re-index |

`project` defaults to the `X-Lineage-Project` header that each repo sets once in `.vscode/mcp.json`.

## Running a team server

```sh
pip install -e ".[pg]"                                   # add ,st for local semantic embeddings
set LINEAGE_DB_URL=postgresql://lineage:***@db.internal/engineering
set LINEAGE_ALLOWED_ROOTS=D:\repos                       # checkouts the server may index
lineage user add aalok --admin                           # prints the admin token once
lineage serve --transport streamable-http --host 0.0.0.0 --tls-cert cert.pem --tls-key key.pem
```

Open the web UI, sign in, onboard a project (indexing runs in the background with live
progress), write its rules and dictionary, and add people. Each person gets a token, shown once,
and is a developer on every project straight away.

### A developer connecting their repository

Web UI → project → **Connect** → commit the generated `.vscode/mcp.json`:

```json
{
  "inputs": [{ "type": "promptString", "id": "lineage-token", "description": "Lineage access token", "password": true }],
  "servers": {
    "lineage": {
      "type": "http",
      "url": "https://lineage.example.internal/mcp",
      "headers": { "Authorization": "Bearer ${input:lineage-token}", "X-Lineage-Project": "billing" }
    }
  }
}
```

VS Code asks each developer for their own token; tokens are never committed. The same page
downloads `copilot-instructions.md` / `AGENTS.md` / `glossary.yaml` built from the approved
rules and dictionary.

### CLI and single-user mode

`lineage index|search|resolve|projects|user|maintainer` and `lineage serve` (stdio) act as the
local admin: whoever can run them already holds the database credentials. `lineage index` runs in
the foreground with progress, which suits CI.

### Docker

```sh
docker build -t lineage .
docker run --rm -e LINEAGE_DB_URL=postgresql://... lineage user add aalok --admin
docker run --rm -p 127.0.0.1:8000:8000 -e LINEAGE_DB_URL=postgresql://... -v D:/repos:/repos:ro \
  lineage serve --transport streamable-http --host 0.0.0.0 --insecure-http   # TLS at your proxy
```

## Large and legacy codebases

Measured on a generated 22,000-file repo (Python, Java, JS, COBOL, PL/SQL, VB, text; 41 MB),
8-core laptop, `hash` embedder:

| | SQLite | Postgres 16 + pgvector |
| --- | --- | --- |
| first index | 76 s | 90 s |
| re-index, nothing changed | 1.9 s | 7.1 s |
| search, median | 216 ms | 60 ms |

- Parsing runs in parallel processes in bounded windows, so memory stays flat at any repo size.
- Only files whose content hash changed are re-processed; every file is its own transaction, so
  an interrupted run resumes where it stopped. A file that fails to parse is logged and skipped.
- On Postgres, loads of 2,000+ new files defer vector-index upkeep to one build afterwards (5x faster).
- Legacy handling: Windows-1252 and UTF-16 sources, COBOL / VB6 / Pascal / Fortran / PL/SQL
  extensions, routines thousands of lines long split so their whole body is searchable, and
  top-level script code between functions indexed too.
- Real embedding models are slower than `hash`; first-index time then depends mostly on the model
  and hardware (a GPU helps). Incremental re-indexing is unaffected.

## Configuration

Defaults < YAML (`LINEAGE_CONFIG`, see `config/lineage.example.yaml`) < env vars
(`LINEAGE_DB_URL`, `LINEAGE_DB_SCHEMA`, `LINEAGE_EMBEDDER`, `LINEAGE_ALLOWED_ROOTS`, `LINEAGE_USER`).

The embedder is **pinned into the index**: everyone and CI must use the same one, and a mismatch
is refused rather than silently mixing vectors.

## Layout

```
src/lineage/
  server.py      MCP tools; HTTP calls authenticate per call
  web.py         JSON API + web UI routes (static/ holds the page)
  cli.py         index | search | resolve | projects | user | maintainer | serve
  service.py     use cases: authorization, validation, jobs, audit; shared by MCP, web and CLI
  auth.py        Principal, roles, token minting/hashing
  indexer.py     parallel, incremental, resumable indexing
  search.py      hybrid retrieval + term resolution
  assets.py      instruction files + glossary from approved rules and dictionary
  ingest/        parse.py (walk, decode, tree-sitter, chunks, calls) · embed.py (hash / st / ollama)
  store/         base.py (the one Store interface) · sql.py (shared SQL)
                 sqlite.py (FTS5 + sqlite-vec) · postgres.py (tsvector + pgvector)
  security.py    secret-file skip list, token redaction, path allowlist
```

## Security model

- **Identity**: random 256-bit tokens, shown once, stored only as SHA-256. Rotating or disabling a
  user takes effect on the next call; roles are re-read on every request.
- **Authorization** is enforced in the service layer for MCP, web and CLI alike. Item ids always
  match together with their project, so an id from another project never resolves.
- **Web UI**: bearer token in `sessionStorage` (no cookies, so no CSRF); strict CSP; all data
  rendered as text; JSON bodies capped at 64 KB; asset downloads whitelisted by name.
- **Transport**: non-loopback HTTP refuses to start without TLS unless explicitly overridden.
- **Secrets never indexed**: `.env`, keys, certs, credential files are skipped; tokens are redacted
  before parsing, embedding or storage.
- **Paths**: onboarding only accepts directories under `allowed_roots` (symlinks resolved).
- **Audit**: every call is logged in the database with user and arguments.

## Known limits (deliberate, with upgrade path)

- Calls resolve by bare name, so same-named methods share callers. Needs import/receiver-aware
  resolution before `check_impact` relies on it.
- Summaries are docstrings/comments/signatures, not LLM-written. Kotlin structure is shallow.
- A file is capped at 800 chunks; the tail of a larger (usually generated) file is not searchable.
- Rule and dictionary history lives in the audit log, not as versioned rows.
- Databases supported: Postgres (pgvector) and SQLite. Others (SQL Server, Oracle) need a dialect
  class like `store/postgres.py`.

## Not built yet (per roadmap)

Phase 1: contract registry keyed by CR, `get_contract`, `check_impact`, decision log, CR
dependency graph. Phase 3: HLD/Confluence ingestion (lands as `proposed` with provenance, never
trusted automatically). Measurement harness (milestone 7) waits on the 10 real design docs.
