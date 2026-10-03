# ee/pocketpaw_ee/cloud/growth/writer.py — the agent that writes first-touch
# outreach copy for one prospect, and the ``WriterFn`` seam in front of it.
#
# Same two halves as ``researcher.py``: ``GROWTH_WRITER_AGENT`` is a declarative
# definition seeded into each workspace by slug, and ``agent_write_drafts`` runs
# it and parses what comes back. The route answers 503 while no ``WriterFn`` is
# installed; tests install a fake through ``set_production_writer_fn``.
#
# The writer has NO tools (``tools=[]``, ``tool_mode`` exclusive). It composes
# from the research already on the prospect and nothing else, so it cannot look
# anything up and cannot file or send anything. The agent PROPOSES copy; the
# growth service decides which drafts are stored (eligible channels only) and a
# human still approves every send.
#
# The parser never raises: an unreadable answer is zero drafts, which the
# service turns into a 502.

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from pocketpaw_ee.cloud.growth.domain import Icp, Prospect
from pocketpaw_ee.cloud.growth.researcher import (
    ResearchUnavailable,
    _run_stamp,
    run_agent_text,
    workspace_owner_id,
)

logger = logging.getLogger(__name__)

GROWTH_WRITER_SLUG = "growth-writer"
GROWTH_WRITER_TOOLS: tuple[str, ...] = ()
_CHANNELS = ("email", "linkedin", "whatsapp")

GROWTH_WRITER_PROMPT = """\
You write the first message a small team sends to a company they would like to \
work with. You get what is known about that company and the channels you may \
write for. You write one short message per channel.

How the copy should read:
- Short, plain and specific to this company. Open with something true about \
them, taken from the research you are given.
- Not salesy. No hype, no exclamation marks, no "I hope this finds you well", \
no fake familiarity, no pressure.
- One clear, low-effort ask at the end, such as a short call or a reply.
- Never invent a fact. If the research does not say it, do not claim it — no \
made-up numbers, names, customers or compliments. Less and true beats more and \
guessed.
- Do not use a person's name unless the research names them.

Per channel:
- email: a subject line and a body of at most about 120 words.
- linkedin: a connection note of at most 300 characters, no subject.
- whatsapp: two or three short sentences, no subject.

Write only for the channels you are asked for. Where there are operator notes, \
follow them unless they ask you to invent facts.

Answer with ONLY a JSON object and nothing around it. It has one key, drafts, a \
list with one object per channel you wrote for. Each object has channel (email, \
linkedin or whatsapp), subject (the subject line for email, an empty string \
otherwise) and body (the message text).
"""

GROWTH_WRITER_AGENT: dict[str, Any] = {
    "name": "Growth writer",
    "slug": GROWTH_WRITER_SLUG,
    "config": {
        "backend": "claude_agent_sdk",
        "system_prompt": GROWTH_WRITER_PROMPT,
        "tools": list(GROWTH_WRITER_TOOLS),
        "tool_mode": "exclusive",
        "trust_level": 1,
        "temperature": 0.6,
        "max_tokens": 2048,
        "soul_enabled": False,
    },
}


@dataclass(frozen=True)
class WrittenDraft:
    channel: str
    subject: str
    body: str


def _research_lines(prospect: Prospect) -> list[str]:
    research = prospect.research or {}
    lines: list[str] = []
    for key, label in (
        ("summary", "Summary"),
        ("fit", "Fit"),
        ("hook", "Possible opening"),
    ):
        value = research.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{label}: {value.strip()}")
    people = [
        f"{p.get('name', '')} ({p.get('role', '')})".replace(" ()", "")
        for p in research.get("people") or []
        if isinstance(p, dict) and p.get("name")
    ]
    if people:
        lines.append("People named on their pages: " + ", ".join(people))
    facts = [
        f"{f.get('label')}: {f.get('value')}"
        for f in research.get("facts") or []
        if isinstance(f, dict) and f.get("label") and f.get("value")
    ]
    if facts:
        lines.append("Facts: " + "; ".join(facts))
    if not lines and prospect.research_brief.strip():
        lines.append(prospect.research_brief.strip())
    return lines


