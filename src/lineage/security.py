"""Secrets never reach the index; tools never touch paths outside allowed roots."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePosixPath

# Files skipped outright, matched against the lowercased file name.
SECRET_FILE_GLOBS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "*.crt", "*.cer", "*.der", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".htpasswd", "*.tfvars", "*.tfstate",
    "credentials", "credentials.*", "secrets.*", "*secret*.json", "*secret*.yaml", "*secret*.yml",
    "service-account*.json", "kubeconfig", "*.kubeconfig",
)

# Content patterns redacted from chunks before they are stored or embedded.
SECRET_PATTERNS = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",                      # AWS access key id
    r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",                     # GitHub tokens
    r"\bgithub_pat_[A-Za-z0-9_]{60,}\b",
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",                   # Slack
    r"\bsk-[A-Za-z0-9_-]{20,}\b",                          # OpenAI/Anthropic-style keys
    r"\bAIza[0-9A-Za-z_-]{35}\b",                          # Google API key
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",  # JWT
    r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]{3,}@",  # credentials in URLs
    # key = "value" assignments for secret-ish names; keeps the name, redacts the value.
    r"""(?i)(?<![\w.-])(?:[a-z0-9_.-]*(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token|auth)[a-z0-9_.-]*)\s*[:=]\s*['"][^'"\s]{8,}['"]""",
)]

REDACTED = "[REDACTED]"


def is_secret_file(rel_path: str) -> bool:
    name = PurePosixPath(rel_path).name.lower()
    return any(fnmatch.fnmatchcase(name, g) for g in SECRET_FILE_GLOBS)


def redact(text: str) -> tuple[str, int]:
    """Return (text with secrets replaced, number of redactions). Newlines are kept so
    line numbers of everything after a multi-line secret stay correct."""
    total = 0
    for pattern in SECRET_PATTERNS:
        text, n = pattern.subn(lambda m: REDACTED + "\n" * m.group().count("\n"), text)
        total += n
    return text, total


def resolve_repo_path(repo_path: str, allowed_roots: list[Path]) -> Path:
    """Resolve symlinks/.. and require the result to sit inside an allowed root."""
    path = Path(repo_path).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"Not a directory: {path}")
    if not any(path == root or path.is_relative_to(root) for root in allowed_roots):
        raise PermissionError(
            f"{path} is outside allowed_roots; add its parent to allowed_roots in config."
        )
    return path


SYSTEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def validate_system_id(system_id: str) -> str:
    if not SYSTEM_ID_RE.fullmatch(system_id):
        raise ValueError("system_id must be 1-64 chars of lowercase letters, digits, '.', '_', '-'")
    return system_id
