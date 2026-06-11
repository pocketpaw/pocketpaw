# ee/pocketpaw_ee/cloud/mandates/autopilot.py
# Created: 2026-06-11 (feat/belt-autopilot).
#
# AUTOPILOT — Foresight-seeded simulated users that exercise a mandate's surface
# and feed the FEEDBACK PATROL. When autopilot is ON, a per-mandate background
# cycle runs every ``POCKETPAW_MANDATE_AUTOPILOT_INTERVAL`` seconds (default 300;
# one cycle also fires IMMEDIATELY on start). Each cycle:
#
#   1. Reads the bound repo's surface context — the README's first lines + the
#      recent commit titles (``git log``) — so the personas "use" something real.
#   2. Builds N personas (1-10, default 3) via the FORESIGHT module's persona
#      seeding (``ee.foresight.persona.OceanDrift`` — a genuine bridge to the sim
#      module; the seeded drift shapes each persona's temperament).
#   3. Each persona emits 1-3 STRUCTURED feedback items {text, severity 1-5,
#      source: "autopilot:<persona>"} through a pluggable ``UserSim`` interface,
#      POSTed through the EXISTING feedback service path
#      (``service.file_feedback`` — NOT raw HTTP) so they become Sightings the
#      next shift's foreman cites.
#
# WHICH PATH + WHY (the honesty note the brief asks for): the brief allows a
# lighter persona LLM call when foresight's full scenario runner is too heavy
# for a per-cycle call. We took the LIGHTER path:
#
#   * Foresight's ``run_scenario`` / OASIS substrate is a TICK-BASED world
#     simulation (CAMEL + OASIS + a YAML scenario config, anchors, prediction
#     records) geared to "rehearse a decision across a population of personas",
#     NOT "use a product and emit free-text feedback". Spinning it up per cycle
#     would pull in torch/igraph/pandas and a multi-tick world loop for what is a
#     one-shot "react to this surface" call — far too heavyweight, and its
#     action vocabulary (``action/rationale/put``) is the wrong shape.
#   * Instead we reuse the FOREMAN's proven pluggable transport pattern
#     (``POCKETPAW_MANDATE_LLM=claude|mock`` — the SAME env the foreman reads) and
#     the foresight ``OceanDrift`` persona-seed value object. The persona LLM
#     call lives behind the ``UserSim`` interface so a later PR can swap the full
#     foresight scenario runner in with no caller change. Mock mode is
#     deterministic + seeded so tests get stable sightings.
#
# RESILIENCE: autopilot must NEVER crash a shift or the app. Every persona call,
# every feedback POST, and every cycle is wrapped — a failure is logged and
# swallowed per-cycle; the loop sleeps and tries again next interval.
#
# BACKGROUND TASK: each mandate's loop is an asyncio task in the process-local
# ``_TASKS`` registry keyed by mandate id (mirrors
# ``decisions._action_sweeper``'s create-task + cancel-and-await shape, but
# per-mandate so STOP can cancel exactly one). The persisted
# ``MandateDoc.autopilot.on`` flag is the source of truth for whether autopilot
# SHOULD be running; the task is process-local and is re-derivable from that flag.

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Default cycle interval (seconds) — env ``POCKETPAW_MANDATE_AUTOPILOT_INTERVAL``.
_DEFAULT_INTERVAL_SECONDS = 300
# Persona-count bounds (mirrors the DTO's clamp).
_MIN_USERS = 1
_MAX_USERS = 10
# Per-persona feedback-item bounds.
_MIN_ITEMS = 1
_MAX_ITEMS = 3
# Subprocess timeout for the per-persona claude CLI call (seconds).
_CLI_TIMEOUT = 120.0
# How much surface context to feed a persona (chars of README / count of commits).
_README_CHARS = 800
_COMMIT_COUNT = 10

# Process-local registry of running autopilot loops, keyed by mandate id.
_TASKS: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Persona seeding — bridges the FORESIGHT module's OceanDrift value object.
# ---------------------------------------------------------------------------


