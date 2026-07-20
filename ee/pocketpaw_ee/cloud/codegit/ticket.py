# ticket.py — the VM→broker credential for the Code Mode git proxy (CM-3d).
# Created 2026-07-16 (feat/code-mode): the sandbox VM needs SOME credential to
# talk to the git proxy — but it must NOT be the GitHub token (that never enters
# the VM). This ticket is that credential: a short-lived JWT, signed with the
# shared ``auth.core.SECRET`` (same primitive as codeconnect/state.py and the
# ws_tickets), scoped to ONE ``(sandbox, repo)`` under ``(workspace, user)``.
#
# It is embedded in the VM's git remote URL (basic-auth password), so it does
# live in the VM — and that's fine: it only authorizes proxying git to ITS repo
# for ITS sandbox. A leak lets someone push to that one repo through the broker
# (exactly what the legitimate user can already do); it can NEVER reach the
# GitHub token or another repo. The broker mints the real, repo-scoped GitHub
# token itself, server-side, on each proxied request.
#
# Lifetime is generous (24h) relative to a sandbox's minutes-long idle life —
# a reprovision re-wires a fresh ticket, so this only has to outlive one active
# editing session.

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from pocketpaw_ee.cloud.auth.core import SECRET

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_AUDIENCE = "code-git-proxy"
# A sandbox auto-stops after minutes of idle and is reprovisioned (re-wiring a
# fresh ticket) on next open, so 24h comfortably covers any single session while
# keeping a leaked ticket useless within a day.
_LIFETIME_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class TicketClaims:
    """The identity a verified git-proxy ticket recovers."""

    workspace_id: str
    user_id: str
    sandbox_id: str
    repo: str  # "owner/repo" — the ONE repo this ticket may proxy


def sign_ticket(
    workspace_id: str,
    user_id: str,
    sandbox_id: str,
    repo: str,
    *,
    now: datetime | None = None,
) -> str:
    """Sign a git-proxy ticket scoped to one ``(sandbox, repo)`` for ``(ws, user)``."""
    reference = now or datetime.now(UTC)
    payload = {
        "ws": workspace_id,
        "sub": user_id,
        "sbx": sandbox_id,
        "repo": repo,
        "aud": _AUDIENCE,
        "iat": int(reference.timestamp()),
        "exp": int((reference + timedelta(seconds=_LIFETIME_SECONDS)).timestamp()),
    }
    return jwt.encode(payload, SECRET, algorithm=_ALGORITHM)


def verify_ticket(ticket: str) -> TicketClaims | None:
    """Verify a git-proxy ticket; return its claims, or ``None`` on ANY failure.

    Every unverifiable ticket — bad signature, wrong audience, expired, malformed,
    missing claims — returns ``None`` so the proxy rejects them all identically
    (a clean 401, never a distinguishable error shape).
    """
    if not ticket:
        return None
    try:
        payload = jwt.decode(ticket, SECRET, algorithms=[_ALGORITHM], audience=_AUDIENCE)
    except Exception:  # noqa: BLE001 — any decode failure is a uniform reject
        logger.debug("codegit: rejected git-proxy ticket", exc_info=True)
        return None
    ws = payload.get("ws")
    user = payload.get("sub")
    sandbox = payload.get("sbx")
    repo = payload.get("repo")
    if not all(isinstance(v, str) and v for v in (ws, user, sandbox, repo)):
        return None
    return TicketClaims(workspace_id=ws, user_id=user, sandbox_id=sandbox, repo=repo)


__all__ = ["TicketClaims", "sign_ticket", "verify_ticket"]
