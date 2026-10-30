# ee/pocketpaw_ee/cloud/belt/clef.py — Belt's consult of Cloudflare's Clef.
#
# Clef (``@cf/cloudflare/clef`` on Workers AI) is a decision model: it takes a
# ``state`` plus typed questions (``noul`` yes/no, ``choice``, ``score``) and
# returns a probability per answer. Belt asks it about the foreman's PLANS, the
# station's DIFFS and the FINDINGS of its reviewer, tester and Review Room, and
# records each verdict on the run's or plan's Instinct Action blob (``clef`` on
# ``_belt_plan`` / ``_code_change``; findings append to ``clef_findings``). Diff
# verdicts are shadow only; a plan verdict decides only under the auto plan gate
# (``mandates/plan_policy.py``); finding verdicts decide only with
# ``POCKETPAW_BELT_CLEF_FINDINGS=live``. The eval lists verdicts beside what the
# human, the executor or the issue's closer did (``list_verdicts``,
# ``resolve_outcomes``, ``agreement``, ``scripts/clef_verdicts.py``).
#
#   * ``ClefClient.ask`` — one POST, 20 s timeout, one retry on a 5xx; returns
#     ``None`` on any failure (timeout, 4xx, ``success:false``, bad JSON).
#   * ``score_plan`` / ``score_diff`` / ``score_finding`` — question builders plus
#     one ask. A plan state carries its cited issues' text and one ``spec_clear_<i>``
#     per task; a diff state the file list plus the head (it says when it cut); a
#     finding asks ``blocking``, ``mechanical`` and, with candidates (the open
#     issues sharing most words, ``candidate_issues``), a ``duplicate`` choice.
#   * ``shadow_plan`` / ``shadow_diff`` — fire-and-forget hooks; the auto plan
#     gate awaits ``shadow_plan``'s task (shielded: a timeout still records).
#   * ``judge_review`` / ``judge_tester_bugs`` / ``judge_one`` — the finding
#     hooks, awaited inline (``FINDINGS_WAIT_S``). Live, ``decide`` holds a finding
#     at ``blocking`` >= 0.5 or with no score, else it becomes a follow-up issue;
#     ``audit-rule`` over 0.7 ``mechanical``; over 0.8 ``duplicate`` comments on
#     that issue instead. ``round_clean`` / ``loop_decision`` are the stop rule
#     for a loop until clean (2 clean rounds in a row, else escalate at a cap).
#
# Off unless ``POCKETPAW_BELT_CLEF=shadow`` and both ``POCKETPAW_CLEF_TOKEN`` and
# ``POCKETPAW_CLEF_ACCOUNT_ID`` are set. INVARIANTS: nothing here raises into a
# shift or a run; the token and the account id (it sits in the URL) never reach
# a log line or a trace span (errors log the exception type and status only;
# requests run under ``logfire.suppress_instrumentation``). The blob carries
# only the verdict numbers, never the state or a finding's text, because
# ``list_runs`` reads every ``_code_change`` blob in bulk.

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

FLAG = "POCKETPAW_BELT_CLEF"
TIMEOUT_S = 20.0
MODEL = "clef"
_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/clef"
# Clef truncates long state itself; we cut first so the cut is ours and named.
MAX_STATE_CHARS = 24_000
# The cited issues' text in a plan state: per issue, and in all.
_CITED_CHARS = 1_500
_CITED_TOTAL = 8_000
_PARAM_KEYS = {"plan": "_belt_plan", "diff": "_code_change"}

# Tests swap in an ``httpx.MockTransport``.
_transport: httpx.AsyncBaseTransport | None = None
# Strong refs so a fire-and-forget task is not collected mid-flight.
_TASKS: set[asyncio.Task[Any]] = set()


@dataclass(frozen=True)
class Question:
    type: str  # "noul" | "choice" | "score"
    instructions: str
    criteria: dict[str, Any] | list[Any] | None = None

    def wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            out["criteria"] = self.criteria
        return out


@dataclass(frozen=True)
class Answer:
    type: str
    noul: float | None = None
    choice: str | None = None
    score: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Answer:
        return cls(
            type=str(raw.get("type") or ""),
            noul=raw.get("noul"),
            choice=raw.get("choice"),
            score=raw.get("score"),
            probabilities=dict(raw.get("probabilities") or {}),
            confidence=raw.get("confidence"),
        )


@dataclass(frozen=True)
class PlanVerdict:
    in_scope: float
    low_risk: float
    tasks_independent: float
    too_big: float
    truncated: bool
    # Per task: P(its cited issue states a concrete, testable acceptance
    # criterion); -1 = unanswered; () = not asked (verdicts before the question).
    spec_clear: tuple[float, ...] = ()


@dataclass(frozen=True)
class DiffVerdict:
    duplicates_existing_helper: float
    new_error_type_or_pattern: float
    model_depends_on_upper_layer: float
    adds_model_field_without_validate_or_export: float
    tests_assert_behavior: float
    change_kind: str
    change_kind_confidence: float
    truncated: bool


