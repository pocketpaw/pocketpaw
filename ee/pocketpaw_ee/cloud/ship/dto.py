# ee/pocketpaw_ee/cloud/ship/dto.py — request/response schemas for the /ship
# HTTP surface (SHIP-3).
#
# Distinct Request / Response models per ee/cloud Rule 4 — one model never does
# both jobs.
#
# FROZEN RESPONSE SHAPES. The /ship console (paw-enterprise#655) consumes these
# verbatim; the five below are contract, not convenience. Do NOT rename a field
# and do NOT add a REQUIRED one:
#
#   BoxOut     {id, provider, ip, status, price_monthly?}
#   AppOut     {id, name, box_id, status, urls[], source_kind, repo_url, repo_ref}
#   DeployOut  {id, app_id, status, started_at, finished_at?}
#   LogsOut    {lines[]}
#   MetricsOut {cpu, mem, disk}
#   delete     {status: "pending_approval", proposal_id}
#
# ``AppOut``'s ``source_kind`` / ``repo_url`` / ``repo_ref`` (SHIP-14) are ADDITIVE
# with defaults (no token field — the private-repo token is write-only, never
# serialized). ``SetSourceRequest`` sets them via ``PUT /ship/apps/{id}/source``.
#
# ``DomainOut`` / ``DbOut`` are not part of the frozen five; they mirror SHIP-1's
# ``DomainResult`` / ``DbResult`` so the wire never invents a second vocabulary
# for the same fact. ``DbOut.env_var`` is the NAME of the variable holding the
# connection string — the string itself is a secret and never crosses the wire.
#
# FROZEN ENV SHAPE (SHIP-9). The /ship console (SHIP-10) is built against
# ``EnvOut { vars: [{ key, masked_value, scope }] }`` verbatim — do NOT rename a
# field or add a required one. ``masked_value`` is ALWAYS a masked hint (a short
# suffix, or bullets for short values); there is no plaintext field on any
# response, by design.
#
# Created 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): new module.
# Changed 2026-07-23 (feat/ship-9-env-store, SHIP-9): added the env-management
# request/response shapes — ``EnvVarIn`` / ``SetEnvRequest`` / ``ImportEnvRequest``
# in, ``EnvVarOut`` / ``EnvOut`` (masked-only) out.
# Changed 2026-07-23 (feat/ship-14-source-deploy, SHIP-14): added the deploy-
# source shapes — ``SetSourceRequest`` (+ the same fields on ``CreateAppRequest``)
# in, ``source_kind`` / ``repo_url`` / ``repo_ref`` on ``AppOut`` out. The
# private-repo ``token`` is REQUEST-only — no response ever carries it.

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Names that reach the deploy engine are constrained HERE, at the boundary, the
# same way SHIP-1's ``AppSpec`` constrains env keys: the driver shell-quotes
# everything it interpolates, so this is not the injection defence — it is what
# keeps unusable garbage (an app name Dokku will reject, a "domain" that is a
# sentence) from becoming a failed SSH round trip.
#
# Dokku app / service names: lowercase alphanumeric, hyphens inside.
_APP_NAME_RE = r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$"
# A DNS hostname: two or more dot-separated labels, each starting and ending
# alphanumeric with hyphens allowed inside. Written without look-around —
# pydantic compiles patterns with the Rust regex engine, which has none.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DOMAIN_RE = rf"^{_LABEL}(?:\.{_LABEL})+$"
# POSIX environment variable NAME (values are never accepted).
_ENV_NAME_RE = r"^[A-Za-z_][A-Za-z0-9_]*$"
# An OCI image reference (``registry/repo:tag`` / ``@sha256:...``) and a git ref.
# Both reach the engine as shell arguments, so they get the same boundary
# treatment as the app name rather than being free strings: the driver quotes
# them, and these keep shell metacharacters out of a value that would otherwise
# only fail deep inside an SSH round trip.
# Empty is allowed on both: it is the "not specified" value the create body
# carries when a client registers an app before choosing what to ship.
_IMAGE_RE = r"^$|^[A-Za-z0-9][A-Za-z0-9._/:@-]*$"
_GIT_REF_RE = r"^$|^[A-Za-z0-9][A-Za-z0-9._/-]*$"
# A git clone URL (SHIP-14): empty ("not specified"), a ``scheme://…`` URL
# (https/http/git/ssh), or an scp-style ``git@host:path``. Constrained to a
# URL-safe charset — no spaces, quotes, or shell metacharacters — so a "repo"
# that is really a sentence or an injection attempt dies at the boundary rather
# than deep in an SSH round trip (the driver shell-quotes it regardless). Written
# without look-around (pydantic's Rust regex engine has none). The access token
# is NEVER part of this URL — it is a separate, encrypted field.
_REPO_URL_RE = (
    r"^$"
    r"|^(?:https?|git|ssh)://[A-Za-z0-9._~:/@%+-]+$"
    r"|^git@[A-Za-z0-9._~:/@%+-]+$"
)
# A repo access token is an opaque secret (a PAT / deploy token) — no pattern,
# just a sane length cap so an authenticated route can't be memory-abused.
_REPO_TOKEN_MAX = 4096

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateBoxRequest(BaseModel):
    """Provision a box. ``server_type`` / ``region`` fall back to the deployment
    defaults when omitted (see ``ship.service.DEFAULT_SERVER_TYPE``)."""

    provider: str = "hcloud"
    server_type: str | None = None
    region: str | None = None


