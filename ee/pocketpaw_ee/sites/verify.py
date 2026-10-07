# ee/pocketpaw_ee/sites/verify.py — the three-layer verification pipeline behind every
# agent create / edit and the ``verify_site`` tool. Contract §5 of
# docs/design/drafts/2026-09-24-sites-deps-verify-contract.md defines the verdict.
#
#   1. STATIC — ``paw-sites-gen check`` (``generator_client.run_static_check``) on the
#      API host, about a second, installs nothing. A static failure stops here.
#   2. BUILD — the preview lane (``build_job.enqueue_preview_build``) keyed on the SAME
#      armed content hash the editor view and the pre-warm use, so one sandbox serves
#      the UI preview and the verdict (the job id IS the hash).
#   3. BROWSER — the paw-sites harness, run by the preview job in that same sandbox
#      after a clean build. html has no build: its own sandbox job, build ``skipped``.
#
# TWO ENTRY POINTS. :func:`verify_site` (the ``verify_site`` tool, creates) runs all
# three layers and WAITS for the sandbox. :func:`verify_edit` (every edit tool) runs
# ONLY the static layer synchronously, enqueues the sandbox layers and returns
# ``status: "pending"`` with ``build: "pending"`` and the ``job_id``; html enqueues
# nothing (its browser check is on demand via ``verify_site``). The enqueue is recorded
# as the pocket's ``latest`` record, and :func:`settled_verdict` turns it into the full
# verdict once the job's sandbox report lands — the edit tools attach that to their
# NEXT result as ``previous_verification``, once.
#
# ``passed`` MEANS EVERY APPLICABLE LAYER RAN AND PASSED. Anything that could not run
# is ``unverified`` with a reason. Any ``failed`` layer makes the verdict ``failed``.
#
# CACHED PER CONTENT HASH in ``verify_store``: ``passed`` / ``failed`` verdicts are
# cache hits; ``unverified`` never is. The preview job stores its build + browser
# report under the hash, so a pre-warm answers the next verify for free.
#
# ENGINES: svelte, react, html. ripple returns ``unverified`` / ``engine_not_verifiable``
# (no authored code to check). A DYNAMIC svelte site's browser layer only loads its
# prerendered shell, which the verdict says in ``note``.
#
# DIAGNOSTICS ARE AGENT-ONLY: ``verify_diagnostics.finalize`` redacts and caps them, and
# :func:`status_summary` (the ``/status`` view) returns counts, never a message.
#
# Every layer logs its elapsed ms (``sites.verify: layer=…``) so edit latency is
# measurable from the API log alone.
from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.sites import verify_store
from pocketpaw_ee.sites.verify_diagnostics import finalize

logger = logging.getLogger(__name__)

#: Bumped when the pipeline changes what a verdict means; an older cached verdict then
#: reads as a miss rather than as a pass the current pipeline never gave.
VERIFY_PIPELINE_VERSION = 1

#: Engines with authored code this pipeline can check.
VERIFIABLE_ENGINES: tuple[str, ...] = ("svelte", "react", "html")

#: How long a tool call waits for the sandbox layers. A svelte build is ~15 s in-sandbox
#: plus create / upload / teardown and the browser run; this leaves room for a cold
#: chromium install without letting a tool call hang. On expiry the verdict is
#: ``unverified`` / ``timeout`` and the job keeps running, so the next ``verify_site``
#: attaches to it (same job id) or reads its stored report.
DEFAULT_WAIT_SECONDS = 90
WAIT_ENV = "PAW_SITES_VERIFY_WAIT_SEC"

WORKER_RENDERED_NOTE = "worker-rendered site: browser layer checked the prerendered shell only"


def verify_wait_seconds() -> float:
    raw = (os.environ.get(WAIT_ENV) or "").strip()
    try:
        value = float(raw) if raw else float(DEFAULT_WAIT_SECONDS)
    except ValueError:
        return float(DEFAULT_WAIT_SECONDS)
    return value if value > 0 else float(DEFAULT_WAIT_SECONDS)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RenderInputs:
    """Everything that decides one verification: what is built and under which hash."""

    engine: str
    source: dict[str, Any]
    theme: dict[str, Any]
    site_name: str
    builder_origin: str
    keeps_client_bundle: bool
    content_hash: str
    dynamic: bool


