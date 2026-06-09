# dto.py — Request/response schemas for the member-day digest service.
# Created: 2026-06-08 — VIP Onboarding Phase B chunk 5 (the synthesized
#   "your day" digest + the agent briefing).
# Per cloud rule §4 (request/response split) and §6 (validate at entry): the
# service re-parses its inputs through ``MemberDayDigestRequest`` even when
# called by internal callers (the agent briefing, chunk 6's intent board) —
# not just over HTTP. The response models are the reusable structured shape
# chunk 6 consumes; the agent briefing renders them down to a capped string.

from __future__ import annotations

from pydantic import BaseModel, Field


class MemberDayDigestRequest(BaseModel):
    """Input to ``member_day_digest`` — the tenant + member to summarize.

    ``member_id`` is the opaque cloud user id; it keys the per-user OAuth
    token bucket AND (downstream, in the agent gate) the ``user:{id}`` scope.
    We forbid empty/whitespace ids so a blank id can never collapse into a
    cross-member read.
    """

    workspace_id: str = Field(min_length=1)
    member_id: str = Field(min_length=1)

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        if not self.workspace_id.strip():
            raise ValueError("workspace_id must not be blank")
        if not self.member_id.strip():
            raise ValueError("member_id must not be blank")


class DigestEvent(BaseModel):
    """One upcoming calendar event in the digest (next ~7 days)."""

    summary: str = ""
    start: str = ""
    end: str = ""
    location: str = ""


class DigestMail(BaseModel):
    """One recent/unread mail item in the digest (top items only)."""

    subject: str = ""
    sender: str = ""
    date: str = ""


class MemberDayDigest(BaseModel):
    """The structured "your day" digest for one member.

    Reusable: chunk 6's intent board reads this same shape; the agent
    briefing (``agent_service._member_briefing_block``) renders it down to a
    short capped string. ``member_id`` is echoed so a consumer can assert the
    digest belongs to the member it asked about — the per-member isolation
    guarantee is that this field always equals the ``member_id`` argument
    (never a caller-supplied value).
    """

    workspace_id: str
    member_id: str
    # Forward-looking calendar (next ~7 days), soonest first.
    events: list[DigestEvent] = Field(default_factory=list)
    # Recent/unread mail — top items only.
    unread_mail_count: int = 0
    top_mail: list[DigestMail] = Field(default_factory=list)
    # Per-source errors (auth/transient). Non-fatal: a source that errored
    # simply contributes nothing. ``empty`` is True when BOTH sources yielded
    # no data (no connected accounts, or genuinely nothing) — the briefing
    # uses it to emit no block at all rather than an empty heading.
    errors: list[str] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        """True when there is nothing to brief: no events AND no mail."""
        return not self.events and self.unread_mail_count == 0 and not self.top_mail