class CreateAppRequest(BaseModel):
    """Register an app on a box.

    ``name`` + ``box_id`` are the documented body; the rest are optional build
    inputs a client may supply up front so ``POST /apps/{id}/deploy`` (which
    takes no body) has an image to ship. ``env_refs`` are variable NAMES — the
    API never accepts env VALUES.
    """

    name: str = Field(min_length=1, max_length=63, pattern=_APP_NAME_RE)
    box_id: str = Field(min_length=1)
    build_path: Literal["dockerfile", "nixpacks"] = "dockerfile"
    git_ref: str = Field(default="", max_length=255, pattern=_GIT_REF_RE)
    image: str = Field(default="", max_length=255, pattern=_IMAGE_RE)
    prod: bool = False
    env_refs: list[str] = Field(default_factory=list)
    # Deploy source (SHIP-14). ``image`` (default) keeps the pre-built-image path;
    # ``git`` builds from ``repo_url`` @ ``repo_ref``. ``token`` is the optional
    # private-repo credential — REQUEST-only, never echoed on any response.
    source_kind: Literal["image", "git"] = "image"
    repo_url: str = Field(default="", max_length=512, pattern=_REPO_URL_RE)
    repo_ref: str = Field(default="main", max_length=255, pattern=_GIT_REF_RE)
    token: str | None = Field(default=None, max_length=_REPO_TOKEN_MAX)

    @field_validator("env_refs")
    @classmethod
    def _names_only(cls, value: list[str]) -> list[str]:
        """Reject anything that is not a bare env var NAME.

        A caller sending ``["API_KEY=hunter2"]`` is trying to store a secret in
        a field that is never treated as one — refuse it rather than persist it.
        """
        for name in value:
            if not re.match(_ENV_NAME_RE, name):
                raise ValueError(f"env_refs takes variable names only, got {name!r}")
        return value

    @model_validator(mode="after")
    def _git_needs_a_repo(self) -> CreateAppRequest:
        """A ``git`` source is meaningless without a repo URL — reject it here
        rather than let it become an app that can never deploy."""
        if self.source_kind == "git" and not self.repo_url:
            raise ValueError("source_kind 'git' requires a repo_url")
        return self


class AddDomainRequest(BaseModel):
    """Route a domain to an app and (by default) issue TLS for it."""

    domain: str = Field(min_length=1, max_length=253, pattern=_DOMAIN_RE)
    enable_tls: bool = True


class CreateDbRequest(BaseModel):
    """Create a database service and link it to the app.

    ``service`` defaults to ``<app-name>-db`` when omitted. ``db_type`` selects
    the Dokku plugin (postgres / redis / mongo); it defaults to ``mongo`` so the
    SHIP-3 behaviour is unchanged for callers that omit it.
    """

    service: str | None = None
    db_type: Literal["postgres", "redis", "mongo"] = "mongo"


