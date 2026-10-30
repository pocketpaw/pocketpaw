# Instinct outcome verification — deterministic success-criteria checker.
# Created: 2026-05-21 (feat/instinct-outcome-verification)
#
# Issue #1162, foundation half. Spotify's background-coding-agent post ends
# on an honest admission: "even if we make it to a merged PR, how do we know
# if it actually solved the original problem?" A completed action is an
# output. Whether it solved the problem is an outcome — and they are not the
# same thing.
#
# This module is the FOUNDATION for answering that: a deterministic verifier
# that checks an action's result text against the success_criteria captured
# at task intake (deep_work issue #1161) and returns a structured
# OutcomeVerdict. "Deterministic" means it uses plain token matching — no
# LLM, no model call, fully repeatable. The sophisticated half, LLM-as-judge
# scoring for soft / subjective criteria, is deliberately out of scope here
# and tracked as a separate follow-up issue.
#
# Public API:
#   verify_outcome(result, success_criteria) -> OutcomeVerdict
#   check_criterion(result, criterion) -> CriterionResult

from __future__ import annotations

import re
from typing import Any

from pocketpaw.instinct.models import (
    CriterionResult,
    OutcomeStatus,
    OutcomeVerdict,
)

# Tokens shorter than this are ignored when matching a criterion against a
# result — "a", "is", "the" carry no signal and would match everything.
_MIN_TOKEN_LEN = 3

# Words that pass the length filter but carry no content signal: articles,
# conjunctions, auxiliary verbs, and — importantly — quantifiers like "one",
# "per", "each", "all". A criterion such as "one email is sent per invoice"
# keys off "email", "sent", "invoice"; the quantifiers are grammatical glue,
# and demanding the result echo them verbatim produces false negatives.
# Kept deliberately small — this is a deterministic foundation, not a search
# engine — but it has to cover quantifiers to be usable.
_STOPWORDS = frozenset(
    {
        # articles / conjunctions / prepositions
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "its",
        "his",
        "her",
        "not",
        # auxiliary / modal verbs
        "are",
        "was",
        "has",
        "have",
        "been",
        "will",
        "should",
        "must",
        "when",
        # quantifiers — grammatical glue, not content
        "one",
        "per",
        "each",
        "all",
        "any",
        "every",
        "some",
        "least",
        "most",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _stem(token: str) -> str:
    """Strip a common plural suffix so "email"/"emails" match.

    Deliberately crude — a deterministic suffix trim, not a real stemmer.
    The rules, in order:

      - ``-ies`` → ``-y`` ("categories" → "category")
      - ``-es`` → drop ``es`` only after a sibilant (s/x/z/ch/sh), where
        the ``e`` is part of the plural marker ("boxes" → "box",
        "matches" → "match")
      - ``-s`` → drop ``s`` otherwise ("invoices" → "invoice",
        "emails" → "email")

    This collapses regular plurals without over-trimming words like
    "invoices" (which must become "invoice", not "invoic"). Tokens of
    three characters or fewer are left alone.
    """
    if len(token) <= 3:
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith("es") and token[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokenize(text: str) -> list[str]:
    """Lowercase a string into significant, stemmed word tokens.

    Drops short tokens and stopwords so the matcher keys off the content
    words of a criterion ("invoice", "overdue", "email") rather than
    grammatical filler, then stems each so plural and singular forms match.
    """
    tokens = _WORD_RE.findall(text.lower())
    return [_stem(t) for t in tokens if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS]


def _result_to_text(result: Any) -> str:
    """Flatten an action result into a single searchable string.

    A result may be a plain string, or a dict / list (e.g. structured tool
    output). We stringify the whole thing — the verifier only needs the
    text content, not the shape.
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return " ".join(_result_to_text(v) for v in result.values())
    if isinstance(result, (list, tuple)):
        return " ".join(_result_to_text(v) for v in result)
    return str(result)


def check_criterion(result: Any, criterion: str) -> CriterionResult:
    """Check a single success criterion against an action result.

    Deterministic rule: the criterion is *met* when every significant token
    of the criterion appears somewhere in the result text. This is a
    foundation-level check — it catches "the result clearly mentions what
    the criterion asked for" and nothing subtler. A criterion with no
    significant tokens (e.g. punctuation only) cannot be verified and is
    reported as not met with an explanatory detail.

    Args:
        result: The action result — string, dict, or list.
        criterion: One "this is done when…" statement.

    Returns:
        A :class:`CriterionResult` with the met verdict and a short detail.
    """
    result_text = _result_to_text(result)
    result_tokens = set(_tokenize(result_text))
    criterion_tokens = _tokenize(criterion)

    if not criterion_tokens:
        return CriterionResult(
            criterion=criterion,
            met=False,
            detail="Criterion has no checkable content",
        )

    missing = [t for t in criterion_tokens if t not in result_tokens]
    if not missing:
        return CriterionResult(
            criterion=criterion,
            met=True,
            detail="All criterion keywords found in the result",
        )

    matched = len(criterion_tokens) - len(missing)
    return CriterionResult(
        criterion=criterion,
        met=False,
        detail=(
            f"Matched {matched}/{len(criterion_tokens)} keywords; missing: {', '.join(missing)}"
        ),
    )


def verify_outcome(result: Any, success_criteria: list[str]) -> OutcomeVerdict:
    """Verify an action result against its captured success criteria.

    This is the deterministic foundation verifier for issue #1162. It checks
    each criterion with :func:`check_criterion` and rolls the per-criterion
    results into a single :class:`OutcomeVerdict`:

      - every criterion met → ``SOLVED``
      - some met, some not → ``PARTIAL``
      - none met → ``NOT_SOLVED``
      - no criteria captured → ``UNKNOWN`` (there is nothing to check —
        a bare completion, exactly the gap issue #1162 calls out)

    Args:
        result: The action result to verify — string, dict, or list.
        success_criteria: The verifiable end-state checks captured at task
            intake. May be empty.

    Returns:
        An :class:`OutcomeVerdict`. ``summary`` carries a one-line
        human-readable roll-up; ``criteria_results`` carries the detail.
    """
    criteria = [c for c in (success_criteria or []) if c and c.strip()]

    if not criteria:
        return OutcomeVerdict(
            status=OutcomeStatus.UNKNOWN,
            criteria_results=[],
            summary="No success criteria were captured — outcome cannot be verified",
        )

    results = [check_criterion(result, c) for c in criteria]
    met = sum(1 for r in results if r.met)
    total = len(results)

    if met == total:
        status = OutcomeStatus.SOLVED
    elif met == 0:
        status = OutcomeStatus.NOT_SOLVED
    else:
        status = OutcomeStatus.PARTIAL

    return OutcomeVerdict(
        status=status,
        criteria_results=results,
        summary=f"{met}/{total} success criteria met",
    )
