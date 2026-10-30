# Deploying Lineage for a team

The normal setup: one or more Lineage servers, a hosted Postgres the client owns, and CI keeping
the index fresh. SQLite is for POCs and tests only.

```
 developers' editors ──HTTPS + token──▶ Lineage server(s) ──TLS──▶ hosted Postgres (client-owned)
 CI (lineage index) ───────────────────────────────────────TLS──▶        "lineage" schema
```

## Your data stays yours

Every piece of state (code index, embeddings, people, rules, dictionary, jobs, audit log) lives
in **one database you choose**:

| `db_url` | Use |
| --- | --- |
| `postgresql://user:pw@host/db` | the client's own Postgres. Lineage keeps its tables in one schema (`db_schema`, default `lineage`) beside your data. |
| `sqlite:///path/index.db` | POC and tests only: one machine, not shared |

Lineage has no database of its own and no telemetry. Code is embedded locally (`hash`, a local
`sentence-transformers` model, or your own Ollama) and never leaves your infrastructure. Nothing is
written into your repositories.

## 1. Database (the DBA runs this once)

```sql
CREATE EXTENSION IF NOT EXISTS vector;                 -- needs a privileged role
CREATE ROLE lineage_app LOGIN PASSWORD '...';
CREATE SCHEMA lineage AUTHORIZATION lineage_app;      -- Lineage then needs no other privilege
```

- **Any managed Postgres 14+ with pgvector 0.6+** works: AWS RDS/Aurora, Azure Database for
  PostgreSQL (allow-list `vector` in `azure.extensions`), Google Cloud SQL / AlloyDB, Supabase,
  Neon, or self-hosted. Connect directly or through a session-mode pooler (transaction pooling
  breaks per-connection settings).
- **TLS is required by default** for any non-local host (`sslmode=require`); use
  `?sslmode=verify-full&sslrootcert=...` to also verify the server.
- **Credentials** come from the environment: `LINEAGE_DB_URL`, or a password-less URL plus
  `PGPASSWORD` / `.pgpass`. Only Lineage servers and CI hold them; anyone with them is an admin.
- **Backups**: the `lineage` schema is all there is. Your normal database backups cover it.

## 2. Server

```sh
pip install -e ".[pg]"                                   # add ,st for local semantic embeddings
export LINEAGE_DB_URL=postgresql://lineage_app:***@db.internal/engineering
export LINEAGE_ALLOWED_ROOTS=/srv/repos                  # checkouts the server may index
lineage db check                                         # connects, migrates, reports TLS/latency
lineage user add aalok --admin                           # prints the admin token once
lineage serve --transport streamable-http --host 0.0.0.0 --tls-cert cert.pem --tls-key key.pem
```

Then in the web UI: onboard each system (indexing runs in the background with live progress),
give each project a one-line description ("orders-service: backend for orders, pricing and payments"),
write its rules and dictionary, and add people. Each person gets a token, shown once, and is a
developer on every project straight away.

`lineage db check` reports the server version, whether TLS is in use, pgvector and schema
versions, round-trip time and row counts, with warnings for anything that needs attention.

## 3. Each repository

Web UI → project → **Connect** → pick the team's AI tool (VS Code Copilot, Claude Code, Codex,
Cursor, Gemini CLI, or any MCP client) and follow the generated setup. For VS Code it is a
`.vscode/mcp.json` to commit:

```json
{
  "inputs": [{ "type": "promptString", "id": "lineage-token", "description": "Lineage access token", "password": true }],
  "servers": {
    "lineage": {
      "type": "http",
      "url": "https://lineage.example.internal/mcp",
      "headers": { "Authorization": "Bearer ${input:lineage-token}", "X-Lineage-Project": "web-frontend" }
    }
  }
}
```

Tokens never go into committed files: VS Code prompts for them, Claude Code stores them in the
developer's local config, and Codex, Cursor and Gemini read `LINEAGE_TOKEN` from the environment.

The same page downloads the instruction file each tool reads (`copilot-instructions.md`,
`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`), generated fresh from the approved rules, dictionary and
system map. Re-download them when rules change.

## 4. CI

Keep the index current on every merge to main:

```sh
lineage index /path/to/checkout --project orders-service
```

Only changed files are re-processed, so this takes seconds on most merges.

## Running several servers

Several Lineage servers can share one database (high availability, or one per team). The
database enforces one index job per project, jobs send heartbeats, and a restarting server only
cleans up jobs whose server went silent.

## Upgrades

Schema changes are migrations applied on startup under a lock. Rules, dictionary and people are
never rebuilt. A database newer than the running Lineage is refused rather than modified.

## Docker

```sh
docker build -t lineage .
docker run --rm -e LINEAGE_DB_URL=postgresql://... lineage user add aalok --admin
docker run --rm -p 127.0.0.1:8000:8000 -e LINEAGE_DB_URL=postgresql://... -v /srv/repos:/repos:ro \
  lineage serve --transport streamable-http --host 0.0.0.0 --insecure-http   # TLS at your proxy
```

Grammars are downloaded at build time, so the container runs without network access.

## Configuration

Defaults < YAML (`LINEAGE_CONFIG`, see [`config/lineage.example.yaml`](../config/lineage.example.yaml))
< environment variables:

| Variable | Meaning |
| --- | --- |
| `LINEAGE_DB_URL` | where all state lives (`postgresql://...` or `sqlite:///...`) |
| `LINEAGE_DB_SCHEMA` | Postgres schema for Lineage's tables (default `lineage`) |
| `LINEAGE_DB_POOL_SIZE` | connections per server (default 10) |
| `LINEAGE_EMBEDDER` | `hash` (default), `st:<model>` or `ollama:<model>`; pinned into the index |
| `LINEAGE_ALLOWED_ROOTS` | directories the server may index |
| `LINEAGE_USER` | identity of the local admin for the CLI and stdio mode |

The embedder is pinned: everyone and CI must use the same one, and a mismatch is refused rather
than silently mixing vectors.

## Latency

Every request crosses the network to the database. Measured through a 31 ms round trip (a
cross-region link): indexing 1,500 files went from 19 s to 23 s, a no-change re-index from 0.6 s
to 0.9 s, and search was 133 ms. Run Lineage servers and CI in the same region as the database.