class SetScaleRequest(BaseModel):
    """Set the app's process scale (``PUT /ship/apps/{id}/scale``, SHIP-17).

    ``scale`` maps a Procfile process type to a container count, e.g.
    ``{"web": 2, "worker": 1}``. Counts are non-negative; a process set to 0 is
    stopped. Process names use the same POSIX-ish shape Dokku accepts.
    """

    scale: dict[str, int] = Field(default_factory=dict)

    @field_validator("scale")
    @classmethod
    def _valid_scale(cls, value: dict[str, int]) -> dict[str, int]:
        for proc, count in value.items():
            if not re.match(r"^[a-z][a-z0-9_-]*$", proc):
                raise ValueError(f"invalid process name: {proc!r}")
            if count < 0:
                raise ValueError(f"process count must be >= 0, got {count} for {proc!r}")
        return value


class SetChecksRequest(BaseModel):
    """Configure zero-downtime deploy checks (``PUT /ship/apps/{id}/checks``,
    SHIP-17). ``zero_downtime`` toggles Dokku's built-in checks; an optional
    ``healthcheck_path`` is the HTTP path the check hits."""

    zero_downtime: bool = True
    healthcheck_path: str = Field(default="", max_length=512)


class SetSourceRequest(BaseModel):
    """Point an app at a deploy source (``PUT /ship/apps/{id}/source``, SHIP-14).

    Same URL/ref validation discipline as the create body. ``token`` is the
    optional private-repo credential — REQUEST-only, Fernet-encrypted at rest,
    never returned. Its absence (``None``) LEAVES an existing token untouched (so
    a client can re-point the ref without re-sending the credential); an empty
    string CLEARS it (a public repo).
    """

    source_kind: Literal["image", "git"] = "git"
    repo_url: str = Field(default="", max_length=512, pattern=_REPO_URL_RE)
    repo_ref: str = Field(default="main", max_length=255, pattern=_GIT_REF_RE)
    token: str | None = Field(default=None, max_length=_REPO_TOKEN_MAX)

    @model_validator(mode="after")
    def _git_needs_a_repo(self) -> SetSourceRequest:
        if self.source_kind == "git" and not self.repo_url:
            raise ValueError("source_kind 'git' requires a repo_url")
        return self


# An env var's deploy scope, echoed on the wire. Kept in lock-step with the
# model's ``ShipEnvScope`` — the DTO is the boundary that validates it.
EnvScope = Literal["both", "prod", "preview"]

# A value is opaque (any string — a token, a DSN, a PEM), so it carries no
# pattern. It IS length-capped: a real env var is never megabytes, and an
# unbounded field is a needless memory-abuse vector on an authenticated route.
_ENV_VALUE_MAX = 65536


class EnvVarIn(BaseModel):
    """One env var to upsert. ``key`` keeps the POSIX-name grammar (the same one
    ``env_refs`` enforces); ``value`` is opaque and Fernet-encrypted before it is
    stored; ``scope`` defaults to ``both``."""

    key: str = Field(min_length=1, max_length=255, pattern=_ENV_NAME_RE)
    value: str = Field(max_length=_ENV_VALUE_MAX)
    scope: EnvScope = "both"


class SetEnvRequest(BaseModel):
    """Upsert a batch of env vars (``PUT .../env``). Each key is added or
    overwritten; keys absent from the batch are left untouched."""

    vars: list[EnvVarIn] = Field(default_factory=list)


class ImportEnvRequest(BaseModel):
    """Import a ``.env`` blob (``POST .../env/import``). Blank lines and ``#``
    comments are ignored, each remaining line is split on the first ``=`` and
    surrounding quotes are stripped; lines whose key is not a valid POSIX name
    are SKIPPED (a bulk paste should not 422 on one stray line)."""

    dotenv: str = Field(default="", max_length=_ENV_VALUE_MAX)


# ---------------------------------------------------------------------------
# Responses (the frozen shapes)
# ---------------------------------------------------------------------------


