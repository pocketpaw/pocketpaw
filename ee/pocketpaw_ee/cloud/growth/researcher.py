# ee/pocketpaw_ee/cloud/growth/researcher.py — the production ``ResearchFn``:
# the agent that actually goes looking, and the definition of that agent.
# Created 2026-07-29 (feat/growth-research-agent).
#
# Discovery shipped with an injectable ``ResearchFn`` seam and nothing behind
# it, so the cron was a logged no-op. This is the thing behind it.
#
# TWO HALVES:
#   * ``GROWTH_RESEARCHER_AGENT`` — a DECLARATIVE definition (name, slug,
#     backend, tool surface, system prompt). Data, not code, so it can be
#     seeded into a workspace, exported, and imported into another paw-os
#     install. Anything that had to be a function here would not travel.
#   * ``agent_research`` — the ``ResearchFn``. Builds a prompt from the
#     ``ResearchRequest``, runs the agent, parses the JSON it returns into a
#     ``ResearchResult``.
#
# THE TOOL SURFACE IS THE SAFETY PROPERTY, and it is why ``tool_mode`` is
# ``exclusive`` rather than the default ``additive``:
#
#   additive  = the agent's tools UNIONed with the universal MCP grant. On this
#               deployment that hands the researcher Write, Bash, and the growth
#               MCP tools — including ``growth_upsert_prospect``.
#   exclusive = the run's surface is capped to exactly ``tools``.
#
# A researcher holding ``growth_upsert_prospect`` could file a prospect
# DIRECTLY, which routes around ``recordable_emails`` — the single function
# that decides whether an address is real enough to store. The whole email
# guarantee would become a request in a prompt rather than a property of the
# system. So: the agent REPORTS, and ``run_discovery`` decides what is stored.
# Same shape as the send gate one layer up (the agent proposes, a human
# disposes); here the engine disposes.
#
# WebSearch and WebFetch need no wiring — the Claude SDK backend already grants
# both (``src/pocketpaw/agents/claude_sdk.py``). This is a persona and a policy,
# not plumbing.

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pocketpaw_ee.cloud.growth.discovery import (
    DiscoveredCompany,
    ResearchRequest,
    ResearchResult,
)
from pocketpaw_ee.cloud.growth.domain import EmailEvidence

logger = logging.getLogger(__name__)

# The agent's slug. Resolved per workspace at run time — a workspace without
# the agent seeded gets a clean "not available" rather than a crash.
GROWTH_RESEARCHER_SLUG = "growth-researcher"

# The ONLY tools this agent may hold. Pinned by a test: widening this list is a
# deliberate act that has to break something first.
GROWTH_RESEARCHER_TOOLS: tuple[str, ...] = ("WebSearch", "WebFetch")

# The research discipline. Written at the model, not about it.
#
# The email rule gets the most words on purpose. Everything else here degrades
# gracefully when the model does it badly — a weak research brief is a weak
# row a human skims past. A fabricated email address does not degrade: it is
# indistinguishable from a real one until it bounces, and the bounce lands on
# the sending domain's reputation, which is shared by every other client in the
# workspace. Hence the explicit worked example of the failure mode, rather than
# a polite instruction not to guess.
GROWTH_RESEARCHER_PROMPT = """\
You find companies that match a description, by actually searching the web and \
reading pages. You do not send anything, write anything, or change anything — \
you report what you found and someone else decides what to do with it.

## What you are looking for

You will be given a description of a kind of business, optionally a place, and \
optionally a list of who to skip. Find companies that genuinely match it. A \
company you are not confident about is better reported with an honest note \
than silently dropped or quietly inflated.

## The rules

**Report only what you actually read.** Every company you report must come \
from a page you actually opened. Include the URL. A company with no source URL \
is not a finding — do not report it.

**Never construct an email address.** This is the one rule with no judgement in \
it. Do not infer an address from a pattern. If you see that the company is \
"Northwind Dental" at northwinddental.com, and you have seen `priya@` used \
somewhere, you may NOT report `priya@northwinddental.com` — you did not read \
that string on a page, you assembled it.

Report an address only if you literally saw that exact string on a page you \
opened, and give the URL where you saw it. Mark it `observed`.

If you did not see an address, report none. **That is a correct and expected \
outcome, not a failure.** A guessed address bounces; bounces damage the domain \
this workspace sends all of its mail from; a damaged domain hurts every client \
it sends for. An empty email field costs nothing by comparison.

**Skip what you were told to skip.** If exclusions are given, honour them.

## What is actually valuable

The qualification is worth more than the contact details. For each company, \
say what they actually do, why they fit the description, and what a first \
approach might reasonably open with. Specifics from their own site beat \
adjectives. "Still shows a 2019 portfolio and lists WordPress maintenance as a \
retainer" is useful; "innovative digital agency" is noise.

## How to answer

Return ONLY a JSON object, no prose around it, in exactly this shape:

{
  "companies": [
    {
      "domain": "example.com",
      "company": "Example Ltd",
      "name": "contact person's name, or empty string if you did not find one",
      "research_brief": "what they do, why they fit, what the hook is",
      "source_urls": ["https://example.com/about"],
      "emails": [
        {
          "address": "hello@example.com",
          "confidence": "observed",
          "source_url": "https://example.com/contact"
        }
      ]
    }
  ],
  "notes": "anything about the search itself worth logging"
}

`domain` is the only required field. `emails` is usually an empty list — that \
is normal. `notes` is for the run log ("three directories were paywalled"), \
never a claim about a specific company.
"""