def _ocean_drift_cls() -> Any:
    """Resolve foresight's ``OceanDrift`` value object, or ``None`` if the
    foresight module can't be imported (a partial install). The bridge is
    genuine but optional — without it we still build personas with a name +
    a neutral temperament string."""
    try:
        from pocketpaw_ee.foresight.persona import OceanDrift

        return OceanDrift
    except Exception:  # noqa: BLE001 — foresight is optional here
        logger.debug("autopilot: foresight OceanDrift unavailable", exc_info=True)
        return None


# A small deterministic persona palette — name + drift seed. In mock mode the
# personas are picked from the head of this list so tests are stable; the
# claude transport uses the same names + drift to shape the prompt.
_PERSONA_PALETTE: list[dict[str, Any]] = [
    {"name": "power-user", "drift": {"conscientiousness": 1.5, "openness": 0.5}},
    {"name": "skeptic", "drift": {"agreeableness": -1.0, "neuroticism": 0.8}},
    {"name": "newcomer", "drift": {"openness": 1.0, "conscientiousness": -0.5}},
    {"name": "ops-lead", "drift": {"conscientiousness": 2.0, "neuroticism": 0.5}},
    {"name": "casual", "drift": {"extraversion": 1.0, "conscientiousness": -1.0}},
    {"name": "security-minded", "drift": {"neuroticism": 1.2, "conscientiousness": 1.0}},
    {"name": "designer", "drift": {"openness": 1.8, "agreeableness": 0.5}},
    {"name": "integrator", "drift": {"conscientiousness": 0.8, "openness": 0.8}},
    {"name": "manager", "drift": {"extraversion": 1.2, "agreeableness": 0.8}},
    {"name": "tester", "drift": {"conscientiousness": 1.6, "neuroticism": 1.0}},
]


class Persona:
    """One autopilot simulated user — a name + a temperament block.

    The temperament block is rendered from foresight's ``OceanDrift`` when the
    foresight module is available (the genuine bridge), else a neutral string."""

    def __init__(self, name: str, drift_kwargs: dict[str, float]) -> None:
        self.name = name
        drift_cls = _ocean_drift_cls()
        if drift_cls is not None:
            try:
                self._drift = drift_cls(**drift_kwargs)
                self.temperament = self._drift.as_prompt_block()
            except Exception:  # noqa: BLE001 — drift construction must never break a persona
                self._drift = None
                self.temperament = "baseline temperament"
        else:
            self._drift = None
            self.temperament = "baseline temperament"


def build_personas(users: int) -> list[Persona]:
    """Build ``users`` personas (clamped 1-10) from the deterministic palette.

    Deterministic + seeded: the first ``users`` entries of the palette are taken
    in order, so the same ``users`` value always yields the same personas — the
    test seeding the brief requires. Personas beyond the palette length wrap with
    an index suffix so a request for the max (10) is always satisfiable."""
    n = max(_MIN_USERS, min(_MAX_USERS, int(users)))
    personas: list[Persona] = []
    for i in range(n):
        spec = _PERSONA_PALETTE[i % len(_PERSONA_PALETTE)]
        name = spec["name"] if i < len(_PERSONA_PALETTE) else f"{spec['name']}-{i}"
        personas.append(Persona(name=name, drift_kwargs=dict(spec["drift"])))
    return personas


# ---------------------------------------------------------------------------
# Surface context — what the personas "use" (README + recent commit titles).
# ---------------------------------------------------------------------------


async def _read_surface(repo_id: str) -> dict[str, Any]:
    """Read the bound repo's surface context for the personas.

    Returns ``{"name", "readme", "commits"}`` — the repo dir name, the first
    ~800 chars of the README (any-case ``README*``), and up to 10 recent commit
    titles (``git log --format=%s``). A missing repo / README / git history
    degrades to empty values — autopilot never raises on a thin surface."""
    repo = Path(repo_id).expanduser()
    out: dict[str, Any] = {"name": repo.name, "readme": "", "commits": []}
    if not repo.is_dir():
        return out

    # README — first matching README* file, first ~800 chars.
    try:
        for child in sorted(repo.iterdir()):
            if child.is_file() and child.name.lower().startswith("readme"):
                out["readme"] = child.read_text(encoding="utf-8", errors="replace")[:_README_CHARS]
                break
    except OSError:
        logger.debug("autopilot: README read failed for %s", repo_id, exc_info=True)

    # Recent commit titles — git log, argv-only subprocess (no shell).
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            f"-{_COMMIT_COUNT}",
            "--format=%s",
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            stdout = b""
        titles = [
            line.strip() for line in stdout.decode("utf-8", "replace").splitlines() if line.strip()
        ]
        out["commits"] = titles[:_COMMIT_COUNT]
    except Exception:  # noqa: BLE001 — git history is optional context
        logger.debug("autopilot: git log failed for %s", repo_id, exc_info=True)
    return out


