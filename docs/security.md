# Security model

Lineage handles source code, so it is built to keep that code inside your infrastructure and to
show only what each caller may see.

## Data

- **One database, yours.** All state lives in the configured Postgres schema (or a local SQLite
  file for POCs). Lineage runs no service of its own and sends no telemetry.
- **No LLM calls.** Indexing and search run locally; code is never sent to a model provider.
- **Nothing written into repositories.** Agent instruction files are generated on demand.

## Identity and access

- **Tokens** are random 256-bit secrets, shown once, stored only as SHA-256. Rotating or
  disabling a user takes effect on the next call; roles are re-read on every request.
- **Roles**: admin (onboarding, rules, people), maintainer (per project: approve, re-index),
  developer (everyone else: use and suggest).
- **Authorization** is enforced in one service layer shared by MCP, the web UI and the CLI. Item
  ids always match together with their project, so an id from another project never resolves.
- **Database credentials** belong only to Lineage servers and CI. Developers reach data through a
  server, which applies roles.

## Code and secrets

- **Secret files are never indexed**: `.env`, keys, certificates, credential and kubeconfig files.
- **Secrets inside code are redacted** (cloud keys, tokens, private keys, `password = "..."`, URL
  credentials) before parsing, embedding, storage, or being returned by `read_code`.
- **`read_code`** serves only files Lineage indexed, never outside the project root, at most 300
  lines per call, and every read is audited.
- **Onboarding** accepts only directories under `allowed_roots`, with symlinks resolved.

## Transport and web UI

- **TLS**: non-loopback HTTP refuses to start without a certificate unless explicitly overridden
  (for a TLS-terminating proxy). Database connections require TLS for non-local hosts by default.
- **Web UI**: the bearer token lives in `sessionStorage` (no cookies, so no CSRF); strict
  Content-Security-Policy; all data rendered as text; JSON bodies capped at 64 KB; asset downloads
  whitelisted by name.
- **Errors**: expected failures return their message; anything else returns a generic error, so
  internals never leak.

## Audit

Every tool call and API action is logged in the database with the user and its arguments, so
you can see which tools an agent called and what it read.

## Reporting a vulnerability

Please open a private security advisory on GitHub rather than a public issue.