class ClefClient:
    def __init__(
        self, token: str, account_id: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._token = token
        self._url = _URL.format(account=account_id)
        self._transport = transport

    async def ask(self, state: Any, questions: dict[str, Question]) -> dict[str, Answer] | None:
        body = {
            "model": MODEL,
            "state": state,
            "questions": {qid: q.wire() for qid, q in questions.items()},
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=self._transport) as client:
                for attempt in (1, 2):
                    with _suppressed():
                        resp = await client.post(self._url, json=body, headers=headers)
                    if resp.status_code < 500 or attempt == 2:
                        break
                    logger.info("clef: %s on attempt 1, retrying once", resp.status_code)
            if resp.status_code >= 400:
                logger.warning("clef: request refused with %s", resp.status_code)
                return None
            data = resp.json()
            if not data.get("success", True):
                logger.warning("clef: response success=false")
                return None
            answers = (data.get("result") or {}).get("answers") or {}
            return {qid: Answer.parse(a) for qid, a in answers.items() if isinstance(a, dict)}
        except Exception as exc:  # noqa: BLE001 — Clef is advisory; never raise
            # Type only: httpx messages carry the URL, and the URL carries the account id.
            logger.warning("clef: request failed (%s)", type(exc).__name__)
            return None


def _suppressed() -> Any:
    """Keep the Clef POST (its URL holds the account id) out of the traces."""
    try:
        import logfire
    except ImportError:
        return nullcontext()
    return logfire.suppress_instrumentation()


def from_env() -> ClefClient | None:
    """The client when shadow mode is on and both credentials are set, else ``None``."""
    if os.environ.get(FLAG, "").strip().lower() != "shadow":
        return None
    token = os.environ.get("POCKETPAW_CLEF_TOKEN", "").strip()
    account = os.environ.get("POCKETPAW_CLEF_ACCOUNT_ID", "").strip()
    if not token or not account:
        return None
    return ClefClient(token, account, transport=_transport)


def _noul(text: str, yes: str, no: str) -> Question:
    return Question("noul", text, {"true": yes, "false": no})


def _p(answers: dict[str, Answer], qid: str) -> float:
    a = answers.get(qid)
    return float(a.noul) if a is not None and a.noul is not None else -1.0


def _cap(text: str, limit: int = MAX_STATE_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n[truncated: showing the first {limit} of {len(text)} chars]", True


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------


def plan_questions(tasks: int = 0) -> dict[str, Question]:
    """The plan questions, plus ``spec_clear_<i>`` for each of ``tasks`` tasks."""
    spec = {
        f"spec_clear_{i}": _noul(
            f"Does task {i + 1} in the plan cite an issue that states a concrete, testable "
            "acceptance criterion (see cited_issues)?",
            "its issue states a concrete, testable acceptance criterion",
            "its acceptance criterion is missing, vague or not testable",
        )
        for i in range(tasks)
    }
    return {
        "in_scope": _noul(
            "Does every task in the plan stay inside the mandate scope and respect the rubric?",
            "every task is in scope",
            "at least one task is out of scope or breaks a boundary",
        ),
        "low_risk": _noul(
            "Is this plan low risk to ship (no auth, data loss, migrations or wide blast radius)?",
            "low risk",
            "risky",
        ),
        "tasks_independent": _noul(
            "Can each task be built and landed independently of the other tasks in this plan?",
            "independent",
            "some task depends on another task in this plan",
        ),
        "too_big": _noul(
            "Is any task too big for one focused change (more than a few files or one concern)?",
            "at least one task is too big",
            "every task is small and focused",
        ),
        **spec,
    }


async def score_plan(
    plan_tasks: list[dict[str, Any]],
    mandate_scope: str,
    rubric_text: str,
    *,
    client: ClefClient | None = None,
    evidence: dict[str, str] | None = None,
) -> PlanVerdict | None:
    """``evidence``: the text of each sighting (issue) a task cites, by id."""
    client = client or from_env()
    if client is None:
        return None
    tasks_text, truncated = _cap(json.dumps(plan_tasks, default=str, indent=1))
    cited = "\n\n".join(f"[{sid}]\n{text[:_CITED_CHARS]}" for sid, text in (evidence or {}).items())
    state = {
        "mandate_scope": mandate_scope,
        "rubric": rubric_text,
        "plan_tasks": tasks_text,
        "cited_issues": _cap(cited, _CITED_TOTAL)[0] or "(no cited issue text)",
    }
    answers = await client.ask(state, plan_questions(len(plan_tasks)))
    if answers is None:
        return None
    return PlanVerdict(
        in_scope=_p(answers, "in_scope"),
        low_risk=_p(answers, "low_risk"),
        tasks_independent=_p(answers, "tasks_independent"),
        too_big=_p(answers, "too_big"),
        truncated=truncated,
        spec_clear=tuple(_p(answers, f"spec_clear_{i}") for i in range(len(plan_tasks))),
    )


# ---------------------------------------------------------------------------
# diffs
# ---------------------------------------------------------------------------

CHANGE_KINDS = {
    "feature": "adds new user-facing or API behavior",
    "fix": "corrects a bug in existing behavior",
    "refactor": "restructures code without changing behavior",
    "test": "only adds or changes tests",
    "docs": "only docs or comments",
    "chore": "dependencies, config, build or tooling",
}

DEFAULT_DIFF_RUBRIC = (
    "Reuse existing helpers before adding new ones. Do not invent new error types or "
    "patterns where one exists. Lower layers (models, domain) never import upper layers "
    "(services, routers). A new model field is validated and exported where the model is. "
    "Tests assert behavior, not implementation details or mocks."
)

_FILE_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def diff_state(diff_text: str) -> tuple[str, bool]:
    """The file list, then the diff head; says so when the head was cut."""
    files = [b for _a, b in _FILE_RE.findall(diff_text)]
    head = f"Files changed ({len(files)}):\n" + "\n".join(f"- {f}" for f in files) + "\n\n"
    body, truncated = _cap(diff_text, max(MAX_STATE_CHARS - len(head), 2_000))
    if truncated:
        body += " (the full file list is above)"
    return head + body, truncated


def diff_questions() -> dict[str, Question]:
    return {
        "duplicates_existing_helper": _noul(
            "Does the diff add a function, class or module that duplicates one the known "
            "helpers list or the repo already has?",
            "it duplicates an existing helper",
            "it reuses or adds something genuinely new",
        ),
        "new_error_type_or_pattern": _noul(
            "Does the diff introduce a new exception type or a new way of doing something "
            "the codebase already has a pattern for?",
            "new error type or competing pattern",
            "follows the existing patterns",
        ),
        "model_depends_on_upper_layer": _noul(
            "Does a model or domain module in this diff import from a service, router or "
            "other upper layer?",
            "a lower layer imports an upper layer",
            "layering is respected",
        ),
        "adds_model_field_without_validate_or_export": _noul(
            "Does the diff add a model field without validating it or carrying it through "
            "the export, DTO or mapper that the model's other fields go through?",
            "a new field is unvalidated or not exported",
            "no such field, or it is validated and exported",
        ),
        "tests_assert_behavior": _noul(
            "Do the tests in this diff assert observable behavior (not just that mocks were "
            "called)?",
            "tests assert behavior",
            "no tests, or tests only check mocks",
        ),
        "change_kind": Question(
            "choice", "What kind of change is this diff mainly?", dict(CHANGE_KINDS)
        ),
    }


async def score_diff(
    diff_text: str,
    rubric_text: str,
    known_helpers_text: str,
    *,
    client: ClefClient | None = None,
) -> DiffVerdict | None:
    client = client or from_env()
    if client is None:
        return None
    shown, truncated = diff_state(diff_text)
    state = {"rubric": rubric_text, "known_helpers": known_helpers_text, "diff": shown}
    answers = await client.ask(state, diff_questions())
    if answers is None:
        return None
    kind = answers.get("change_kind")
    return DiffVerdict(
        duplicates_existing_helper=_p(answers, "duplicates_existing_helper"),
        new_error_type_or_pattern=_p(answers, "new_error_type_or_pattern"),
        model_depends_on_upper_layer=_p(answers, "model_depends_on_upper_layer"),
        adds_model_field_without_validate_or_export=_p(
            answers, "adds_model_field_without_validate_or_export"
        ),
        tests_assert_behavior=_p(answers, "tests_assert_behavior"),
        change_kind=(kind.choice if kind and kind.choice else ""),
        change_kind_confidence=float(kind.confidence or 0.0) if kind else 0.0,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------

FINDINGS_FLAG = "POCKETPAW_BELT_CLEF_FINDINGS"
AUDIT_RULE = "audit-rule"
BLOCKING_MIN = 0.5  # at or over: the finding holds the run
MECHANICAL_MIN = 0.7  # over: the issue Belt files gets ``audit-rule``
DUPLICATE_MIN = 0.8  # over: comment on that issue instead of filing one
MAX_FINDINGS = 10  # scored per round; the rest go unscored (and hold, live)
MAX_CANDIDATES = 5
FINDINGS_WAIT_S = 45.0  # one round's scoring, all findings at once
_FINDING_CHARS = 4_000
_CONTEXT_CHARS = 6_000
_CANDIDATE_CHARS = 600
_FINDINGS_KEPT = 100  # ``clef_findings`` entries kept on one run's blob
_FILE_LINE = re.compile(r"([\w./-]+\.[A-Za-z0-9]+):\d+")
_PNG_NAME = re.compile(r"[\w.-]+\.png\b")
_WORD = re.compile(r"[a-z0-9_]{4,}")
_ISSUE_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/(\d+)")
_SOURCE_ISSUE = re.compile(r"(?<![\w/&])#(\d+)\b")  # the issue a run's task names
_SOURCES = {
    "review": "Belt's pre-merge code reviewer, judging the PR's diff",
    "tester": "Belt's post-merge tester, trying to break the landed feature",
    "room": "a human trying the built app in the Review Room",
}

# A gh runner: async (argv) -> (returncode, stdout, stderr).
GhFn = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


@dataclass(frozen=True)
class Finding:
    text: str
    source: str  # "review" | "tester" | "room"
    run_id: str
    files: tuple[str, ...] = ()
    screenshots: tuple[str, ...] = ()
    issue: int = 0  # the issue this finding already is (a tester's bug), never its own dup


@dataclass(frozen=True)
class FindingVerdict:
    blocking: float
    mechanical: float
    # P(it duplicates ``duplicate_of``): the ``duplicate`` choice's highest
    # probability over the candidate issues (not "none"); -1 = not asked or
    # unanswered, and then ``duplicate_of`` is 0.
    duplicate: float
    duplicate_of: int
    truncated: bool


@dataclass(frozen=True)
class FindingDecision:
    holds: bool
    audit_rule: bool
    duplicate_of: int  # 0 = file it (or keep it) as its own issue


def findings_live() -> bool:
    """``POCKETPAW_BELT_CLEF_FINDINGS=live``: finding verdicts decide; else shadow."""
    return os.environ.get(FINDINGS_FLAG, "").strip().lower() == "live"


def finding_questions(candidates: Sequence[dict[str, Any]] = ()) -> dict[str, Question]:
    """``blocking`` and ``mechanical``, plus ``duplicate`` when there are candidates."""
    questions = {
        "blocking": _noul(
            "Does this finding break the feature's acceptance test or a review rubric item "
            "(see context), rather than being polish?",
            "it breaks the acceptance test or the rubric; the feature is not done until fixed",
            "it is polish or a nice-to-have; it can be a follow-up",
        ),
        "mechanical": _noul(
            "Could an automated check (a lint rule, a test, or the app audit's pixel and "
            "accessibility checks) catch this whole class of bug, not just this instance?",
            "an automated check could catch the whole class",
            "it needs human judgement",
        ),
    }
    if candidates:
        options = {
            f"issue_{c['number']}": f"it duplicates #{c['number']}: {(c.get('title') or '')[:120]}"
            for c in candidates
        }
        options["none"] = "it duplicates none of the candidate issues"
        questions["duplicate"] = Question(
            "choice", "Which open issue in candidate_issues does this finding duplicate?", options
        )
    return questions


def candidate_issues(
    rows: list[dict[str, Any]], text: str, n: int = MAX_CANDIDATES, exclude: int = 0
) -> list[dict[str, Any]]:
    """The ``n`` open issues sharing the most words (4+ chars) with ``text``;
    none that share no word, never ``exclude``."""
    want = set(_WORD.findall(text.lower()))
    scored = []
    for row in rows:
        number = row.get("number")
        if not isinstance(number, int) or number == exclude:
            continue
        have = set(_WORD.findall(f"{row.get('title') or ''} {row.get('body') or ''}".lower()))
        if shared := len(want & have):
            scored.append((shared, number, row))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [row for _s, _n, row in scored[:n]]


async def score_finding(
    client: ClefClient,
    finding: Finding,
    context: str,
    candidates: Sequence[dict[str, Any]] = (),
) -> FindingVerdict | None:
    """One finding against the run's ``context`` (its task and acceptance test)."""
    text, cut = _cap(finding.text, _FINDING_CHARS)
    ctx, ctx_cut = _cap(context, _CONTEXT_CHARS)
    state = {
        "finding": text,
        "source": _SOURCES.get(finding.source, finding.source),
        "run": finding.run_id,
        "files": "\n".join(finding.files) or "(none named)",
        "screenshots": "\n".join(finding.screenshots) or "(none)",
        "context": ctx or "(no task text)",
        "candidate_issues": "\n\n".join(
            f"#{c['number']} {c.get('title') or ''}\n{str(c.get('body') or '')[:_CANDIDATE_CHARS]}"
            for c in candidates
        )
        or "(no candidates)",
    }
    answers = await client.ask(state, finding_questions(candidates))
    if answers is None:
        return None
    dup, dup_of = -1.0, 0
    picked = answers.get("duplicate")
    if picked is not None:
        probs = {k: float(v) for k, v in picked.probabilities.items() if k != "none"}
        if not probs and picked.choice and picked.choice != "none":
            probs = {picked.choice: float(picked.confidence or 0.0)}
        if probs:
            key = max(probs, key=lambda k: probs[k])
            if key.removeprefix("issue_").isdigit():
                dup, dup_of = probs[key], int(key.removeprefix("issue_"))
        else:
            dup = 0.0
    return FindingVerdict(
        blocking=_p(answers, "blocking"),
        mechanical=_p(answers, "mechanical"),
        duplicate=dup,
        duplicate_of=dup_of,
        truncated=cut or ctx_cut,
    )


def decide(verdict: FindingVerdict | None) -> FindingDecision:
    """The live policy for one finding. No verdict, or an unanswered
    ``blocking``, holds: Clef being down never waves a finding through."""
    if verdict is None:
        return FindingDecision(holds=True, audit_rule=False, duplicate_of=0)
    return FindingDecision(
        holds=not 0 <= verdict.blocking < BLOCKING_MIN,
        audit_rule=verdict.mechanical > MECHANICAL_MIN,
        duplicate_of=verdict.duplicate_of if verdict.duplicate > DUPLICATE_MIN else 0,
    )


def round_clean(verdicts: list[FindingVerdict | None]) -> bool:
    """True when no finding in the round blocks (``decide``): every one scored
    under ``BLOCKING_MIN``. A round with no findings is clean."""
    return not any(decide(v).holds for v in verdicts)


def loop_decision(
    rounds: list[list[FindingVerdict | None]], cap: int, clean_needed: int = 2
) -> str:
    """The stop rule for a loop-until-clean review: ``done`` after
    ``clean_needed`` clean rounds in a row, ``escalate`` (to the captain) once
    ``cap`` rounds ran without that, else ``continue``."""
    tail = rounds[-clean_needed:]
    if len(tail) == clean_needed and all(round_clean(r) for r in tail):
        return "done"
    return "escalate" if len(rounds) >= cap else "continue"


# ---------------------------------------------------------------------------
# recording + the eval read
# ---------------------------------------------------------------------------


async def record_verdict(
    run_or_plan_id: str,
    kind: str,
    verdict: PlanVerdict | DiffVerdict,
    actual_outcome: str | None = None,
    *,
    workspace_id: str,
) -> bool:
    """Write ``clef`` onto the judged Action's blob. Never raises."""
    from pocketpaw.stores import get_instinct_store
    from pocketpaw_ee.cloud._core.proposals import update_action_blob

    record = {
        "kind": kind,
        "model": MODEL,
        "scored_at": datetime.now(UTC).isoformat(),
        "verdict": asdict(verdict),
        "actual_outcome": actual_outcome,
    }
    try:
        store = get_instinct_store(workspace_id=workspace_id or None)
        return await update_action_blob(
            store=store,
            action_id=run_or_plan_id,
            param_key=_PARAM_KEYS[kind],
            updates={"clef": record},
        )
    except Exception as exc:  # noqa: BLE001 — shadow write; never fail the caller
        logger.warning(
            "clef: could not record %s verdict on %s (%s)", kind, run_or_plan_id, type(exc).__name__
        )
        return False


def finding_entry(
    finding: Finding, verdict: FindingVerdict, *, actions: Sequence[str] = (), issue: str = ""
) -> dict[str, Any]:
    """One ``clef_findings`` entry: the numbers, the source, what live mode
    did (``actions``; empty in shadow) and the issue it touched (``owner/repo#n``).
    Never the finding's text: ``list_runs`` reads these blobs in bulk."""
    return {
        "kind": "finding",
        "model": MODEL,
        "scored_at": datetime.now(UTC).isoformat(),
        "source": finding.source,
        "actions": list(actions),
        "issue": issue or None,
        "verdict": asdict(verdict),
        "actual_outcome": None,
    }


async def record_findings(run_id: str, entries: list[dict[str, Any]], *, workspace_id: str) -> bool:
    """Append ``entries`` to the run's ``_code_change.clef_findings`` (the newest
    ``_FINDINGS_KEPT``). Never raises."""
    if not entries:
        return False
    from pocketpaw.stores import get_instinct_store
    from pocketpaw_ee.cloud._core.proposals import update_action_blob

    try:
        store = get_instinct_store(workspace_id=workspace_id or None)
        action = await store.get_action(run_id)
        blob = (getattr(action, "parameters", None) or {}).get(_PARAM_KEYS["diff"])
        if not isinstance(blob, dict):
            return False
        kept = [*(blob.get("clef_findings") or []), *entries][-_FINDINGS_KEPT:]
        return await update_action_blob(
            store=store,
            action_id=run_id,
            param_key=_PARAM_KEYS["diff"],
            updates={"clef_findings": kept},
        )
    except Exception as exc:  # noqa: BLE001 — a lost record never fails the caller
        logger.warning("clef: could not record findings on %s (%s)", run_id, type(exc).__name__)
        return False


def _outcome(action: Any, blob: dict[str, Any], recorded: str | None) -> str:
    """What actually happened: the recorded outcome, else the Action's state
    (a diff that landed reads ``landed``)."""
    if recorded:
        return recorded
    status = str(getattr(action, "status", "") or "")
    if status == "executed" and (blob.get("pr_url") or blob.get("commit_sha")):
        return "landed"
    return status


async def list_verdicts(workspace_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Every recorded verdict in the workspace beside its actual outcome, newest
    first. A finding row carries ``source``, ``actions`` and ``issue``; one tied
    to an issue has outcome "" until ``resolve_outcomes`` reads the issue."""
    from pocketpaw.stores import get_instinct_store

    store = get_instinct_store(workspace_id=workspace_id or None)
    rows: list[dict[str, Any]] = []
    for action in await store.list_actions(pocket_id=workspace_id, limit=limit):
        params = getattr(action, "parameters", None) or {}
        for kind, key in _PARAM_KEYS.items():
            blob = params.get(key)
            if not isinstance(blob, dict):
                continue
            base = {"action_id": str(action.id), "title": getattr(action, "title", "")}
            rec = blob.get("clef")
            if isinstance(rec, dict):
                rows.append(
                    {
                        **base,
                        "kind": kind,
                        "scored_at": rec.get("scored_at"),
                        "verdict": rec.get("verdict") or {},
                        "outcome": _outcome(action, blob, rec.get("actual_outcome")),
                    }
                )
            findings = blob.get("clef_findings") if kind == "diff" else None
            for rec in findings if isinstance(findings, list) else []:
                if not isinstance(rec, dict):
                    continue
                issue = rec.get("issue")
                ran = "" if issue else _outcome(action, blob, None)
                rows.append(
                    {
                        **base,
                        "kind": "finding",
                        "source": rec.get("source"),
                        "scored_at": rec.get("scored_at"),
                        "verdict": rec.get("verdict") or {},
                        "actions": rec.get("actions") or [],
                        "issue": issue,
                        "outcome": rec.get("actual_outcome") or ran,
                    }
                )
    return rows


def issue_outcome(state: str, reason: str) -> str:
    """What the human did with an issue: ``open``, ``duplicate``, ``fixed``,
    ``wont_fix`` (GitHub's ``state`` + ``stateReason``), else ``closed``."""
    if state.upper() == "OPEN":
        return "open"
    return {"DUPLICATE": "duplicate", "COMPLETED": "fixed", "NOT_PLANNED": "wont_fix"}.get(
        reason.upper(), "closed"
    )


async def resolve_outcomes(rows: list[dict[str, Any]], gh: GhFn) -> list[dict[str, Any]]:
    """Fill the outcome of each finding row tied to an issue from GitHub: one
    ``gh issue list --state all`` per repo. An unreadable repo leaves them ""."""
    slugs = {str(r["issue"]).split("#")[0] for r in rows if r.get("issue") and not r["outcome"]}
    states: dict[str, str] = {}
    for slug in sorted(slugs):
        argv = ["gh", "issue", "list", "-R", slug, "--state", "all", "--limit", "1000"]
        try:
            code, out, _ = await gh([*argv, "--json", "number,state,stateReason"])
            for i in json.loads(out) if code == 0 else []:
                states[f"{slug}#{i['number']}"] = issue_outcome(
                    str(i.get("state") or ""), str(i.get("stateReason") or "")
                )
        except Exception:  # noqa: BLE001 — the eval prints what it could read
            logger.warning("clef: issue states for %s unreadable", slug, exc_info=True)
    for r in rows:
        if r.get("issue") and not r["outcome"]:
            r["outcome"] = states.get(str(r["issue"]), "")
    return rows


def agreement(rows: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """``{decision: (agreed, judged)}`` over finding rows whose issue a human
    closed. ``blocking`` agrees when blocking matches "fixed" (not won't-fix);
    ``duplicate`` when over ``DUPLICATE_MIN`` matches "closed as duplicate".
    ``mechanical`` has no human signal on the issue yet, so it is not counted."""
    out = {"blocking": (0, 0), "duplicate": (0, 0)}
    for r in rows:
        v, outcome = r.get("verdict") or {}, r.get("outcome")
        if r.get("kind") != "finding":
            continue
        if outcome in ("fixed", "wont_fix") and v.get("blocking", -1) >= 0:
            said = v["blocking"] >= BLOCKING_MIN
            agreed, judged = out["blocking"]
            out["blocking"] = (agreed + (said == (outcome == "fixed")), judged + 1)
        if outcome in ("fixed", "wont_fix", "duplicate") and v.get("duplicate", -1) >= 0:
            said = v["duplicate"] > DUPLICATE_MIN
            agreed, judged = out["duplicate"]
            out["duplicate"] = (agreed + (said == (outcome == "duplicate")), judged + 1)
    return out


# ---------------------------------------------------------------------------
# hooks — fire-and-forget, shadow only
# ---------------------------------------------------------------------------


def _spawn(label: str, coro_fn: Any) -> asyncio.Task[Any] | None:
    try:
        client = from_env()  # synchronous check: no task at all when off
    except Exception:  # noqa: BLE001 — a hook never fails its caller
        return None
    if client is None:
        return None

    async def run() -> Any:
        try:
            return await coro_fn(client)
        except Exception as exc:  # noqa: BLE001 — shadow mode never fails a shift
            logger.warning("clef: %s shadow score failed (%s)", label, type(exc).__name__)
            return None

    try:
        task = asyncio.get_running_loop().create_task(run())
    except Exception:  # noqa: BLE001 — no loop, nothing to score on
        return None
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def score_and_record_plan(
    workspace_id: str,
    plan_action_id: str,
    plan_tasks: list[dict[str, Any]],
    charter: dict,
    *,
    client: ClefClient | None = None,
    evidence: dict[str, str] | None = None,
) -> PlanVerdict | None:
    """Score a proposed plan, record the verdict on its ``_belt_plan`` blob and
    return it (``None`` when Clef is off or failed). The shadow hook spawns this;
    the auto plan gate awaits it."""
    client = client or from_env()
    if client is None:
        return None
    scope = f"Goal: {charter.get('goal', '')}"
    rubric = "\n".join(
        [f"Boundary: {b}" for b in charter.get("boundaries") or []]
        + [f"Says no to: {s}" for s in charter.get("says_no") or []]
    )
    verdict = await score_plan(plan_tasks, scope, rubric, client=client, evidence=evidence)
    if verdict is None:
        return None
    await record_verdict(plan_action_id, "plan", verdict, workspace_id=workspace_id)
    logger.info("clef: plan %s scored %s", plan_action_id, asdict(verdict))
    return verdict


def shadow_plan(
    workspace_id: str,
    plan_action_id: str,
    plan_tasks: list[dict[str, Any]],
    charter: dict,
    evidence: dict[str, str] | None = None,
) -> asyncio.Task[Any] | None:
    """Score a proposed plan in the background and record the verdict. The
    task's result is the ``PlanVerdict`` (or ``None``), so the auto plan gate
    can await it; a timed-out wait leaves it recording in the background."""

    async def go(client: ClefClient) -> PlanVerdict | None:
        return await score_and_record_plan(
            workspace_id, plan_action_id, plan_tasks, charter, client=client, evidence=evidence
        )

    return _spawn("plan", go)


def shadow_diff(
    workspace_id: str, action_id: str, diff_text: str, repo: str = ""
) -> asyncio.Task[Any] | None:
    """Score a run's diff (the repo's C4 components as the known helpers) and
    record the verdict on its ``_code_change`` blob."""

    async def go(client: ClefClient) -> None:
        from pocketpaw_ee.cloud.belt.orient import c4_lines

        helpers = "\n".join(c4_lines(repo)) if repo else ""
        verdict = await score_diff(diff_text, DEFAULT_DIFF_RUBRIC, helpers, client=client)
        if verdict is None:
            return
        await record_verdict(action_id, "diff", verdict, workspace_id=workspace_id)
        logger.info("clef: run %s scored %s", action_id, asdict(verdict))

    return _spawn("diff", go)


# ---------------------------------------------------------------------------
# finding hooks — awaited inline; shadow records, live (FINDINGS_FLAG) acts
# ---------------------------------------------------------------------------

_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


async def judge(
    findings: list[Finding],
    *,
    context: str,
    repo: str,
    gh: GhFn,
    client: ClefClient | None = None,
) -> list[FindingVerdict | None] | None:
    """Score up to ``MAX_FINDINGS`` findings at once, each against its top open
    issues (``patrols.open_issues``, ranked by ``candidate_issues``; never the
    issue ``context`` names, which the run itself closes), within
    ``FINDINGS_WAIT_S``. One verdict (or None) per finding; None when Clef is
    off, there is nothing to score, or the wait ran out. Never raises."""
    client = client or from_env()
    if client is None or not findings:
        return None
    try:
        from pocketpaw_ee.cloud.mandates.patrols import open_issues

        own = _SOURCE_ISSUE.search(context)
        rows = await open_issues(repo, gh) if repo else []
        rows = [r for r in rows if not own or r.get("number") != int(own.group(1))]

        async def one(finding: Finding) -> FindingVerdict | None:
            try:
                found = candidate_issues(rows, finding.text, exclude=finding.issue)
                return await score_finding(client, finding, context, found)
            except Exception as exc:  # noqa: BLE001 — one finding's failure is its own
                logger.warning("clef: finding score failed (%s)", type(exc).__name__)
                return None

        scored = await asyncio.wait_for(
            asyncio.gather(*(one(f) for f in findings[:MAX_FINDINGS])), FINDINGS_WAIT_S
        )
    except Exception as exc:  # noqa: BLE001 — Clef is advisory; never raise
        logger.warning("clef: findings not scored (%s)", type(exc).__name__)
        return None
    return [*scored, *([None] * (len(findings) - len(scored)))]


async def judge_one(finding: Finding, blob: dict[str, Any], gh: GhFn) -> FindingVerdict | None:
    """``judge`` for one finding of the run ``blob``; None when Clef is off."""
    verdicts = await judge([finding], context=_context(blob), repo=_repo(blob), gh=gh)
    return verdicts[0] if verdicts else None


def review_findings(note: str, run_id: str) -> list[Finding]:
    """A ``Belt-Review: fail`` comment's items: each non-empty line after the
    verdict and ``Head:`` lines, bullet stripped, with the files it names."""
    out = []
    for line in note.strip().splitlines()[2:]:
        text = _BULLET.sub("", line).strip()
        if text:
            files = tuple(dict.fromkeys(_FILE_LINE.findall(text)))
            out.append(Finding(text, "review", run_id, files=files))
    return out


async def judge_review(
    run_id: str,
    blob: dict[str, Any],
    note: str,
    *,
    slug: str,
    pr_url: str,
    gh: GhFn,
    line: dict[str, Any],
) -> tuple[str, str]:
    """The reviewer said ``fail``; ``note`` is its comment, ``line`` the
    mandate's charter read. Returns the status to post and the fixer's note:
    ``("fail", "")`` is today's path (shadow, Clef off, no items parsed). Live,
    each finding that does not hold becomes a follow-up issue (labels
    ``review``, ``gap``, the line's, ``audit-rule`` when mechanical) or a
    comment on the open issue it duplicates; ``("followup", "")`` when none
    holds (the round is clean), else ``("fail", <the note with only the holding
    items>)``. A finding Belt could not file holds. Never raises."""
    try:
        findings = review_findings(note, run_id)
        verdicts = await judge(findings, context=_context(blob), repo=_repo(blob), gh=gh)
        if verdicts is None:
            return "fail", ""
        live = findings_live()
        labels = ["review", "gap", *((line.get("charter") or {}).get("issue_labels") or [])]
        made: set[str] = set()
        held: list[str] = []  # the holding findings, then any Belt could not file
        filed: list[str] = []
        entries = []
        for finding, verdict in zip(findings, verdicts, strict=True):
            act, issue = decide(verdict), ""
            actions: list[str] = []
            if live and not act.holds:
                actions, issue = await _follow_up(gh, slug, finding, act, labels, made, pr_url)
            if live and not issue:
                actions = ["held"]
                held.append(finding.text)
            filed += [issue] if issue else []
            if verdict is not None:
                entries.append(finding_entry(finding, verdict, actions=actions, issue=issue))
        await record_findings(run_id, entries, workspace_id=str(blob.get("workspace_id") or ""))
        if not live:
            return "fail", ""
        if round_clean(verdicts) and not held:  # every finding is a follow-up now
            return "followup", ""
        kept = [*note.strip().splitlines()[:2], *(f"- {text}" for text in held)]
        if filed:
            kept.append(f"Filed as follow-ups, not for this PR: {', '.join(filed)}")
        return "fail", "\n".join(kept)
    except Exception as exc:  # noqa: BLE001 — the review's own fail stands
        logger.warning("clef: review findings of %s not judged (%s)", run_id, type(exc).__name__)
        return "fail", ""


async def judge_tester_bugs(
    run_id: str, blob: dict[str, Any], bugs: list[dict[str, Any]], *, slug: str, gh: GhFn
) -> None:
    """The bug issues the run's tester filed (``number``, ``title``, ``body``).
    Each is recorded with its issue; live, a mechanical one gets ``audit-rule``
    and one duplicating an open issue gets a comment there and the ``duplicate``
    label. ``blocking`` is recorded only: the run has landed and a tester never
    holds it. Never raises."""
    try:
        findings = [
            Finding(
                text=f"{b.get('title') or ''}\n{b.get('body') or ''}".strip(),
                source="tester",
                run_id=run_id,
                files=tuple(dict.fromkeys(_FILE_LINE.findall(str(b.get("body") or "")))),
                screenshots=tuple(dict.fromkeys(_PNG_NAME.findall(str(b.get("body") or "")))),
                issue=b["number"],
            )
            for b in bugs
            if isinstance(b.get("number"), int)
        ]
        verdicts = await judge(findings, context=_context(blob), repo=_repo(blob), gh=gh)
        live, entries = findings_live(), []
        for finding, verdict in zip(findings, verdicts or [], strict=False):
            if verdict is None:
                continue
            act, actions = decide(verdict), list[str]()
            if live and act.audit_rule and await _add_label(gh, slug, finding.issue, AUDIT_RULE):
                actions.append("audit_rule")
            if live and act.duplicate_of:
                said = f"Belt's tester filed this again as #{finding.issue}."
                argv = ["gh", "issue", "comment", str(act.duplicate_of), "-R", slug, "--body", said]
                if (await gh(argv))[0] == 0 and await _add_label(
                    gh, slug, finding.issue, "duplicate"
                ):
                    actions.append("duplicate")
            issue = f"{slug}#{finding.issue}"
            entries.append(finding_entry(finding, verdict, actions=actions, issue=issue))
        await record_findings(run_id, entries, workspace_id=str(blob.get("workspace_id") or ""))
    except Exception as exc:  # noqa: BLE001 — the tester's bugs stand as filed
        logger.warning("clef: tester bugs of %s not judged (%s)", run_id, type(exc).__name__)


def _context(blob: dict[str, Any]) -> str:
    return str(blob.get("task") or blob.get("title") or "")


def _repo(blob: dict[str, Any]) -> str:
    return str(blob.get("repo") or "")


async def _add_label(gh: GhFn, slug: str, number: int, label: str) -> bool:
    """Label issue ``number``, creating the label first (that fails harmlessly
    when it exists)."""
    await gh(["gh", "label", "create", label, "-R", slug])
    argv = ["gh", "issue", "edit", str(number), "-R", slug, "--add-label", label]
    return (await gh(argv))[0] == 0


async def _follow_up(
    gh: GhFn,
    slug: str,
    finding: Finding,
    act: FindingDecision,
    labels: list[str],
    made: set[str],
    pr_url: str,
) -> tuple[list[str], str]:
    """Comment on the issue a non-blocking review finding duplicates, else file
    it as a follow-up issue. ``(actions, "owner/repo#n")``, or ``([], "")`` when
    GitHub refused."""
    if act.duplicate_of:
        said = f"Belt's review of {pr_url} found this again:\n\n{finding.text}"
        argv = ["gh", "issue", "comment", str(act.duplicate_of), "-R", slug, "--body", said]
        ok = (await gh(argv))[0] == 0
        return (["duplicate"], f"{slug}#{act.duplicate_of}") if ok else ([], "")
    wanted = [*labels, *([AUDIT_RULE] if act.audit_rule else [])]
    for label in wanted:
        if label not in made:  # an existing label exits non-zero; that is fine
            made.add(label)
            await gh(["gh", "label", "create", label, "-R", slug])
    body = (
        f"Belt's review of {pr_url} found this and judged it non-blocking, so it did "
        f"not hold the PR.\n\n{finding.text}"
    )
    title = f"Follow-up: {finding.text.splitlines()[0][:90]}"
    argv = ["gh", "issue", "create", "-R", slug, "--title", title, "--body", body]
    for label in wanted:
        argv += ["--label", label]
    code, out, _ = await gh(argv)
    found = _ISSUE_URL.search(out or "")
    if code != 0 or not found:
        logger.warning("clef: follow-up issue on %s refused (exit %s)", slug, code)
        return [], ""
    return ["follow_up", *(["audit_rule"] if act.audit_rule else [])], f"{slug}#{found.group(1)}"
