# ee/pocketpaw_ee/cloud/jobs/registry.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — the in-process named
# job registry + the two security validators (`validate_job_params`,
# `validate_job_result`) that gate the dispatch and writeback boundaries.
# Follows the module-level-dict pattern of
# `pockets.instinct_compensation_registry.OptimisticCompensationRegistry`:
# one process-wide dict, registered into at mount time by the built-ins
# package. Pure module — no Beanie / FastAPI imports — so it sits on the
# import-linter "Jobs" allowlist (only `service.py` writes Beanie).
# Updated: 2026-06-20 (review fix MINOR B) — `validate_job_params` now
# RECURSES into nested dicts and lists, so a credential hidden under
# `{"config": {"api_key": "..."}}` can no longer slip past the top-level
# scan. The validator's contract ("a job NEVER receives credentials through
# params") now matches its implementation at every depth.

"""Named job registry + param/result validators for workspace jobs."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Errors — carry an HTTP-friendly `status` + a stable machine `code` so the
# dispatch router can surface them as the contracted 400s without importing
# the cloud error envelope into this pure module.
# ---------------------------------------------------------------------------


class JobError(Exception):
    """Base class for registry / validation rejections."""

    status: int = 400
    code: str = "job.error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownJobError(JobError):
    """Raised when a job name has no registered callable → 400 `job.unknown`."""

    code = "job.unknown"


class JobParamsError(JobError):
    """Raised when params carry a credential-shaped key → 400."""

    code = "job.params_forbidden"


class JobResultError(JobError):
    """Raised when a job result writes a template-owned region → rejected."""

    code = "job.result_forbidden"


# ---------------------------------------------------------------------------
# JobCallable Protocol — the shape every registered job implements.
# ---------------------------------------------------------------------------


@runtime_checkable
class JobCallable(Protocol):
    """A named, server-side async job.

    ``name`` is the stable registry key used by ``rippleSpec.actions[...].job``.
    ``__call__`` runs the job under the workspace service identity and returns
    a PARTIAL rippleSpec the worker writes back via ``merge_spec``. The result
    may write ONLY ``state`` — ui / actions / sources / shape are template-
    owned and rejected by :func:`validate_job_result`.
    """

    name: str

    async def __call__(
        self, *, workspace_id: str, pocket_id: str, job_id: str, params: dict
    ) -> dict: ...


# ---------------------------------------------------------------------------
# The registry — a single process-wide dict.
# ---------------------------------------------------------------------------

_JOB_REGISTRY: dict[str, JobCallable] = {}


def get_job_registry() -> dict[str, JobCallable]:
    """Return the process-wide name → JobCallable map (the live dict)."""
    return _JOB_REGISTRY


def register_job(job: JobCallable) -> None:
    """Register a job under its ``name``.

    Re-registering the same name overwrites — built-ins register at mount time
    and tests swap doubles; last-writer-wins keeps both paths simple.
    """
    name = getattr(job, "name", "")
    if not name or not isinstance(name, str):
        raise ValueError("a registered job must expose a non-empty string `name`")
    _JOB_REGISTRY[name] = job


def resolve_job(job_name: str) -> JobCallable:
    """Look a job up by name; raise :class:`UnknownJobError` on a miss."""
    job = _JOB_REGISTRY.get(job_name)
    if job is None:
        raise UnknownJobError(f"no registered job named '{job_name}'")
    return job


# ---------------------------------------------------------------------------
# Security validators.
# ---------------------------------------------------------------------------

# Substrings (case-insensitive) that mark a params key as credential-bearing.
# A job NEVER receives credentials through `params` — connector calls use the
# workspace's stored creds. Rejecting these at dispatch closes a cred-exfil
# vector where an action author smuggles a token into the broadcast/audit path.
_CRED_KEY_SUBSTRINGS = ("token", "api_key", "apikey", "credential", "secret", "password")

# Result keys a job is NOT allowed to write. The template owns the structure
# (ui / actions / sources) and the shape; a job only contributes `state`.
_FORBIDDEN_RESULT_KEYS = frozenset({"ui", "actions", "sources", "shape"})


def _scan_for_cred_keys(value: object) -> None:
    """Walk a params value depth-first; raise on the first credential key.

    Recurses into nested dicts and lists so a credential buried under
    ``{"config": {"api_key": "..."}}`` (or inside a list of dicts) is caught
    just like a top-level key. Scalars are inert — only dict KEYS are matched
    against the credential substrings.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if any(sub in lowered for sub in _CRED_KEY_SUBSTRINGS):
                raise JobParamsError(
                    f"param key '{key}' looks credential-bearing — jobs read workspace "
                    "creds server-side and never accept tokens through params"
                )
            _scan_for_cred_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _scan_for_cred_keys(child)


def validate_job_params(params: dict) -> dict:
    """Reject credential-shaped param keys; return the params unchanged.

    Case-insensitive substring match on EVERY key, at every depth — the scan
    recurses into nested dicts and lists (see :func:`_scan_for_cred_keys`).
    Raises :class:`JobParamsError` (400) on the first offending key so no
    credential-bearing value ever reaches a job or the audit log, even when
    nested under a benign-looking parent key.
    """
    _scan_for_cred_keys(params)
    return params


def validate_job_result(result: dict) -> dict:
    """Reject a result that writes any template-owned region.

    A legal job result is a partial spec that touches ONLY ``state``. Any
    ``ui`` / ``actions`` / ``sources`` / ``shape`` key raises
    :class:`JobResultError`; the worker turns that into a `failed` job + a
    failed-state writeback (never a silent drop).
    """
    if not isinstance(result, dict):
        raise JobResultError(f"job result must be a dict, got {type(result).__name__}")
    offending = _FORBIDDEN_RESULT_KEYS.intersection(result.keys())
    if offending:
        raise JobResultError(
            "job result may write only `state` — these template-owned keys are "
            f"forbidden: {', '.join(sorted(offending))}"
        )
    return result


__all__ = [
    "JobCallable",
    "JobError",
    "JobParamsError",
    "JobResultError",
    "UnknownJobError",
    "get_job_registry",
    "register_job",
    "resolve_job",
    "validate_job_params",
    "validate_job_result",
]