# ---------------------------------------------------------------------------
# UserSim — the pluggable persona transport (mirrors the foreman's PlanLlm).
# ---------------------------------------------------------------------------


class UserSim(Protocol):
    """One persona's reaction: surface context + persona in, feedback items out.

    Returns a list of 1-3 ``{text, severity}`` dicts (the ``source`` is stamped
    by the caller as ``autopilot:<persona>``). Selected by
    ``POCKETPAW_MANDATE_LLM`` — the SAME env the foreman's transport reads."""

    async def react(self, *, persona: Persona, surface: dict[str, Any]) -> list[dict[str, Any]]: ...


class MockUserSim:
    """Deterministic, SEEDED persona transport for tests + offline demos.

    Each persona emits 1-3 feedback items derived from the surface context +
    the persona name, with a per-persona RNG SEEDED on the persona name so the
    same persona + same surface always yields the same items (the brief's
    deterministic-in-mock-mode requirement)."""

    async def react(self, *, persona: Persona, surface: dict[str, Any]) -> list[dict[str, Any]]:
        rng = random.Random(persona.name)  # seeded on the persona — stable
        name = surface.get("name") or "the product"
        commits = list(surface.get("commits") or [])
        readme = (surface.get("readme") or "").strip()

        # A small templated bank — the persona picks a deterministic subset.
        templates = [
            f"As a {persona.name}, the onboarding for {name} felt unclear.",
            f"As a {persona.name}, I hit friction using {name} today.",
            f"As a {persona.name}, I'd want clearer docs for {name}.",
        ]
        if commits:
            templates.append(
                f"As a {persona.name}, the recent change '{commits[0][:60]}' needs a follow-up."
            )
        if readme:
            templates.append(
                f"As a {persona.name}, the README pitch for {name} oversells vs. reality."
            )

        count = rng.randint(_MIN_ITEMS, min(_MAX_ITEMS, len(templates)))
        chosen = rng.sample(templates, count)
        return [{"text": text, "severity": rng.randint(2, 5)} for text in chosen]


class ClaudeCliUserSim:
    """Real persona transport — shells the ``claude`` CLI (demo bar).

    Mirrors the foreman's ``ClaudeCliLlm``: ``claude -p <prompt> --output-format
    json``; the prompt is a single argv element (the CLI does its own auth). The
    model is asked for STRICT JSON: a list of ``{text, severity}`` feedback items.
    A transport / parse failure returns an empty list — the caller logs + skips
    that persona; autopilot never raises out of a cycle."""

    async def react(self, *, persona: Persona, surface: dict[str, Any]) -> list[dict[str, Any]]:
        prompt = _build_persona_prompt(persona, surface)
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=_CLI_TIMEOUT)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning("autopilot: persona %s CLI timed out", persona.name)
                return []
            if proc.returncode != 0:
                logger.warning(
                    "autopilot: persona %s CLI failed (exit %s): %s",
                    persona.name,
                    proc.returncode,
                    err_b.decode("utf-8", "replace").strip()[:200],
                )
                return []
            text = _unwrap_cli_result(out_b.decode("utf-8", "replace"))
            return _parse_items(text)
        except Exception:  # noqa: BLE001 — a transport failure skips this persona
            logger.warning("autopilot: persona %s react crashed", persona.name, exc_info=True)
            return []


