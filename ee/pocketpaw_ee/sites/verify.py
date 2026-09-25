# ee/pocketpaw_ee/sites/verify.py — the three-layer verification pipeline behind every
# agent create / edit and the ``verify_site`` tool.
#
# Created 2026-09-24 (PP-2, feat/sites-verify-pipeline). Contract §5 of
# docs/design/drafts/2026-09-24-sites-deps-verify-contract.md defines the verdict this
# returns; this header records how each layer is produced and why.
#
#   1. STATIC — ``paw-sites-gen check`` (``generator_client.run_static_check``) on the
#      API host. It installs nothing, so it is safe for any source, packages included,
#      and it is about a second. The input is the SAME generator payload the preview
#      build gets. A static failure stops here: nothing is spent on a sandbox to confirm
#      a failure the author already has to fix.
#   2. BUILD — the preview lane (``build_job.enqueue_preview_build``) keyed on the SAME
#      armed content hash ``get_native_artifact`` and the pre-warm use. One sandbox serves
#      the UI preview and the verdict; a verify of a render the editor already queued
#      waits on that job instead of opening another (the job id IS the hash).
#   3. BROWSER — the paw-sites harness, run by the preview job in that same sandbox after
#      a clean build (``browser_check``). html has no build, so it gets its own sandbox
#      job (``build_job.run_site_html_verify``) and its build layer is ``skipped``.
#
# ``passed`` MEANS EVERY APPLICABLE LAYER RAN AND PASSED. A sandbox that could not be
# created, a queue that is down, a browser that would not launch, a wait that ran out:
# each is ``unverified`` with a reason, never ``passed``. Any ``failed`` layer makes the
# verdict ``failed`` (it is actionable even when another layer could not run).
#
# CACHED PER CONTENT HASH. A ``passed`` / ``failed`` verdict is stored in ``verify_store``
# under the hash, so re-verifying unchanged source is a read. ``unverified`` is stored
# for ``/status`` but never served as a cache hit — the next call tries again. The
# preview job ALSO stores its build + browser report under the hash, so a UI pre-warm
# answers the sandbox layers of the next verify for free.
#
# ENGINES: svelte, react, html. ripple (landing and dynamic) returns ``unverified`` /
# ``engine_not_verifiable``: a landing site is assembled from vetted components with no
# authored code to check, and the dynamic track renders in a Worker the harness cannot
# serve — saying so is honest, a silent pass would not be. A DYNAMIC svelte site verifies
# all three layers but the browser can only load its prerendered shell, which the verdict
# says in ``note``.
#
# THE DIAGNOSTICS ARE AGENT-ONLY. ``errors`` / ``warnings`` go through
# ``verify_diagnostics.finalize`` (redacted, relativized, keys scrubbed, 2 KB cap).
# :func:`status_summary` — the ``/status`` view — returns counts and never a message.
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
    """The builder origin the armed hash is computed with.

    The editor's VIEW hashes with its request Origin, and ``make_site_editable`` stores
    that origin on the Site row — so the row's ``builder_origin`` is the best available
    guess at the key the editor will read, and reusing it is what lets a verify warm the
    editor's cache instead of building a second render. Falls back to the configured
    ``PAW_SITES_BUILDER_ORIGIN`` (what the pre-warm and a view without an Origin use).
    Never raises.
    """
    from pocketpaw_ee.sites import service as sites_service

    try:
        doc = await sites_service._canonical_site_doc(workspace_id, pocket_id)
    except Exception:  # noqa: BLE001 — an origin guess is never worth a failed verify
        doc = None
    stored = (getattr(doc, "builder_origin", "") or "").strip() if doc is not None else ""
    return stored or sites_service._builder_origin()


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
            if inputs.engine == "html":
                enqueued = await build_job.enqueue_html_verify(
                    pocket_id=pocket_id,
                    content_hash=inputs.content_hash,
                    generator_input=generator_input,
                    _pool_override=pool,
                )
            else:
                enqueued = await build_job.enqueue_preview_build(
                    pocket_id=pocket_id,
                    content_hash=inputs.content_hash,
                    engine=inputs.engine,
                    generator_input=generator_input,
                    _pool_override=pool,
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
    layers = _layers_from_report(record, inputs.engine)
    diagnostics = record.get("diagnostics") if isinstance(record.get("diagnostics"), dict) else {}
    return layers, list(diagnostics.get("errors") or []), list(diagnostics.get("warnings") or [])


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
    static, errors, warnings = await _static_layer(generator_input, check=_check)

    # A dynamic svelte site holding packages can only have got them from outside the
    # declaration tools (which refuse it). It can never publish, so say so as a static
    # error the agent can act on (drop the packages) rather than let a build run.
    from pocketpaw_ee.sites.dependency_manifest import has_author_dependencies

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

    if site_refuses_author_packages(pocket) and has_author_dependencies(inputs.source):
        errors = [
            {
                "layer": "static",
                "file": "paw.dependencies.json",
                "code": "engine_unsupported",
                "message": DYNAMIC_PACKAGES_REASON
                + " Remove them with set_site_dependencies(remove=[...]).",
            },
            *errors,
        ]
        static = _layer("static", "failed", "static_check_failed:engine_unsupported")
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
    return verdict


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
    "render_inputs",
    "status_summary",
    "unverifiable",
    "verify_pocket",
    "verify_site",
    "verify_wait_seconds",
]
