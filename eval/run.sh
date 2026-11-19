#!/usr/bin/env bash
# Index the pinned open-source repos in repos.txt and score search on cases.yaml.
#   eval/run.sh [workdir] [extra `lineage eval` args, e.g. --min-top10 0.85]
# Uses the configured embedder (default: the semantic model), a fresh SQLite file in workdir.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mkdir -p "${1:-${TMPDIR:-/tmp}/lineage-eval}" && cd "${1:-${TMPDIR:-/tmp}/lineage-eval}" && pwd)"
shift || true
native() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else echo "$1"; fi; }  # Git Bash on Windows
export LINEAGE_DB_URL="sqlite:///$(native "$work/eval.db")" LINEAGE_ALLOWED_ROOTS="$(native "$work/repos")"
rm -f "$work/eval.db"
mkdir -p "$work/repos"
grep -v '^\s*#' "$here/repos.txt" | while read -r project url ref; do
  [ -n "$project" ] || continue
  dir="$work/repos/$project"
  if [ "$(git -C "$dir" rev-parse HEAD 2>/dev/null)" != "$(git -C "$dir" rev-parse "$ref^{commit}" 2>/dev/null || echo none)" ]; then
    rm -rf "$dir" && git init -q "$dir"
    git -C "$dir" fetch -q --depth 1 "$url" "$ref"
    git -C "$dir" -c advice.detachedHead=false checkout -q FETCH_HEAD
  fi
  lineage index "$dir" --project "$project" >/dev/null
done
lineage eval "$here/cases.yaml" "$@" | tee "$work/result.json"