def _build_persona_prompt(persona: Persona, surface: dict[str, Any]) -> str:
    """Assemble the persona prompt for the claude transport."""
    commit_titles = (surface.get("commits") or [])[:_COMMIT_COUNT]
    commits = "\n".join(f"- {c}" for c in commit_titles) or "(none)"
    readme = (surface.get("readme") or "(no README)").strip()[:_README_CHARS]
    return f"""You are a simulated user of the product "{surface.get("name")}".
Persona: {persona.name}. Temperament: {persona.temperament}.

== PRODUCT README (excerpt) ==
{readme}

== RECENT CHANGES (commit titles) ==
{commits}

You just used this product as the persona above. Report 1-3 pieces of concrete,
specific feedback — friction, confusion, a bug you'd hit, a missing capability —
in the voice of the persona. Each item has a severity 1 (minor) to 5 (blocking).

Reply with STRICT JSON only — no prose, no markdown fences:
[{{"text": "<one concrete piece of feedback>", "severity": 3}}]"""


def _unwrap_cli_result(out: str) -> str:
    """Pull the ``result`` field off the claude CLI JSON envelope, tolerating a
    bare-text response (mirrors the foreman's ClaudeCliLlm)."""
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError:
        return out
    if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
        return envelope["result"]
    return out


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _parse_items(raw: str) -> list[dict[str, Any]]:
    """Parse the model's text into a list of ``{text, severity}`` items.

    Tolerates a fenced JSON block or stray text around a top-level JSON array.
    Each item is normalized: ``text`` is required (non-empty), ``severity`` is
    clamped to 1-5 (default 3). Returns at most 3 items. An unparseable response
    yields an empty list — the caller skips that persona."""
    text = raw.strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    data: Any = None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in data[:_MAX_ITEMS]:
        if not isinstance(entry, dict):
            continue
        body = str(entry.get("text") or "").strip()
        if not body:
            continue
        try:
            sev = int(entry.get("severity") or 3)
        except (TypeError, ValueError):
            sev = 3
        sev = max(1, min(5, sev))
        items.append({"text": body, "severity": sev})
    return items


def resolve_user_sim() -> UserSim:
    """Pick the persona transport from ``POCKETPAW_MANDATE_LLM`` (the SAME env the
    foreman reads). ``mock`` → deterministic seeded sim; anything else → claude."""
    choice = (os.environ.get("POCKETPAW_MANDATE_LLM") or "claude").strip().lower()
    if choice == "mock":
        return MockUserSim()
    return ClaudeCliUserSim()


# ---------------------------------------------------------------------------
# One cycle — the unit the loop runs (and the unit run immediately on start).
# ---------------------------------------------------------------------------


async def run_autopilot_cycle(
    workspace_id: str,
    mandate_id: str,
    *,
    users: int,
    user_sim: UserSim | None = None,
) -> int:
    """Run ONE autopilot cycle: build personas, read the surface, file feedback.

    Returns the number of feedback sightings filed. NEVER raises — every persona
    and every feedback POST is wrapped; a failure is logged and swallowed so a
    bad cycle can't crash the loop, a shift, or the app. The feedback goes
    through the EXISTING ``service.file_feedback`` path (not raw HTTP), so each
    item becomes a Sighting the next shift's foreman cites, with
    ``source="autopilot:<persona>"``."""
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    sim: UserSim = user_sim or resolve_user_sim()

    # Resolve the bound repo for the surface context. A read failure degrades to
    # an empty surface (the personas still emit generic feedback).
    try:
        repo_id = await mandate_service.repo_for_mandate(workspace_id, mandate_id)
    except Exception:  # noqa: BLE001 — a repo read must never break the cycle
        logger.debug("autopilot: repo lookup failed for %s", mandate_id, exc_info=True)
        repo_id = None
    surface = await _read_surface(repo_id) if repo_id else {"name": "", "readme": "", "commits": []}

    personas = build_personas(users)
    filed = 0
    for persona in personas:
        try:
            items = await sim.react(persona=persona, surface=surface)
        except Exception:  # noqa: BLE001 — one bad persona never sinks the cycle
            logger.warning(
                "autopilot: persona %s reaction failed for mandate %s",
                persona.name,
                mandate_id,
                exc_info=True,
            )
            continue
        for item in items[:_MAX_ITEMS]:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            severity = item.get("severity")
            try:
                # File through the EXISTING feedback service path — the general
                # {text, severity, source} shape becomes a feedback Sighting.
                await mandate_service.file_feedback(
                    workspace_id,
                    f"autopilot:{persona.name}",
                    mandate_id,
                    {
                        "text": text,
                        "severity": severity,
                        "source": f"autopilot:{persona.name}",
                    },
                )
                filed += 1
            except Exception:  # noqa: BLE001 — a feedback failure skips this item
                logger.warning(
                    "autopilot: feedback file failed for mandate %s (persona %s)",
                    mandate_id,
                    persona.name,
                    exc_info=True,
                )
    logger.info(
        "autopilot: cycle for mandate %s filed %d feedback sighting(s) from %d persona(s)",
        mandate_id,
        filed,
        len(personas),
    )
    return filed


