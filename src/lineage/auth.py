"""Who is calling, and what they may do.

Roles: admin (everything: onboarding, rules, people). Every other user is a developer on every
project (use it, suggest entries). An admin may elevate someone to maintainer of a project
(approve dictionary, rules and code links; re-index).
Tokens are random 256-bit secrets shown once; only their SHA-256 is stored.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field

ROLE_RANK = {"developer": 1, "maintainer": 2}
TOKEN_PREFIX = "lin_"


@dataclass(frozen=True)
class Principal:
    name: str
    is_admin: bool = False
    roles: dict[str, str] = field(default_factory=dict)  # project -> maintainer (elevations only)

    def scope(self) -> None:
        """Projects this principal may read: all of them, for every signed-in user."""
        return None

    def role(self, project: str) -> str:
        return "admin" if self.is_admin else self.roles.get(project, "developer")

    def can(self, project: str, role: str) -> bool:
        return self.is_admin or ROLE_RANK[self.role(project)] >= ROLE_RANK[role]

    def require(self, project: str, role: str = "developer") -> None:
        if not self.can(project, role):
            # Same message whether the project exists or not: no probing for project names.
            raise PermissionError(f"{self.name} needs the {role} role on project {project!r}")

    def require_admin(self) -> None:
        if not self.is_admin:
            raise PermissionError(f"{self.name} is not an admin")


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