class BoxOut(BaseModel):
    """One provisioned box. FROZEN — see the module comment."""

    id: str
    provider: str
    ip: str
    status: Literal["provisioning", "ready", "degraded", "destroyed"]
    price_monthly: float | None = None


class DatabaseOut(BaseModel):
    """One linked database on an app. ``env_var`` is the NAME of the variable the
    plugin's link injected (e.g. ``DATABASE_URL``) — the connection string itself
    is a secret and never crosses the wire."""

    name: str
    db_type: str
    env_var: str


class AppOut(BaseModel):
    """One app on a box. FROZEN — see the module comment.

    ``source_kind`` / ``repo_url`` / ``repo_ref`` (SHIP-14) and ``databases`` /
    ``scale`` / ``zero_downtime`` / ``healthcheck_path`` (SHIP-17) are additive
    with defaults. There is deliberately NO token field (the private-repo
    credential is write-only) and no DB connection string (secret, box-only).
    """

    id: str
    name: str
    box_id: str
    status: str
    urls: list[str] = Field(default_factory=list)
    source_kind: Literal["image", "git"] = "image"
    repo_url: str = ""
    repo_ref: str = "main"
    databases: list[DatabaseOut] = Field(default_factory=list)
    scale: dict[str, int] = Field(default_factory=dict)
    zero_downtime: bool = True
    healthcheck_path: str = ""


class DeployOut(BaseModel):
    """One deploy attempt. FROZEN — see the module comment."""

    id: str
    app_id: str
    status: Literal["queued", "building", "releasing", "live", "failed"]
    started_at: datetime | None = None
    finished_at: datetime | None = None


class LogsOut(BaseModel):
    """A chunk of an app's recent log lines, newest last. FROZEN."""

    lines: list[str] = Field(default_factory=list)


class MetricsOut(BaseModel):
    """Box health as three percentages, 0.0–100.0. FROZEN."""

    cpu: float
    mem: float
    disk: float


class AppMetricsOut(BaseModel):
    """One app's health: process state (always present) + real per-container
    resource usage. FROZEN — the /ship console's app Metrics tab consumes this.

    ``cpu``/``mem``/``disk`` are percentages (0.0–100.0) or ``null`` when the box
    could not report them (an old Docker, a down container) — the UI renders "—"
    for a null, never a misleading 0. Process state (``deployed``/``running``/
    ``processes``) always comes back; it is what Dokku itself reports."""

    deployed: bool
    running: bool
    processes: int
    cpu: float | None = None
    mem: float | None = None
    disk: float | None = None


class PendingApprovalOut(BaseModel):
    """The answer to a DELETE: the teardown is PARKED, not performed. FROZEN."""

    status: Literal["pending_approval"] = "pending_approval"
    proposal_id: str


# ---------------------------------------------------------------------------
# Responses (SHIP-1 result mirrors)
# ---------------------------------------------------------------------------


class DomainOut(BaseModel):
    """A domain routed to an app; ``tls_enabled`` reports the cert outcome."""

    domain: str
    tls_enabled: bool


class DomainListOut(BaseModel):
    """The domains currently routed to an app."""

    domains: list[DomainOut] = Field(default_factory=list)


class DbOut(BaseModel):
    """A database service linked to an app.

    ``env_var`` is the NAME of the variable carrying the connection string; the
    string itself never crosses the wire.
    """

    service: str
    linked_app: str
    env_var: str


# ---------------------------------------------------------------------------
# Env (SHIP-9) — FROZEN masked-only shape (see the module comment)
# ---------------------------------------------------------------------------


class EnvVarOut(BaseModel):
    """One env var as the console sees it. ``masked_value`` is ALWAYS a mask —
    never the plaintext. There is deliberately no field that could carry the
    value in the clear."""

    key: str
    masked_value: str
    scope: EnvScope


class EnvOut(BaseModel):
    """An app's env vars, masked. Returned by every env route (GET / PUT /
    import / DELETE) so the caller always sees the resulting state."""

    vars: list[EnvVarOut] = Field(default_factory=list)
