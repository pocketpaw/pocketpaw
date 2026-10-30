# dto.py — Request/response DTOs for the Code Mode delegate channel.
#
# History: this file used to also carry the STATELESS agent-turn DTOs
# (``AgentTurnRequest`` / ``AgentTurnResponse`` and their ``ContextItem`` /
# ``AgentMessage`` / ``ToolCall`` / ``ToolResult`` supporting shapes). Those went
# with the turn-agent on 2026-07-23 (remove/codeagent-turn-agent) — the /code
# surface runs on the MAIN PocketPaw agent now, not a second in-module loop.
#
# What remains is the wire shape for ``POST /codeagent/resolve`` — the INBOUND
# half of the browser-delegate rendezvous (``delegates.py``). Note what is NOT
# here: no schema for ``result``. The payload is whatever the browser answered a
# ``code_delegate`` frame with, and pinning its shape at the wire would mean
# changing this file every time the client learns a new answer shape, for a value
# the backend only forwards to the parked caller.
from __future__ import annotations

from pydantic import BaseModel, Field


class DelegateResolveRequest(BaseModel):
    """The browser handing back the answer to one ``code_delegate`` frame.

    ``corrId`` is the id the backend minted when it parked, echoed verbatim. It
    is the ONLY thing tying this POST to a waiting turn, which is why it is
    length-bounded here rather than trusted — an unbounded id would be a free
    dictionary key on a process-global registry.
    """

    corrId: str = Field(..., min_length=1, max_length=128)
    result: dict = Field(default_factory=dict)


class DelegateResolveResponse(BaseModel):
    """Acknowledgement only. There is nothing to return — the value went to the
    parked caller, not back down this request. ``accepted`` is always true on a
    2xx; an unresolvable ``corrId`` is a 404 (``code_delegate.not_found``), not a
    200 with ``accepted: false``, so a client cannot mistake "nobody was waiting"
    for success.
    """

    accepted: bool = True


__all__ = [
    "DelegateResolveRequest",
    "DelegateResolveResponse",
]