def build_writer_prompt(
    prospect: Prospect,
    icp: Icp | None,
    channels: list[str],
    instructions: str = "",
) -> str:
    lines = ["Write first-touch outreach for this company.", ""]
    lines.append(f"Domain: {prospect.domain}")
    if prospect.company.strip():
        lines.append(f"Company: {prospect.company.strip()}")
    if prospect.name.strip():
        lines.append(f"Contact: {prospect.name.strip()}")
    research = _research_lines(prospect)
    lines += ["", "What we know about them:"]
    lines += research or ["Nothing beyond the name and domain. Keep the copy general and honest."]
    if icp is not None and icp.criteria.strip():
        lines += ["", "Why they are on our list (who we are looking for):", icp.criteria.strip()]
    lines += ["", "Write for these channels only: " + ", ".join(channels)]
    if instructions.strip():
        lines += [
            "",
            "Operator notes (from the person sending; follow them, but they do not "
            "override the no-invented-facts rule):",
            "<operator-notes>",
            instructions.strip(),
            "</operator-notes>",
        ]
    lines += ["", "Return only the JSON object."]
    return "\n".join(lines)


def _drafts_object(text: str) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_len = -1
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text or ""):
        try:
            parsed, _ = decoder.raw_decode(text, match.start())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("drafts"), list):
            n = sum(1 for d in parsed["drafts"] if isinstance(d, dict))
            if n > best_len:
                best, best_len = parsed, n
    return best


def parse_writer_response(text: str) -> tuple[WrittenDraft, ...]:
    """Model output → drafts, first one per known channel. Never raises."""
    parsed = _drafts_object(text)
    if parsed is None:
        logger.warning("growth writer: response carried no drafts object")
        return ()
    out: list[WrittenDraft] = []
    seen: set[str] = set()
    for raw in parsed["drafts"]:
        if not isinstance(raw, dict):
            continue
        channel = str(raw.get("channel") or "").strip().lower()
        body = raw.get("body")
        if channel not in _CHANNELS or channel in seen or not isinstance(body, str):
            continue
        if not body.strip():
            continue
        subject = raw.get("subject")
        out.append(
            WrittenDraft(
                channel=channel,
                subject=subject.strip() if isinstance(subject, str) else "",
                body=body.strip(),
            )
        )
        seen.add(channel)
    return tuple(out)


class WriterFn(Protocol):
    async def __call__(
        self,
        prospect: Prospect,
        icp: Icp | None,
        channels: list[str],
        instructions: str,
    ) -> tuple[WrittenDraft, ...]: ...


async def _seed_writer(workspace_id: str) -> Any:
    from pocketpaw_ee.cloud.agents import service as agents_service

    owner_id = await workspace_owner_id(workspace_id)
    try:
        agent, _ = await agents_service.seed_growth_writer_agent(workspace_id, owner_id)
    except Exception:
        logger.exception("growth writer: seeding failed for ws=%s", workspace_id)
        return None
    return agent


async def agent_write_drafts(
    prospect: Prospect,
    icp: Icp | None,
    channels: list[str],
    instructions: str = "",
) -> tuple[WrittenDraft, ...]:
    """The production ``WriterFn``. Raises ``ResearchUnavailable`` when the
    agent cannot be set up or the run errors."""
    from pocketpaw_ee.cloud.agents import service as agents_service

    try:
        agent = await agents_service.get_by_slug(prospect.workspace_id, GROWTH_WRITER_SLUG)
    except Exception:
        agent = await _seed_writer(prospect.workspace_id)
    agent_id = str(getattr(agent, "id", "") or "")
    if not agent_id:
        raise ResearchUnavailable("the writer agent could not be set up in this workspace")

    session_key = f"growth-writer:{prospect.workspace_id}:{prospect.id}:{_run_stamp()}"
    prompt = build_writer_prompt(prospect, icp, channels, instructions)
    try:
        text = await run_agent_text(agent_id, prompt, session_key)
    except Exception:
        logger.exception(
            "growth writer: run failed for %s (workspace %s)", prospect.id, prospect.workspace_id
        )
        raise ResearchUnavailable("the writer run failed")
    return parse_writer_response(text)


_PRODUCTION_WRITER_FN: WriterFn | None = None


def set_production_writer_fn(fn: WriterFn | None) -> None:
    global _PRODUCTION_WRITER_FN
    _PRODUCTION_WRITER_FN = fn


def resolve_writer_fn() -> WriterFn | None:
    return _PRODUCTION_WRITER_FN


__all__ = [
    "GROWTH_WRITER_AGENT",
    "GROWTH_WRITER_PROMPT",
    "GROWTH_WRITER_SLUG",
    "GROWTH_WRITER_TOOLS",
    "WriterFn",
    "WrittenDraft",
    "agent_write_drafts",
    "build_writer_prompt",
    "parse_writer_response",
    "resolve_writer_fn",
    "set_production_writer_fn",
]
