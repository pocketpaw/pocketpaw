# skills_loop/rubric.py — anti-pattern rubric for the session reviewer.
# Created: 2026-06-16 (feat/self-improving-skills) — ports the spirit of
#   Hermes's reviewer rubric. Captures the prompt the forked reviewer runs under
#   AND a deterministic code-side guard (``is_rubric_banned``) that blocks the
#   worst failure modes BEFORE a write, so a misbehaving LLM can't harden a
#   self-cited refusal into the soul. The three banned classes:
#     1. environment / infrastructure failures ("the network was down")
#     2. "tool X is broken" claims (they become self-cited refusals)
#     3. one-off task narratives (per-customer / per-date / per-bug stories)

from __future__ import annotations

import re

# The system prompt the forked write-only reviewer runs under. It frames the
# job (learn a REUSABLE procedure, not a story) and enumerates the bans.
REVIEWER_SYSTEM_PROMPT = """\
You are a write-only session reviewer for a workspace AI agent. You have read
the agent's session transcript. Your ONLY capability is writing a learned
procedure into the agent's persistent procedural memory (the soul-write tool).
You cannot run commands, read files, or call any other tool.

Capture at most a few GENERALIZABLE, REUSABLE procedures — durable how-tos the
agent should apply in future sessions. A good procedure is phrased as a
repeatable instruction ("To do X, run Y", "When Z happens, check W").

Do NOT capture any of the following — they poison future sessions:
  1. Environment or infrastructure failures (network down, disk full,
     permission denied, service unavailable, rate-limited). These are
     transient, not learnings.
  2. "Tool X is broken / always fails / don't use it" claims. These harden
     into self-cited refusals where the agent stops using a working tool.
  3. One-off task narratives — anything tied to a specific customer, date,
     ticket, or bug fix ("On <date> we fixed <bug> for <customer>"). These are
     episodic events, not procedures.

If nothing in the session is worth durably remembering, write nothing.
"""

# Banned-pattern detectors. Each returns a short reason when it matches.
_ENV_FAILURE_RE = re.compile(
    r"\b("
    r"network (was )?(down|unavailable)|"
    r"disk (is )?full|"
    r"permission denied|"
    r"environment (is )?(misconfigured|broken)|"
    r"service (is )?(down|unavailable)|"
    r"rate.?limit|"
    r"timed? out|timeout|"
    r"connection (refused|reset|failed)"
    r")\b",
    re.IGNORECASE,
)

_TOOL_BROKEN_RE = re.compile(
    r"\btool\b.{0,40}\b(broken|is broken|always (errors|fails)|doesn'?t work|never works)\b"
    r"|\b\w+ tool is broken\b"
    r"|\b(do not|don'?t|never|avoid) (use|call)(ing)?\b.{0,30}\btool\b",
    re.IGNORECASE,
)

# One-off narrative markers: explicit dates, customer/ticket references, or
# past-tense "we fixed/resolved <thing>" stories.
_ONE_OFF_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"  # an ISO date → episodic
    r"|\b(we|i) (fixed|resolved|debugged|patched) (the |a )?\w+ (bug|issue|ticket)\b"
    r"|\bfor (customer|client|account|tenant) [A-Z]\w+\b"
    r"|\bticket #?\d+\b",
    re.IGNORECASE,
)


def is_rubric_banned(procedure_text: str) -> tuple[bool, str | None]:
    """Return ``(True, reason)`` if ``procedure_text`` violates the rubric.

    Deterministic belt-and-suspenders guard applied to every candidate
    procedure BEFORE it is written, regardless of what the LLM proposed. A
    clean procedure returns ``(False, None)``.
    """
    text = (procedure_text or "").strip()
    if not text:
        return True, "empty procedure"
    if _TOOL_BROKEN_RE.search(text):
        return True, "tool-broken claim (becomes a self-cited refusal)"
    if _ENV_FAILURE_RE.search(text):
        return True, "environment/infrastructure failure (transient, not a learning)"
    if _ONE_OFF_RE.search(text):
        return True, "one-off task narrative (episodic, not a reusable procedure)"
    return False, None


__all__ = ["REVIEWER_SYSTEM_PROMPT", "is_rubric_banned"]