def render_inputs(
    pocket: dict[str, Any], *, builder_origin: str | None = None
) -> RenderInputs | None:
    """Resolve a pocket wire dict into the verify inputs, or ``None`` when the engine has
    nothing this pipeline can check.

    svelte / react use the ARMED hash — builder origin included — because that is the
    key the preview lane builds and caches under, and sharing it is the whole reason the
    verify costs no extra sandbox. html has no armed build lane, so it hashes with an
    empty origin (its render does not depend on one).
    """
    from pocketpaw_ee.sites import generator_client
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites.engines import normalize_engine

    engine = normalize_engine(pocket.get("engine"))
    source = pocket.get("source")
    if engine not in VERIFIABLE_ENGINES or not isinstance(source, dict):
        return None
    ripple_spec = pocket.get("rippleSpec") or {}
    theme = (ripple_spec.get("theme") if isinstance(ripple_spec, dict) else {}) or {}
    origin = (
        ((builder_origin or "").strip() or sites_service._builder_origin())
        if engine != "html"
        else ""
    )
    keeps = sites_service._resolve_keeps_client_bundle(pocket)
    content_hash = sites_service._artifact_content_hash(
        source=source,
        theme=theme,
        builder_origin=origin,
        gen_version=generator_client.generator_version(),
        engine=engine,
        keeps_client_bundle=keeps,
    )
    return RenderInputs(
        engine=engine,
        source=source,
        theme=theme,
        site_name=(pocket.get("name") or "").strip() or "Untitled site",
        builder_origin=origin,
        keeps_client_bundle=keeps,
        content_hash=content_hash,
        dynamic=engine == "svelte" and generator_client.svelte_source_is_dynamic(source),
    )


def generator_input_for(inputs: RenderInputs, pocket_id: str) -> dict[str, Any]:
    """The generator payload for this verify — byte-for-byte what the preview lane builds
    for svelte / react (``service._build_native_artifact``), so the static check reads
    exactly what the build compiles. The capture key is a decoy and is scrubbed anyway.
    """
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites.build_job import scrub_build_input
    from pocketpaw_ee.sites.generator_client import build_generator_input

    return scrub_build_input(
        build_generator_input(
            engine=inputs.engine,
            theme=inputs.theme,
            site_id=sites_service._preview_id(pocket_id),
            title=inputs.site_name,
            capture_api_base=sites_service._capture_base(),
            capture_signed_key=(
                f"site_key_{secrets.token_urlsafe(24)}" if inputs.builder_origin else ""
            ),
            ripple_spec={},
            source=inputs.source,
            builder_origin=inputs.builder_origin or None,
            keeps_client_bundle=inputs.keeps_client_bundle,
        )
    )


async def resolve_builder_origin(workspace_id: str, pocket_id: str) -> str:
    """The builder origin the armed hash is computed with: the ONE resolver
    (``service.resolve_armed_builder_origin``) the editor view, the pre-warm and
    ``preview_site`` use too, so one edit builds one render. Never raises."""
    from pocketpaw_ee.sites import service as sites_service

    return await sites_service.resolve_armed_builder_origin(workspace_id, pocket_id)


def _layer(name: str, status: str, reason: str = "") -> dict[str, str]:
    out = {"name": name, "status": status}
    if reason:
        out["reason"] = reason
    return out