# ---------------------------------------------------------------------------
# The background loop + the per-mandate task registry (start / stop).
# ---------------------------------------------------------------------------


def _interval_seconds() -> int:
    """Read the cycle interval from env, default 300s. A non-int / non-positive
    value falls back to the default."""
    raw = os.environ.get("POCKETPAW_MANDATE_AUTOPILOT_INTERVAL", "").strip()
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "POCKETPAW_MANDATE_AUTOPILOT_INTERVAL=%r is not an int — using %d",
            raw,
            _DEFAULT_INTERVAL_SECONDS,
        )
        return _DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else _DEFAULT_INTERVAL_SECONDS


async def _autopilot_loop(
    workspace_id: str, mandate_id: str, users: int, *, run_immediate: bool = True
) -> None:
    """The per-mandate background loop body. Runs ONE cycle immediately (unless
    ``run_immediate`` is False — the service already ran it synchronously), then
    a cycle every interval. Per-cycle failures are caught inside
    ``run_autopilot_cycle`` already, but the loop also guards the sleep +
    catch-all so nothing escapes. ``CancelledError`` propagates so STOP can
    cancel-and-await cleanly."""
    interval = _interval_seconds()
    logger.info(
        "autopilot: loop started for mandate %s (users=%d, interval=%ds)",
        mandate_id,
        users,
        interval,
    )
    # One cycle IMMEDIATELY on start (the brief's "also run ONE cycle immediately")
    # — skipped only when the caller already ran it synchronously (the endpoint
    # path, so START's response already reflects the first cycle's sightings).
    if run_immediate:
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await run_autopilot_cycle(workspace_id, mandate_id, users=users)
            except Exception:  # noqa: BLE001 — already swallowed inside; belt-and-braces
                logger.warning(
                    "autopilot: immediate cycle failed for %s", mandate_id, exc_info=True
                )

    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("autopilot: loop for mandate %s cancelled — exiting", mandate_id)
            raise
        try:
            await run_autopilot_cycle(workspace_id, mandate_id, users=users)
        except Exception:  # noqa: BLE001 — a bad cycle never sinks the loop
            logger.warning("autopilot: cycle failed for mandate %s", mandate_id, exc_info=True)


async def start_autopilot(
    workspace_id: str, mandate_id: str, users: int, *, run_immediate: bool = True
) -> None:
    """Start (or restart) the background autopilot loop for a mandate.

    Idempotent on restart: an existing live task for this mandate is cancelled
    first (a ``users`` change takes effect on restart), then a fresh task is
    created and registered. ``run_immediate=False`` tells the loop the caller
    already ran the first cycle synchronously (the endpoint path), so the loop's
    next action is the first interval sleep — no double-fire."""
    await stop_autopilot(mandate_id)
    n = max(_MIN_USERS, min(_MAX_USERS, int(users)))
    task = asyncio.create_task(
        _autopilot_loop(workspace_id, mandate_id, n, run_immediate=run_immediate),
        name=f"autopilot-{mandate_id}",
    )
    _TASKS[mandate_id] = task


async def stop_autopilot(mandate_id: str) -> None:
    """Cancel + await the mandate's background loop. Safe to call when no task is
    running (a no-op). Idempotent."""
    task = _TASKS.pop(mandate_id, None)
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def is_running(mandate_id: str) -> bool:
    """True when a live autopilot loop is registered for the mandate."""
    task = _TASKS.get(mandate_id)
    return task is not None and not task.done()


__all__ = [
    "ClaudeCliUserSim",
    "MockUserSim",
    "Persona",
    "UserSim",
    "build_personas",
    "is_running",
    "resolve_user_sim",
    "run_autopilot_cycle",
    "start_autopilot",
    "stop_autopilot",
]
