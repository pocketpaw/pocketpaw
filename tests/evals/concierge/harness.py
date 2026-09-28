# tests/evals/concierge/harness.py — runs one eval case through the REAL v2 runner.
#
# Updated: 2026-09-28 (feat/concierge-eval-gate, page-aware merge) — stub CR-5's daily spend
# read, which reached Mongo on every case and fell open with a traceback.
# Created: 2026-09-28 (feat/concierge-eval-gate, CR-6). ``run_case`` drives
# ``concierge_runtime.run_concierge_v2`` itself, not a copy of it, so the eval
# scores exactly what a visitor would get: the real page resolution, retrieval,
# prompt, frame, model settings and output filter. Only the boundaries are
# replaced, the same seams tests/cloud/test_paw_bar_concierge_v2.py uses:
#
#   * ``KnowledgeService`` search/context/article reads -> ``seed.FakeKnowledge``;
#   * the run store (``create_run`` / ``mark_*``) -> no-ops (no Mongo), and
#     ``find_run_usage_since`` (CR-5's daily spend read) -> no spend, so every
#     case runs under the cap instead of through its failed-read fallback;
#   * ``_settings`` -> the settings the run was given;
#   * ``_build_model`` -> a replay ``FunctionModel`` in recorded mode, and left
#     alone in real mode (the deployment's configured pydantic_ai model).
#
# Two wrappers observe without changing anything: ``build_prompt`` is wrapped to
# fingerprint the prompt (a recording made from a different prompt is "stale"),
# and ``FenceFilter`` is swapped for a subclass that keeps the raw model text it is
# fed, which is what a real run records and what a recorded run replays.

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from tests.evals.concierge import seed
from tests.evals.concierge.scorers import Turn

# Replayed text is streamed in pieces this long, so fences split across deltas
# exercise the filter's holding logic the way a real stream does.
REPLAY_CHUNK_CHARS = 17


@dataclass
class CaseRun:
    turn: Turn
    prompt_fingerprint: str


def prompt_fingerprint(frame: str, prompt: str) -> str:
    return hashlib.sha256(f"{frame}\n\x00\n{prompt}".encode()).hexdigest()[:16]


def _replay_model(raw_text: str) -> Any:
    from pydantic_ai.models.function import FunctionModel

    async def _stream(_messages: Any, _info: Any):
        for i in range(0, len(raw_text), REPLAY_CHUNK_CHARS):
            yield raw_text[i : i + REPLAY_CHUNK_CHARS]

    return FunctionModel(stream_function=_stream, model_name="recorded-concierge")


def _parse_sse(frames: list[bytes]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for frame in frames:
        event, data = "", "{}"
        for line in frame.decode().splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        out.append((event, json.loads(data)))
    return out


@contextmanager
def _seams(settings: Any, replay: str | None, seen: dict[str, Any]) -> Iterator[None]:
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService
    from pocketpaw_ee.paw_bar import concierge_runtime

    kb = seed.FakeKnowledge()
    real_build_prompt = concierge_runtime.build_prompt
    base_filter = concierge_runtime.FenceFilter

    class _RecordingFilter(base_filter):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            seen["knowledge"] = list(kwargs.get("knowledge") or ())
            seen["raw"] = ""

        def feed(self, chunk: str) -> list[str]:
            seen["raw"] += chunk or ""
            return super().feed(chunk)

    def _build_prompt(*args: Any, **kwargs: Any) -> str:
        prompt = real_build_prompt(*args, **kwargs)
        seen["prompt"] = prompt
        return prompt

    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    async def _no_usage(**_kw: Any) -> list[Any]:
        return []

    async def _create_run(spec: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(run_id=spec.run_id)

    runs = "pocketpaw_ee.cloud.chat.runs.service"
    with ExitStack() as stack:
        for name in (
            "search_articles_for_scope",
            "search_context_for_scope",
            "get_article_for_scope",
        ):
            stack.enter_context(patch.object(KnowledgeService, name, getattr(kb, name)))
        stack.enter_context(patch(f"{runs}.create_run", _create_run))
        for name in ("mark_running", "mark_completed", "mark_terminal"):
            stack.enter_context(patch(f"{runs}.{name}", _noop))
        stack.enter_context(patch(f"{runs}.find_run_usage_since", _no_usage))
        stack.enter_context(patch.object(concierge_runtime, "_settings", lambda: settings))
        stack.enter_context(patch.object(concierge_runtime, "build_prompt", _build_prompt))
        stack.enter_context(patch.object(concierge_runtime, "FenceFilter", _RecordingFilter))
        if replay is not None:
            stack.enter_context(
                patch.object(concierge_runtime, "_build_model", lambda _s: _replay_model(replay))
            )
        yield


async def run_case(case: dict[str, Any], settings: Any, *, replay: str | None = None) -> CaseRun:
    """Run ``case`` once. ``replay`` is the recorded raw model text (recorded mode);
    None calls the configured model (real mode)."""
    from pocketpaw_ee.paw_bar import concierge_runtime

    widget = seed.make_widget(case["site"])
    site = seed.make_site(case["site"], case.get("site_overrides"))
    seen: dict[str, Any] = {"raw": "", "prompt": "", "knowledge": []}
    frames: list[bytes] = []
    with _seams(settings, replay, seen):
        async for frame in concierge_runtime.run_concierge_v2(
            widget,
            site,
            None,
            case["message"],
            case.get("page"),
            workspace_id="ws-eval",
            pocket_id=site.pocket_id,
            customer_ref="eval-visitor",
            session_key=f"eval:{case['id']}",
            history=list(case.get("history") or []),
            stored_user_text=case["message"],
        ):
            frames.append(frame)

    events = _parse_sse(frames)
    final = "".join(d.get("content", "") for e, d in events if e == "chunk")
    sources = next((d.get("items") or [] for e, d in events if e == "sources"), [])
    error = next((d.get("code", "error") for e, d in events if e == "error"), "")
    frame = (
        concierge_runtime.FRAME_DOC_CODE
        if getattr(site, "concierge_allow_doc_code", False) is True
        else concierge_runtime.FRAME
    )
    page = case.get("page") or {}
    turn = Turn(
        final_text=final,
        raw_text=seen["raw"],
        sources=list(sources),
        knowledge=list(seen["knowledge"]),
        catalog=seed.catalog_dicts(widget),
        verbs=[a.verb for a in widget.spec.actions],
        gated_args=seed.gated_args(widget),
        page_text=seen["prompt"].split("<knowledge>", 1)[0] if page else "",
        error=error,
    )
    return CaseRun(turn=turn, prompt_fingerprint=prompt_fingerprint(frame, seen["prompt"]))


__all__ = ["REPLAY_CHUNK_CHARS", "CaseRun", "prompt_fingerprint", "run_case"]