def _verdict(
    *,
    content_hash: str,
    layers: list[dict[str, str]],
    errors: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Assemble a §5 verdict from its layers. The status rule lives here and only here."""
    statuses = [layer["status"] for layer in layers]
    clean_errors, clean_warnings = finalize(errors or [], warnings or [], layer="static")
    verdict: dict[str, Any] = {"content_hash": content_hash}
    if "failed" in statuses:
        verdict["status"] = "failed"
    elif "unverified" in statuses or not any(s == "passed" for s in statuses):
        verdict["status"] = "unverified"
        first = next((layer for layer in layers if layer["status"] == "unverified"), None)
        verdict["reason"] = (first or {}).get("reason") or "not_verified"
    else:
        verdict["status"] = "passed"
    verdict["layers"] = layers
    verdict["errors"] = clean_errors
    verdict["warnings"] = clean_warnings
    if note:
        verdict["note"] = note
    verdict["checked_at"] = _now_iso()
    return verdict


def unverifiable(reason: str, *, content_hash: str = "", note: str = "") -> dict[str, Any]:
    """A verdict for a site this pipeline cannot check at all (every layer unverified)."""
    return _verdict(
        content_hash=content_hash,
        layers=[
            _layer("static", "unverified", reason),
            _layer("build", "unverified", reason),
            _layer("browser", "unverified", reason),
        ],
        note=note,
    )


async def _static_layer(
    generator_input: dict[str, Any], *, check: Any = None
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    from pocketpaw_ee.sites import generator_client

    run = check or generator_client.run_static_check
    try:
        report = await run(generator_input)
    except generator_client.StaticCheckUnavailable as exc:
        return _layer("static", "unverified", exc.reason), [], []
    except Exception:  # noqa: BLE001 — the check not running is not the author's fault
        logger.warning("sites.verify: static check raised", exc_info=True)
        return _layer("static", "unverified", "check_crashed"), [], []
    errors = [{**e, "layer": "static"} for e in report.get("errors") or [] if isinstance(e, dict)]
    warnings = [
        {**w, "layer": "static"} for w in report.get("warnings") or [] if isinstance(w, dict)
    ]
    if errors or report.get("ok") is False:
        first = str(errors[0].get("code") or "static_error") if errors else "static_error"
        return _layer("static", "failed", f"static_check_failed:{first}"), errors, warnings
    return _layer("static", "passed"), errors, warnings


def _layers_from_report(report: dict[str, Any], engine: str) -> list[dict[str, str]]:
    """The build + browser layers from a preview / html-verify job report."""
    layers = report.get("layers")
    if not isinstance(layers, dict) and report.get("status") == "built":
        # A result from a preview job that predates the browser step: the build is
        # known good, the browser never ran.
        return [_layer("build", "passed"), _layer("browser", "unverified", "not_checked")]
    out: list[dict[str, str]] = []
    for name in ("build", "browser"):
        entry = layers.get(name) if isinstance(layers, dict) else None
        if isinstance(entry, dict) and isinstance(entry.get("status"), str):
            out.append(_layer(name, entry["status"], str(entry.get("reason") or "")))
        else:
            # A result without layers came from a job that stopped before building
            # (engine refused, empty scaffold). Its reason is a rung, not the author's.
            rung = str(report.get("reason") or "no_report").split(":", 1)[0]
            out.append(_layer(name, "unverified", rung))
    if engine == "html" and out[0]["status"] == "unverified" and out[0]["reason"] == "no_report":
        out[0] = _layer("build", "skipped", "no_build_step")
    return out


async def _sandbox_layers(
    *,
    pocket_id: str,
    inputs: RenderInputs,
    generator_input: dict[str, Any],
    store: Any,
    wait_seconds: float,
    pool: Any = None,
    wait: Any = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build + browser layers: a stored report for this hash, else enqueue and wait."""
    from pocketpaw_ee.sites import build_job

    record = store.read(pocket_id, verify_store.sandbox_key(inputs.content_hash))
    if record is None:
        try:
            enqueued = await _enqueue_sandbox(
                pocket_id=pocket_id, inputs=inputs, generator_input=generator_input, pool=pool
            )
            active_pool = pool or await build_job._get_pool()
        except Exception:
            logger.warning(
                "sites.verify: could not queue the build for %s", pocket_id, exc_info=True
            )
            reason = "queue_unavailable"
            return (
                [_layer("build", "unverified", reason), _layer("browser", "unverified", reason)],
                [],
                [],
            )
        waiter = wait or build_job.wait_for_preview_result
        waited = time.monotonic()
        try:
            record = await waiter(active_pool, enqueued.job_id, timeout=wait_seconds)
        except TimeoutError:
            reason = "timeout"
            return (
                [_layer("build", "unverified", reason), _layer("browser", "unverified", reason)],
                [],
                [],
            )
        except Exception:
            # The job raised: for this lane that means no sandbox could be created.
            logger.warning("sites.verify: build job for %s raised", pocket_id, exc_info=True)
            reason = "sandbox_unavailable"
            return (
                [_layer("build", "unverified", reason), _layer("browser", "unverified", reason)],
                [],
                [],
            )
        logger.info(
            "sites.verify: layer=sandbox_wait pocket=%s job=%s elapsed_ms=%d",
            pocket_id,
            enqueued.job_id,
            (time.monotonic() - waited) * 1000,
        )
    return _report_layers(record, inputs.engine)


def _report_layers(
    record: dict[str, Any], engine: str
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Layers + diagnostics out of a stored / returned sandbox report."""
    layers = _layers_from_report(record, engine)
    diagnostics = record.get("diagnostics") if isinstance(record.get("diagnostics"), dict) else {}
    return layers, list(diagnostics.get("errors") or []), list(diagnostics.get("warnings") or [])


async def _enqueue_sandbox(
    *, pocket_id: str, inputs: RenderInputs, generator_input: dict[str, Any], pool: Any = None
) -> Any:
    """Queue the sandbox job for this render (or attach to the one already queued).

    Raises when the queue cannot take it; the callers turn that into
    ``queue_unavailable`` rather than claim a job nobody will run.
    """
    from pocketpaw_ee.sites import build_job

    if inputs.engine == "html":
        return await build_job.enqueue_html_verify(
            pocket_id=pocket_id,
            content_hash=inputs.content_hash,
            generator_input=generator_input,
            _pool_override=pool,
        )
    return await build_job.enqueue_preview_build(
        pocket_id=pocket_id,
        content_hash=inputs.content_hash,
        engine=inputs.engine,
        generator_input=generator_input,
        _pool_override=pool,
    )


async def _static_checks(
    pocket: dict[str, Any],
    inputs: RenderInputs,
    generator_input: dict[str, Any],
    *,
    pocket_id: str = "",
    check: Any = None,
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    """The static layer plus the policy refusals that are static failures too.

    Shared by :func:`verify_pocket` (the full, waiting verify) and :func:`verify_edit`
    (the edit path's synchronous half), so both answer the same static verdict.
    """
    started = time.monotonic()
    static, errors, warnings = await _static_layer(generator_input, check=check)

    # A dynamic svelte site holding packages can only have got them from outside the
    # declaration tools (which refuse it). It can never publish, so say so as a static
    # error the agent can act on (drop the packages) rather than let a build run.
    from pocketpaw_ee.sites.dependency_manifest import (
        author_build_shell_files,
        has_author_dependencies,
        requires_sandbox,
    )

    # PP-4's legacy build-shell files: the generator refuses them at build time, so
    # name them here as a static error and spend no sandbox finding out.
    from pocketpaw_ee.sites.legacy_build_shell import generator_owned_keys_message
    from pocketpaw_ee.sites.service import DYNAMIC_PACKAGES_REASON, site_refuses_author_packages

    if (owned := generator_owned_keys_message(inputs.engine, inputs.source)) is not None:
        errors = [
            {"layer": "static", "code": "reserved_path", "message": owned},
            *errors,
        ]
        static = _layer("static", "failed", "static_check_failed:reserved_path")

    # The same predicate the routing uses (``requires_sandbox``): a dynamic site
    # cannot build in the sandbox, so authored build-shell files refuse it too.
    if site_refuses_author_packages(pocket) and requires_sandbox(inputs.source):
        if has_author_dependencies(inputs.source):
            refusal = {
                "file": "paw.dependencies.json",
                "message": DYNAMIC_PACKAGES_REASON
                + " Remove them with set_site_dependencies(remove=[...]).",
            }
        else:
            shell = author_build_shell_files(inputs.source)
            refusal = {
                "file": shell[0],
                "message": "a dynamic (live-data) svelte site cannot carry its own build "
                f"config ({', '.join(shell)}): those files build only in the isolated "
                "sandbox, whose output cannot deploy a Worker. Delete them.",
            }
        errors = [
            {"layer": "static", "code": "engine_unsupported", **refusal},
            *errors,
        ]
        static = _layer("static", "failed", "static_check_failed:engine_unsupported")
    logger.info(
        "sites.verify: layer=static pocket=%s engine=%s status=%s elapsed_ms=%d",
        pocket_id,
        inputs.engine,
        static["status"],
        (time.monotonic() - started) * 1000,
    )
    return static, errors, warnings


def _cached(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    if record.get("pipeline_version") != VERIFY_PIPELINE_VERSION:
        return None
    verdict = record.get("verdict")
    if not isinstance(verdict, dict) or verdict.get("status") not in ("passed", "failed"):
        return None
    return verdict


def _save(store: Any, pocket_id: str, content_hash: str, verdict: dict[str, Any]) -> None:
    try:
        store.write(
            pocket_id,
            verify_store.verdict_key(content_hash),
            {"pipeline_version": VERIFY_PIPELINE_VERSION, "verdict": verdict},
        )
    except Exception:  # noqa: BLE001 — a lost record costs a re-verify
        logger.warning("sites.verify: could not store the verdict for %s", pocket_id)


async def verify_pocket(
    pocket: dict[str, Any],
    *,
    pocket_id: str,
    builder_origin: str | None = None,
    wait_seconds: float | None = None,
    force: bool = False,
    _store: Any = None,
    _pool: Any = None,
    _check: Any = None,
    _wait: Any = None,
) -> dict[str, Any]:
    """Verify an already-read pocket wire dict. See :func:`verify_site`."""
    inputs = render_inputs(pocket, builder_origin=builder_origin)
    if inputs is None:
        return unverifiable("engine_not_verifiable")
    store = _store if _store is not None else verify_store.default_verify_store()
    budget = wait_seconds if wait_seconds is not None else verify_wait_seconds()
    started = time.monotonic()

    if not force and (
        hit := _cached(store.read(pocket_id, verify_store.verdict_key(inputs.content_hash)))
    ):
        return {**hit, "cached": True}

    note = WORKER_RENDERED_NOTE if inputs.dynamic else ""
    # The in-flight marker ``/status`` reports as ``pending``.
    try:
        store.write(
            pocket_id,
            verify_store.verdict_key(inputs.content_hash),
            {
                "pipeline_version": VERIFY_PIPELINE_VERSION,
                "verdict": {"status": "pending", "content_hash": inputs.content_hash},
                "started_at": time.time(),
            },
        )
    except Exception:  # noqa: BLE001
        pass

    generator_input = generator_input_for(inputs, pocket_id)
    static, errors, warnings = await _static_checks(
        pocket, inputs, generator_input, pocket_id=pocket_id, check=_check
    )
    if static["status"] == "failed":
        verdict = _verdict(
            content_hash=inputs.content_hash,
            layers=[
                static,
                _layer("build", "skipped", "static_check_failed"),
                _layer("browser", "skipped", "static_check_failed"),
            ],
            errors=errors,
            warnings=warnings,
            note=note,
        )
        _save(store, pocket_id, inputs.content_hash, verdict)
        return verdict

    remaining = max(1.0, budget - (time.monotonic() - started))
    sandbox, sb_errors, sb_warnings = await _sandbox_layers(
        pocket_id=pocket_id,
        inputs=inputs,
        generator_input=generator_input,
        store=store,
        wait_seconds=remaining,
        pool=_pool,
        wait=_wait,
    )
    verdict = _verdict(
        content_hash=inputs.content_hash,
        layers=[static, *sandbox],
        errors=[*errors, *sb_errors],
        warnings=[*warnings, *sb_warnings],
        note=note,
    )
    _save(store, pocket_id, inputs.content_hash, verdict)
    # An explicit verify of the source the last edit enqueued answers that edit too, so
    # the next tool result does not repeat it as ``previous_verification``.
    _mark_surfaced(store, pocket_id, inputs.content_hash)
    return verdict


# ── The edit path: static now, sandbox later ────────────────────────────────────────
# Edit tools answer in about a second. The sandbox layers ride the preview lane in the
# background; the pocket's ``latest`` record remembers what was enqueued, and
# :func:`settled_verdict` reads it back once the job's report has landed.

#: An html edit's browser layer: not run on the edit path, ``verify_site`` runs it.
HTML_BROWSER_ON_DEMAND = "browser_check_on_demand"

#: The reason on a ``create=true`` half step that skipped verification.
HALF_STEP_REASON = "create_half_step"
HALF_STEP_NOTE = (
    "not verified yet: this file is a half step (nothing links to or imports it), so "
    "it is checked with the edit that wires it in"
)


def _summary(verdict: dict[str, Any], *, job_id: str | None = None) -> dict[str, Any]:
    """Add the flat ``static`` / ``build`` fields (and ``job_id``) the edit results carry."""
    layers = {
        layer.get("name"): layer.get("status")
        for layer in verdict.get("layers") or []
        if isinstance(layer, dict)
    }
    out = {
        **verdict,
        "static": layers.get("static") or "unverified",
        "build": layers.get("build") or "unverified",
    }
    if job_id:
        out["job_id"] = job_id
    return out


def half_step_verdict() -> dict[str, Any]:
    """The verdict for a ``create=true`` call nothing references yet: not checked, and
    said so. The follow-up edit that wires the file in verifies the whole site."""
    return _summary(
        {
            "status": "skipped",
            "reason": HALF_STEP_REASON,
            "content_hash": "",
            "layers": [
                _layer("static", "skipped", HALF_STEP_REASON),
                _layer("build", "skipped", HALF_STEP_REASON),
                _layer("browser", "skipped", HALF_STEP_REASON),
            ],
            "errors": [],
            "warnings": [],
            "note": HALF_STEP_NOTE,
            "checked_at": _now_iso(),
        }
    )


def _read_latest(store: Any, pocket_id: str) -> dict[str, Any] | None:
    try:
        record = store.read(pocket_id, verify_store.LATEST_KEY)
    except Exception:  # noqa: BLE001 — a lost pointer costs one missed report
        return None
    if not isinstance(record, dict) or record.get("pipeline_version") != VERIFY_PIPELINE_VERSION:
        return None
    return record


def _write_latest(store: Any, pocket_id: str, record: dict[str, Any]) -> None:
    try:
        store.write(
            pocket_id,
            verify_store.LATEST_KEY,
            {"pipeline_version": VERIFY_PIPELINE_VERSION, **record},
        )
    except Exception:  # noqa: BLE001
        logger.warning("sites.verify: could not record the pending verify for %s", pocket_id)


def _mark_surfaced(store: Any, pocket_id: str, content_hash: str) -> None:
    latest = _read_latest(store, pocket_id)
    if latest is None or latest.get("surfaced") or latest.get("content_hash") != content_hash:
        return
    _write_latest(store, pocket_id, {**latest, "surfaced": True})


def _assemble_from_latest(
    store: Any, pocket_id: str, latest: dict[str, Any]
) -> dict[str, Any] | None:
    """The full verdict for the render ``latest`` enqueued, or ``None`` while its job
    has not reported. Stores it as the per-hash verdict so ``/status`` and the cache
    see it too."""
    content_hash = str(latest.get("content_hash") or "")
    if not content_hash:
        return None
    if hit := _cached(store.read(pocket_id, verify_store.verdict_key(content_hash))):
        return hit
    record = store.read(pocket_id, verify_store.sandbox_key(content_hash))
    if not isinstance(record, dict):
        return None
    static = latest.get("static")
    if not isinstance(static, dict) or not isinstance(static.get("status"), str):
        static = _layer("static", "passed")
    sandbox, sb_errors, sb_warnings = _report_layers(record, str(latest.get("engine") or ""))
    verdict = _verdict(
        content_hash=content_hash,
        layers=[_layer("static", static["status"], str(static.get("reason") or "")), *sandbox],
        errors=[*(latest.get("errors") or []), *sb_errors],
        warnings=[*(latest.get("warnings") or []), *sb_warnings],
        note=str(latest.get("note") or ""),
    )
    _save(store, pocket_id, content_hash, verdict)
    return verdict


def settled_verdict(pocket_id: str, *, _store: Any = None) -> dict[str, Any] | None:
    """The finished build + browser verdict for the pocket's last edit, ONCE.

    ``None`` when there is nothing new to report: no edit enqueued a sandbox job, its
    job is still running, or this verdict was already handed out (or answered by an
    explicit ``verify_site``). A job that never reported within
    ``verify_store.PENDING_STALE_SECONDS`` is reported ``unverified`` / ``no_report``
    so a lost job cannot hide forever. Never raises.
    """
    try:
        store = _store if _store is not None else verify_store.default_verify_store()
        latest = _read_latest(store, pocket_id)
        if latest is None or latest.get("surfaced"):
            return None
        verdict = _assemble_from_latest(store, pocket_id, latest)
        if verdict is None:
            enqueued_at = latest.get("enqueued_at")
            age = time.time() - enqueued_at if isinstance(enqueued_at, (int, float)) else 0.0
            if age < verify_store.PENDING_STALE_SECONDS:
                return None
            static = latest.get("static") if isinstance(latest.get("static"), dict) else {}
            verdict = _verdict(
                content_hash=str(latest.get("content_hash") or ""),
                layers=[
                    _layer("static", str(static.get("status") or "passed")),
                    _layer("build", "unverified", "no_report"),
                    _layer("browser", "unverified", "no_report"),
                ],
            )
        _write_latest(store, pocket_id, {**latest, "surfaced": True})
    except Exception:  # noqa: BLE001 — a report that cannot be read is skipped, not raised
        logger.warning("sites.verify: settled verdict read failed for %s", pocket_id, exc_info=True)
        return None
    return _summary(verdict, job_id=str(latest.get("job_id") or "") or None)


async def verify_edit_pocket(
    pocket: dict[str, Any],
    *,
    pocket_id: str,
    builder_origin: str | None = None,
    _store: Any = None,
    _pool: Any = None,
    _check: Any = None,
) -> dict[str, Any]:
    """The edit path's verify: STATIC now, the sandbox layers enqueued, never waited on.

    Returns, in order of preference:
      * a cached ``passed`` / ``failed`` verdict for this exact source;
      * a static ``failed`` verdict (sandbox layers ``skipped``; nothing enqueued);
      * the full verdict when a sandbox report for this hash already exists (a
        pre-warm or an earlier verify built it);
      * html: static only, browser ``unverified`` / ``browser_check_on_demand``;
      * otherwise ``status: "pending"``, ``build: "pending"`` and the ``job_id`` of
        the enqueued preview build, recorded as the pocket's ``latest`` so
        :func:`settled_verdict` can report it on the next tool result.
    """
    started = time.monotonic()
    inputs = render_inputs(pocket, builder_origin=builder_origin)
    if inputs is None:
        return _summary(unverifiable("engine_not_verifiable"))
    store = _store if _store is not None else verify_store.default_verify_store()
    if hit := _cached(store.read(pocket_id, verify_store.verdict_key(inputs.content_hash))):
        _mark_surfaced(store, pocket_id, inputs.content_hash)
        return _summary({**hit, "cached": True})

    note = WORKER_RENDERED_NOTE if inputs.dynamic else ""
    generator_input = generator_input_for(inputs, pocket_id)
    static, errors, warnings = await _static_checks(
        pocket, inputs, generator_input, pocket_id=pocket_id, check=_check
    )
    if static["status"] == "failed":
        verdict = _verdict(
            content_hash=inputs.content_hash,
            layers=[
                static,
                _layer("build", "skipped", "static_check_failed"),
                _layer("browser", "skipped", "static_check_failed"),
            ],
            errors=errors,
            warnings=warnings,
            note=note,
        )
        _save(store, pocket_id, inputs.content_hash, verdict)
        return _summary(verdict)

    record = store.read(pocket_id, verify_store.sandbox_key(inputs.content_hash))
    if isinstance(record, dict):
        sandbox, sb_errors, sb_warnings = _report_layers(record, inputs.engine)
        verdict = _verdict(
            content_hash=inputs.content_hash,
            layers=[static, *sandbox],
            errors=[*errors, *sb_errors],
            warnings=[*warnings, *sb_warnings],
            note=note,
        )
        _save(store, pocket_id, inputs.content_hash, verdict)
        return _summary(verdict)

    if inputs.engine == "html":
        return _summary(
            _verdict(
                content_hash=inputs.content_hash,
                layers=[
                    static,
                    _layer("build", "skipped", "no_build_step"),
                    _layer("browser", "unverified", HTML_BROWSER_ON_DEMAND),
                ],
                errors=errors,
                warnings=warnings,
                note=note,
            )
        )

    try:
        enqueued = await _enqueue_sandbox(
            pocket_id=pocket_id, inputs=inputs, generator_input=generator_input, pool=_pool
        )
    except Exception:
        logger.warning("sites.verify: could not queue the build for %s", pocket_id, exc_info=True)
        reason = "queue_unavailable"
        return _summary(
            _verdict(
                content_hash=inputs.content_hash,
                layers=[
                    static,
                    _layer("build", "unverified", reason),
                    _layer("browser", "unverified", reason),
                ],
                errors=errors,
                warnings=warnings,
                note=note,
            )
        )

    _write_latest(
        store,
        pocket_id,
        {
            "content_hash": inputs.content_hash,
            "job_id": enqueued.job_id,
            "engine": inputs.engine,
            "static": static,
            "errors": errors,
            "warnings": warnings,
            "note": note,
            "enqueued_at": time.time(),
            "surfaced": False,
        },
    )
    # The in-flight marker ``/status`` reports as ``pending``.
    try:
        store.write(
            pocket_id,
            verify_store.verdict_key(inputs.content_hash),
            {
                "pipeline_version": VERIFY_PIPELINE_VERSION,
                "verdict": {"status": "pending", "content_hash": inputs.content_hash},
                "started_at": time.time(),
            },
        )
    except Exception:  # noqa: BLE001
        pass
    clean_errors, clean_warnings = finalize(errors, warnings, layer="static")
    verdict: dict[str, Any] = {
        "status": "pending",
        "content_hash": inputs.content_hash,
        "layers": [static, _layer("build", "pending"), _layer("browser", "pending")],
        "errors": clean_errors,
        "warnings": clean_warnings,
        "checked_at": _now_iso(),
    }
    if note:
        verdict["note"] = note
    logger.info(
        "sites.verify: edit verify pocket=%s static=%s job=%s queued_status=%s elapsed_ms=%d",
        pocket_id,
        static["status"],
        enqueued.job_id,
        getattr(enqueued, "status", ""),
        (time.monotonic() - started) * 1000,
    )
    return _summary(verdict, job_id=enqueued.job_id)


async def verify_edit(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    _store: Any = None,
    _pool: Any = None,
    _check: Any = None,
) -> dict[str, Any]:
    """:func:`verify_edit_pocket` for a pocket id (the edit tools' entry point).

    Reads the pocket through the pockets service's public ``get`` (tenancy errors
    propagate) and builds with the ONE armed builder origin
    (:func:`resolve_builder_origin`). Never raises for a verification outcome.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    pocket = await pockets_service.get(pocket_id, user_id)
    origin = await resolve_builder_origin(workspace_id, pocket_id)
    return await verify_edit_pocket(
        pocket,
        pocket_id=pocket_id,
        builder_origin=origin,
        _store=_store,
        _pool=_pool,
        _check=_check,
    )


async def verify_site(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    wait_seconds: float | None = None,
    force: bool = False,
    _store: Any = None,
    _pool: Any = None,
    _check: Any = None,
    _wait: Any = None,
) -> dict[str, Any]:
    """Run the three-layer verification for a site pocket and return the §5 verdict.

    Reads the pocket through the pockets service's public ``get`` (tenancy: it raises
    NotFound / Forbidden itself — propagated to the caller). ``workspace_id`` locates
    the pocket's Site row, whose stored ``builder_origin`` picks the armed hash (see
    :func:`resolve_builder_origin`).

    Never raises for a verification outcome: every infrastructure failure is an
    ``unverified`` verdict with a reason. ``force`` skips the per-hash cache (the stored
    sandbox report is still reused). The ``_`` seams are for tests.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    pocket = await pockets_service.get(pocket_id, user_id)
    origin = await resolve_builder_origin(workspace_id, pocket_id)
    return await verify_pocket(
        pocket,
        pocket_id=pocket_id,
        builder_origin=origin,
        wait_seconds=wait_seconds,
        force=force,
        _store=_store,
        _pool=_pool,
        _check=_check,
        _wait=_wait,
    )


def _count(entries: Any) -> int:
    if not isinstance(entries, list):
        return 0
    return sum(1 for e in entries if isinstance(e, dict) and e.get("code") != "truncated")


async def status_summary(
    *, workspace_id: str, pocket_id: str, _store: Any = None
) -> dict[str, Any]:
    """The ``/status`` view of verification (contract §6): COUNTS ONLY, never messages.

    Keyed on the CURRENT render hash, so a verdict for source that has since been edited
    is not reported: the status is ``none`` again until the new source is verified.
    ``pending`` is an in-flight marker younger than ``verify_store.PENDING_STALE_SECONDS``.
    Never raises — a status read must not fail because verification bookkeeping did.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    empty: dict[str, Any] = {
        "status": "none",
        "error_count": 0,
        "checked_at": None,
        "content_hash": None,
    }
    try:
        pocket = await pockets_service.site_render_inputs(workspace_id, pocket_id)
        origin = await resolve_builder_origin(workspace_id, pocket_id)
        inputs = render_inputs(pocket, builder_origin=origin) if pocket is not None else None
        if inputs is None:
            return empty
        store = _store if _store is not None else verify_store.default_verify_store()
        record = store.read(pocket_id, verify_store.verdict_key(inputs.content_hash))
    except Exception:  # noqa: BLE001
        logger.warning("sites.verify: status summary failed for %s", pocket_id, exc_info=True)
        return empty
    if not isinstance(record, dict) or record.get("pipeline_version") != VERIFY_PIPELINE_VERSION:
        return {**empty, "content_hash": inputs.content_hash}
    verdict = record.get("verdict") if isinstance(record.get("verdict"), dict) else {}
    status = verdict.get("status")
    if status == "pending":
        # An edit's sandbox job may have reported since: settle it from the job's
        # stored report (this does not mark it handed out to the agent).
        latest = _read_latest(store, pocket_id)
        if latest is not None and latest.get("content_hash") == inputs.content_hash:
            try:
                settled = _assemble_from_latest(store, pocket_id, latest)
            except Exception:  # noqa: BLE001
                settled = None
            if settled is not None:
                verdict, status = settled, settled.get("status")
    if status == "pending":
        started = record.get("started_at")
        fresh = isinstance(started, (int, float)) and (
            time.time() - started < verify_store.PENDING_STALE_SECONDS
        )
        return {
            **empty,
            "status": "pending" if fresh else "none",
            "content_hash": inputs.content_hash,
        }
    if status not in ("passed", "failed", "unverified"):
        return {**empty, "content_hash": inputs.content_hash}
    return {
        "status": status,
        "error_count": _count(verdict.get("errors")),
        "checked_at": verdict.get("checked_at"),
        "content_hash": inputs.content_hash,
    }


__all__ = [
    "DEFAULT_WAIT_SECONDS",
    "VERIFIABLE_ENGINES",
    "VERIFY_PIPELINE_VERSION",
    "WORKER_RENDERED_NOTE",
    "RenderInputs",
    "generator_input_for",
    "half_step_verdict",
    "render_inputs",
    "settled_verdict",
    "status_summary",
    "unverifiable",
    "verify_edit",
    "verify_edit_pocket",
    "verify_pocket",
    "verify_site",
    "verify_wait_seconds",
]