# The agent, as data. Seed this into a workspace to make discovery available
# there; export it to move the agent to another install.
#
# ``trust_level`` 1: this agent reads public web pages and returns text. It
# holds no credentials, touches no tenant data, and cannot write. There is
# nothing here that warrants a higher trust tier, and a low one keeps it out of
# any surface that gates on trust.
GROWTH_RESEARCHER_AGENT: dict[str, Any] = {
    "name": "Growth researcher",
    "slug": GROWTH_RESEARCHER_SLUG,
    "config": {
        "backend": "claude_agent_sdk",
        "system_prompt": GROWTH_RESEARCHER_PROMPT,
        "tools": list(GROWTH_RESEARCHER_TOOLS),
        # See the module header. This is the safety property, not a preference.
        "tool_mode": "exclusive",
        "trust_level": 1,
        # Low temperature: this is extraction and judgement over pages that
        # exist, not composition. Invention is the failure mode.
        "temperature": 0.2,
        "max_tokens": 8192,
        # A researcher accumulating "memories" across unrelated hunts is a
        # route for one workspace's findings to colour another's. Each run is
        # independent by design.
        "soul_enabled": False,
    },
}


def build_research_prompt(request: ResearchRequest) -> str:
    """The per-run instruction. The standing discipline lives in the system
    prompt; this carries only what changes between runs."""
    lines = [f"Find up to {request.max_results} companies matching this description:", ""]
    lines.append(request.criteria.strip())
    if request.geography.strip():
        lines.append("")
        lines.append(f"Where: {request.geography.strip()}")
    if request.exclusions.strip():
        lines.append("")
        lines.append(f"Skip: {request.exclusions.strip()}")
    lines.append("")
    lines.append("Return the JSON object described in your instructions and nothing else.")
    return "\n".join(lines)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the JSON object out of a model response.

    Tolerant on purpose. A model that wraps its answer in a fenced block, or
    prefixes it with a sentence, has still done the job — refusing that would
    throw away a whole run's research over formatting. What it will NOT do is
    guess at malformed JSON: unparseable means zero findings, never a partial
    object assembled by hand.
    """
    if not text or not text.strip():
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _evidence_from(raw: Any) -> EmailEvidence | None:
    """One reported address → evidence, or None.

    THE ASYMMETRY THAT MATTERS: the model's own ``confidence`` is carried
    through verbatim and never upgraded. A response that omits the field, or
    sends something unrecognised, lands on ``EmailEvidence``'s default —
    ``guessed`` — which ``recordable_emails`` drops. So every path that isn't
    an explicit, sourced claim of having seen the address fails closed.

    An address with no ``source_url`` is refused here regardless of what the
    model claimed about it: "I saw it" without "here" is not evidence.
    """
    if not isinstance(raw, dict):
        return None
    address = str(raw.get("address") or "").strip()
    source_url = str(raw.get("source_url") or "").strip()
    if not address or not source_url:
        return None
    confidence = str(raw.get("confidence") or "").strip().lower()
    if not confidence:
        return None
    return EmailEvidence(address=address, confidence=confidence, seen_at_url=source_url)


def _company_from(raw: Any) -> DiscoveredCompany | None:
    """One reported company → a discovered company, or None.

    ``domain`` is the only field that can fail the row: it is the dedupe
    identity, and a company nobody can look up is not a lead. Everything else
    degrades to empty, because a bare domain with the page it was found on is
    still a perfectly good row for a human to qualify.
    """
    if not isinstance(raw, dict):
        return None
    domain = str(raw.get("domain") or "").strip().lower()
    if not domain:
        return None
    urls = raw.get("source_urls")
    source_urls = (
        tuple(str(u).strip() for u in urls if isinstance(u, str) and str(u).strip())
        if isinstance(urls, list)
        else ()
    )
    emails_raw = raw.get("emails")
    emails = (
        tuple(e for e in (_evidence_from(item) for item in emails_raw) if e is not None)
        if isinstance(emails_raw, list)
        else ()
    )
    return DiscoveredCompany(
        domain=domain,
        name=str(raw.get("name") or "").strip(),
        company=str(raw.get("company") or "").strip(),
        research_brief=str(raw.get("research_brief") or "").strip(),
        source_urls=source_urls,
        emails=emails,
    )


def parse_research_response(text: str, *, max_results: int) -> ResearchResult:
    """Model output → ``ResearchResult``. Never raises.

    Every failure mode here — no JSON, wrong shape, a company with no domain,
    an address with no source — degrades to fewer findings. A research pass
    that returns nothing is a legitimate outcome the caller already handles;
    an exception here would turn one bad response into a dead cron tick for
    every other workspace in the same sweep.
    """
    parsed = _extract_json(text)
    if parsed is None:
        logger.warning("growth researcher: response carried no parseable JSON object")
        return ResearchResult(notes="the researcher's response could not be read")
    raw_companies = parsed.get("companies")
    if not isinstance(raw_companies, list):
        return ResearchResult(notes=str(parsed.get("notes") or ""))
    companies = tuple(c for c in (_company_from(item) for item in raw_companies) if c is not None)
    # The cap is the caller's request, not a promise the model kept.
    return ResearchResult(
        companies=companies[:max_results],
        notes=str(parsed.get("notes") or ""),
    )


async def agent_research(request: ResearchRequest) -> ResearchResult:
    """The production ``ResearchFn`` — run the researcher agent for one ICP.

    Returns an empty result rather than raising on any failure: the agent is
    missing from the workspace, the run errors, the response is unreadable.
    ``run_discovery`` already treats a zero-result pass as a normal outcome,
    and one workspace's broken run must not end the sweep for the rest.
    """
    # Lazy, in-function import of the OSS package — the convention every cloud
    # module reaching into ``pocketpaw.agents`` follows (see
    # ``shared/agent_bridge.py``), which keeps the EE→OSS edge off the module
    # import graph.
    from pocketpaw.agents.pool import get_agent_pool

    # The agents entity owns the Agent document — growth resolves the
    # researcher through its public service rather than reading the doc, which
    # would put a second entity's Beanie import inside growth/service.py.
    from pocketpaw_ee.cloud.agents import service as agents_service

    try:
        agent = await agents_service.get_by_slug(request.workspace_id, GROWTH_RESEARCHER_SLUG)
    except Exception:
        logger.warning(
            "growth researcher: workspace %s has no '%s' agent seeded — discovery is idle for it",
            request.workspace_id,
            GROWTH_RESEARCHER_SLUG,
        )
        return ResearchResult(notes="no researcher agent is available in this workspace")
    agent_id = str(getattr(agent, "id", "") or "")
    if not agent_id:
        return ResearchResult(notes="no researcher agent is available in this workspace")

    prompt = build_research_prompt(request)
    # One session per ICP run. Deliberately not a stable per-ICP key: a hunt
    # that ran yesterday must not carry yesterday's page reads into today's
    # judgement — each run should stand on what it read this time.
    session_key = f"growth-discovery:{request.workspace_id}:{request.icp_id}"

    chunks: list[str] = []
    try:
        pool = get_agent_pool()
        async for event in pool.run(
            agent_id=agent_id,
            message=prompt,
            session_key=session_key,
        ):
            content = getattr(event, "content", None)
            if isinstance(content, str) and content:
                chunks.append(content)
    except Exception:
        logger.exception(
            "growth researcher: the run failed for icp %s (workspace %s)",
            request.icp_id,
            request.workspace_id,
        )
        return ResearchResult(notes="the research run failed")

    return parse_research_response("".join(chunks), max_results=request.max_results)


__all__ = [
    "GROWTH_RESEARCHER_AGENT",
    "GROWTH_RESEARCHER_PROMPT",
    "GROWTH_RESEARCHER_SLUG",
    "GROWTH_RESEARCHER_TOOLS",
    "agent_research",
    "build_research_prompt",
    "parse_research_response",
]
