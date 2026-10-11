# ee/pocketpaw_ee/cloud/belt/clef.py — Belt's SHADOW consult of Cloudflare's Clef.
#
# Clef (``@cf/cloudflare/clef`` on Workers AI) is a decision model: it takes a
# ``state`` plus typed questions (``noul`` yes/no, ``choice``, ``score``) and
# returns a probability per answer. Belt asks it about the foreman's PLANS and
# the station's DIFFS and records the verdict on the Instinct Action blob it
# judged (``_belt_plan`` / ``_code_change``, key ``clef``). SHADOW ONLY: nothing
# reads a verdict to decide anything; the eval lists verdicts beside what the
# human and the executor actually did (``list_verdicts``,
# ``scripts/clef_verdicts.py``).
#
#   * ``ClefClient.ask`` — one POST, 20 s timeout, one retry on a 5xx; returns
#     ``None`` on any failure (timeout, 4xx, ``success:false``, bad JSON).
#   * ``score_plan`` / ``score_diff`` — pure question builders plus one ask. The
#     diff state is the file list plus the diff head (it says when it cut).
#   * ``record_verdict`` / ``list_verdicts`` — the blob write and the eval read.
#   * ``shadow_plan`` / ``shadow_diff`` — the hooks: a synchronous flag check,
#     then a fire-and-forget task that scores and records, logging one line.
#
# Off unless ``POCKETPAW_BELT_CLEF=shadow`` and both ``POCKETPAW_CLEF_TOKEN`` and
# ``POCKETPAW_CLEF_ACCOUNT_ID`` are set. INVARIANTS: nothing here raises into a
# shift or a run; the token and the account id (it sits in the URL) never reach
# a log line or a trace span (errors log the exception type and status only;
# requests run under ``logfire.suppress_instrumentation``). The blob carries
# only the verdict numbers, never the state, because ``list_runs`` reads every
# ``_code_change`` blob in bulk.

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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


def plan_questions() -> dict[str, Question]:
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
    }


async def score_plan(
    plan_tasks: list[dict[str, Any]],
    mandate_scope: str,
    rubric_text: str,
    *,
    client: ClefClient | None = None,
) -> PlanVerdict | None:
    client = client or from_env()
    if client is None:
        return None
    tasks_text, truncated = _cap(json.dumps(plan_tasks, default=str, indent=1))
    state = {"mandate_scope": mandate_scope, "rubric": rubric_text, "plan_tasks": tasks_text}
    answers = await client.ask(state, plan_questions())
    if answers is None:
        return None
    return PlanVerdict(
        in_scope=_p(answers, "in_scope"),
        low_risk=_p(answers, "low_risk"),
        tasks_independent=_p(answers, "tasks_independent"),
        too_big=_p(answers, "too_big"),
        truncated=truncated,
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
    # ponytail: read-modify-write ~1-2 s after the propose; an approve that edits
    # the blob inside that window is overwritten (dispatch already read it). A
    # store-side field merge closes it if Clef ever leaves shadow mode.
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
    """Every recorded verdict in the workspace beside its actual outcome, newest first."""
    from pocketpaw.stores import get_instinct_store

    store = get_instinct_store(workspace_id=workspace_id or None)
    rows: list[dict[str, Any]] = []
    for action in await store.list_actions(pocket_id=workspace_id, limit=limit):
        params = getattr(action, "parameters", None) or {}
        for kind, key in _PARAM_KEYS.items():
            blob = params.get(key)
            if not isinstance(blob, dict) or not isinstance(blob.get("clef"), dict):
                continue
            rec = blob["clef"]
            rows.append(
                {
                    "action_id": str(action.id),
                    "kind": kind,
                    "title": getattr(action, "title", ""),
                    "scored_at": rec.get("scored_at"),
                    "verdict": rec.get("verdict") or {},
                    "outcome": _outcome(action, blob, rec.get("actual_outcome")),
                }
            )
    return rows


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

    async def run() -> None:
        try:
            await coro_fn(client)
        except Exception as exc:  # noqa: BLE001 — shadow mode never fails a shift
            logger.warning("clef: %s shadow score failed (%s)", label, type(exc).__name__)

    try:
        task = asyncio.get_running_loop().create_task(run())
    except Exception:  # noqa: BLE001 — no loop, nothing to score on
        return None
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def shadow_plan(
    workspace_id: str, plan_action_id: str, plan_tasks: list[dict[str, Any]], charter: dict
) -> asyncio.Task[Any] | None:
    """Score a proposed plan and record the verdict on its ``_belt_plan`` blob."""

    async def go(client: ClefClient) -> None:
        scope = f"Goal: {charter.get('goal', '')}"
        rubric = "\n".join(
            [f"Boundary: {b}" for b in charter.get("boundaries") or []]
            + [f"Says no to: {s}" for s in charter.get("says_no") or []]
        )
        verdict = await score_plan(plan_tasks, scope, rubric, client=client)
        if verdict is None:
            return
        await record_verdict(plan_action_id, "plan", verdict, workspace_id=workspace_id)
        logger.info("clef: plan %s scored %s", plan_action_id, asdict(verdict))

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
