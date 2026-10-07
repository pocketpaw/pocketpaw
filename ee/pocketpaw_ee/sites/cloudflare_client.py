# ee/pocketpaw_ee/sites/cloudflare_client.py: async Cloudflare HTTP API client for
# the Sites control plane. httpx-based, injectable transport for tests; account id
# and token come from settings (env), never from tenant rows. Surfaces:
#   * Workers for Platforms: ``put_worker`` uploads a user Worker into our dispatch
#     namespace (live on 200) and ``delete_worker`` removes it. ``put_worker`` and
#     ``upload_assets`` take a ``target``: ``dispatch`` (default, the namespace) or
#     ``account`` (a regular account-level script at ``/workers/scripts/{name}``,
#     the interim home of ``project`` bundles while the account has no WfP). Only
#     the URL differs; the wire shapes are the same. ``put_worker`` has
#     three wire shapes: the legacy single-module PUT (no bindings), the legacy
#     one-module multipart with bindings (dynamic sites, fixed ``index.mjs`` and
#     ``2024-09-23``), and the multi-module bundle form (``modules=``) used by
#     ``bundle_deploy`` for ``paw-build.json`` builds: metadata + one part per
#     module, with compat flags and an ``assets`` block. The two legacy shapes are
#     byte-for-byte what existing engines have always sent.
#   * Static assets: ``upload_assets`` runs the dispatch assets-upload-session,
#     uploads the requested buckets (base64 multipart, session JWT as Bearer, never
#     the account token) and returns the completion JWT for ``put_worker``. Asset
#     hashes are salted per tenant so one tenant cannot probe another's files in
#     the namespace-shared asset store.
#   * Account-level Workers (the ``workers`` deploy mode): ``delete_account_script``,
#     ``list_account_scripts``, ``enable_workers_dev`` (the script's workers.dev
#     toggle, which an API upload leaves off) and ``workers_dev_subdomain`` (the
#     account's ``<sub>.workers.dev``). Wrangler-built sites deploy through
#     ``workers_deploy.py``, project bundles through ``put_worker(target="account")``.
#     ``Site.deploy_target`` decides which delete applies.
#   * Cloudflare for SaaS: custom hostnames (create, poll, delete). The CNAME target
#     is configured (``PAW_CF_CNAME_TARGET``), never derived from the zone id, and
#     create refuses when it is unset. No ``custom_metadata``: it is entitlement-gated.
#   * Worker routes: ``<hostname>/*`` -> the site's Worker, updated in place on a
#     rename (a second POST for the same pattern is a 409).
#   * D1: ``create_database``, ``find_database`` (exact name, crash recovery),
#     ``delete_database``, ``query_d1`` and ``query_d1_batch`` (parameterized, never
#     interpolated SQL).
#   * KV and R2 for ``binding_provisioner``: namespaces (find by title, create,
#     delete) and buckets (exists, create, delete, expire-all lifecycle). The REST API
#     has no object list/delete for R2, so a non-empty bucket delete reports False
#     instead of raising, and the caller decides what to do with it.
#   * Browser Rendering: ``capture_screenshot`` returns image bytes (url or html).
#   * Analytics Engine: ``query_analytics_sql`` sends raw SQL and reads a body with
#     no ``success`` key, so it deliberately does not use ``_unwrap`` on success.
#
# Invariants: the token lives only in the in-memory Authorization header, never in
# logs or on disk. Every call fails closed: a non-2xx or ``success: false`` raises
# ValidationError carrying Cloudflare's own error codes (``_error_detail``). Deletes
# treat a 404 as already done so a resumed teardown never fails on a finished step.
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus

_CF_API = "https://api.cloudflare.com/client/v4"

# Where ``put_worker`` / ``upload_assets`` send a script. ``dispatch`` is our WfP
# namespace; ``account`` is a regular account-level script (no dispatch isolation).
DISPATCH_TARGET = "dispatch"
ACCOUNT_TARGET = "account"
SCRIPT_TARGETS = (DISPATCH_TARGET, ACCOUNT_TARGET)

# How much of an error body to carry into the raised message. Cloudflare's own
# messages are short sentences; the cap exists so a proxy's HTML error page cannot
# paste a whole document into a toast.
_ERROR_DETAIL_MAX = 300


def _error_detail(resp: httpx.Response) -> str:
    """The reason Cloudflare gave, as one line.

    Cloudflare answers failures with ``{"success": false, "errors": [{code,
    message}, ...]}``. EVERY entry is joined, not just the first: on a refusal the
    second is often the actionable one — the first names the endpoint, the second
    names the missing entitlement. Codes ride along because Cloudflare's docs are
    indexed by them, so a number an operator can paste into a search is worth more
    than the sentence next to it.

    Falls back to the raw body, then to nothing, so a non-JSON reply (an edge HTML
    error page) degrades to the status code the caller already has rather than
    turning a diagnosis into a second exception. The response body is never a
    secret: it is Cloudflare's description of OUR request, and the token only ever
    lives in a request header.
    """
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        text = (resp.text or "").strip()
        return text[:_ERROR_DETAIL_MAX] if text else "no error detail"

    errors = body.get("errors") if isinstance(body, dict) else None
    if not errors:
        return "no error detail"

    parts: list[str] = []
    for err in errors:
        if not isinstance(err, dict):
            parts.append(str(err))
            continue
        code, message = err.get("code"), str(err.get("message", "")).strip()
        parts.append(f"{message} (code {code})" if code and message else message or f"code {code}")
    return "; ".join(p for p in parts if p)[:_ERROR_DETAIL_MAX] or "no error detail"


# Filename the module part is uploaded under (and named by ``main_module`` in the
# metadata). The Workers multipart upload references modules by filename; the
# bundle our generator emits is a single ESM entry, so one ``*.mjs`` part suffices.
_MAIN_MODULE = "index.mjs"

# Workers compatibility date for the multipart upload's metadata. Matches the
# date the generator bakes into the (otherwise-ignored-on-direct-API-upload)
# wrangler.toml so the runtime semantics are identical to a wrangler deploy.
_COMPATIBILITY_DATE = "2024-09-23"

# Multi-module uploads. Content-Type tells the runtime how to load each part:
# https://developers.cloudflare.com/workers/configuration/multipart-upload-metadata/
_MODULE_CONTENT_TYPES: dict[str, str] = {
    ".js": "application/javascript+module",
    ".mjs": "application/javascript+module",
    ".cjs": "application/javascript",
    ".wasm": "application/wasm",
    ".json": "application/json",
    ".txt": "text/plain",
    ".html": "text/plain",
    ".md": "text/plain",
    ".sql": "text/plain",
}


def module_content_type(name: str) -> str:
    """The part Content-Type for a Worker module, by extension. Anything unknown is
    uploaded as data (``application/octet-stream``), which imports as an ArrayBuffer."""
    ext = posixpath.splitext(name)[1].lower()
    return _MODULE_CONTENT_TYPES.get(ext, "application/octet-stream")


@dataclass(frozen=True)
class WorkerModule:
    """One module part of a multi-module upload. ``name`` is the part name and the
    specifier other modules import it by: a relative posix path (``chunks/a.js``)."""

    name: str
    content: bytes
    content_type: str


def asset_hash(content: bytes, path: str, salt: str) -> str:
    """The 32-hex asset hash Cloudflare keys uploads on, salted per tenant.

    Assets in a dispatch namespace are deduplicated by this hash across every script
    in it, so an unsalted content hash would let one tenant learn whether another
    already uploaded a given file. Salting with the tenant key keeps dedupe inside the
    tenant and makes cross-tenant probing impossible (the salt is applied here, on our
    side, never by the author). The extension is mixed in, as wrangler does, because
    the same bytes served as ``.js`` and ``.txt`` are different assets."""
    ext = posixpath.splitext(path)[1].lower()
    h = hashlib.sha256()
    h.update(salt.encode("utf-8"))
    h.update(b"\0")
    h.update(content)
    h.update(b"\0")
    h.update(ext.encode("utf-8"))
    return h.hexdigest()[:32]


def _asset_part_type(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


# BC-10: the basic (no-feature) custom-hostname SSL payload — the prior default,
# kept byte-for-byte so a base-tier site never regresses.
_BASIC_SSL: dict = {"method": "http", "type": "dv"}

# BC-10: map a single resold feature → the custom-hostname ``ssl.settings`` fields
# it provisions. A site's plan tier resolves to a SET of feature keys (from
# ``billing.site_plans``); the union of these per-feature fragments becomes the
# ``ssl.settings`` block on the create request. Intentionally MINIMAL — a real CF
# account tunes the exact toggles; the contract is the feature → field mapping,
# not the specific security values. Features not in this map (e.g. analytics,
# custom_domain) provision NO ssl.settings field — they're recorded on
# ``custom_metadata`` so the tier is still visible on the hostname.
_FEATURE_SSL_SETTINGS: dict[str, dict] = {
    # WAF / managed security: opt the hostname into strict TLS so the resold
    # WAF rules sit behind a hardened handshake.
    "waf": {"min_tls_version": "1.2", "tls_1_3": "on"},
    # Edge cache controls: enable HTTP/2 + early-hints-friendly negotiation on
    # the resold edge-cache tier.
    "edge_cache": {"http2": "on"},
}


def _ssl_for_features(features: set[str] | None) -> dict:
    """Build the custom-hostname ``ssl`` payload for a tier's resold features.

    No features (base tier) → the basic ``{"method": "http", "type": "dv"}`` payload.
    With features, it gains an ``ssl.settings`` block that is the UNION of every known
    feature's fragment (``_FEATURE_SSL_SETTINGS``). A feature with no fragment
    contributes nothing, which is the honest answer: nothing about the hostname
    changes for it.

    **No ``custom_metadata``.** BC-10 recorded the resold feature set there so a tier
    was auditable on Cloudflare's side. Per-hostname metadata is not generally
    available — Cloudflare's own doc says "only certain customers have access to this
    feature… contact your account team" — so on an ordinary zone that block turned the
    create into a 403/1413. The effect was that custom domains worked on FREE sites and
    failed on PAID ones. The 2026-06-25 resale research had already marked these SKUs
    DEFER; this restores that decision. The tier is recorded on the Site document,
    which is where anything in this codebase actually reads it from.
    """
    if not features:
        return dict(_BASIC_SSL)
    settings: dict = {}
    for feat in features:
        settings.update(_FEATURE_SSL_SETTINGS.get(feat, {}))
    ssl: dict = dict(_BASIC_SSL)
    if settings:
        ssl["settings"] = settings
    return ssl


def _map_status(cf_status: str, ssl_status: str) -> HostnameStatus:
    if cf_status == "active" and ssl_status == "active":
        return HostnameStatus.LIVE
    if cf_status in {"pending", "pending_deletion"}:
        return HostnameStatus.PENDING
    if cf_status in {"active"} or ssl_status in {"pending_validation", "initializing"}:
        return HostnameStatus.VERIFYING
    return HostnameStatus.ERROR


class CloudflareClient:
    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        zone_id: str,
        dispatch_namespace: str,
        cname_target: str = "",
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._account_id = account_id
        self._zone_id = zone_id
        self._namespace = dispatch_namespace
        # The name a customer pastes into their own registrar. MUST be a proxied
        # record on our zone (see the module header); defaults empty so a caller that
        # has not configured one fails loudly at create time rather than silently
        # handing out something that does not resolve.
        self._cname_target = cname_target.strip()
        self._headers = {"Authorization": f"Bearer {api_token}"}
        self._transport = _transport  # tests inject a MockTransport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, transport=self._transport, timeout=30.0)

    @staticmethod
    def _unwrap(resp: httpx.Response) -> dict:
        if resp.status_code // 100 != 2:
            # Cloudflare names the reason in the body on a refusal, and this branch
            # used to drop it: it raised the bare status and never looked, while the
            # body-reading branch below can only run on a 2xx. So the one useful
            # sentence was discarded on exactly the responses that needed it — a 403
            # on custom_hostnames reached the operator as "Cloudflare API 403", which
            # is true of a token missing the SSL-and-Certificates edit scope, a token
            # with no access to the zone, and a zone without Cloudflare for SaaS,
            # three problems with three different fixes.
            raise ValidationError(
                "sites.cloudflare_error",
                f"Cloudflare API {resp.status_code}: {_error_detail(resp)}",
            )
        body = resp.json()
        if not body.get("success", False):
            raise ValidationError("sites.cloudflare_error", _error_detail(resp))
        return body.get("result", {})

    async def put_worker(
        self,
        *,
        script_name: str,
        bundle: bytes = b"",
        bindings: list[dict] | None = None,
        modules: Sequence[WorkerModule] | None = None,
        main_module: str | None = None,
        compatibility_date: str | None = None,
        compatibility_flags: Sequence[str] | None = None,
        assets: dict | None = None,
        target: str = DISPATCH_TARGET,
    ) -> bool:
        """Upload a user Worker into the dispatch namespace. Live on 200.

        ``target="account"`` sends the same upload to a regular account-level script
        (``PUT /accounts/{id}/workers/scripts/{name}``) instead, see ``_script_url``.

        ``bindings`` (DS-2) carries the Worker's runtime bindings — for a dynamic
        Paw Site, a D1 binding ``{"type": "d1", "name": "DB", "id": <database_id>}``
        so the deployed Worker can reach its per-tenant D1. Each binding is a dict
        passed straight into the upload metadata's ``bindings`` array (the CF
        Workers contract), so future binding types (queues, KV, ...) ride the same
        param without a signature change.

        When ``bindings`` is None/empty (a STATIC site), the upload is the prior
        single-module PUT — ``content=bundle`` with a
        ``application/javascript+module`` Content-Type, byte-for-byte unchanged, so
        the static path never regresses. When bindings are supplied, the upload
        switches to the Workers multipart/form-data contract: a ``metadata`` JSON
        part naming ``main_module`` + the ``bindings`` array + ``compatibility_date``,
        plus the module file part referenced by that filename. The
        dispatch-namespace script upload uses the same multipart contract as a
        normal Worker upload (metadata part named ``metadata``, module parts keyed
        by filename).

        ``modules`` selects the bundle form instead (``paw-build.json`` deploys):
        ``bundle`` must then be empty, and the metadata carries ``main_module``,
        ``bindings``, ``compatibility_date``, ``compatibility_flags`` and, when given,
        ``assets`` (``{"jwt": <completion jwt>, "config": {...}}``), followed by one
        part per module named by its path. An empty ``modules`` with ``assets`` is an
        assets-only Worker (no ``main_module``). Callers vet every value first; this
        method only checks the shape is self-consistent."""
        if modules is not None:
            return await self._put_worker_bundle(
                script_name=script_name,
                bundle=bundle,
                modules=modules,
                main_module=main_module,
                bindings=bindings or [],
                compatibility_date=compatibility_date,
                compatibility_flags=compatibility_flags or [],
                assets=assets,
                target=target,
            )
        url = self._script_url(script_name, target)
        async with self._client() as client:
            if bindings:
                metadata = {
                    "main_module": _MAIN_MODULE,
                    "bindings": list(bindings),
                    "compatibility_date": _COMPATIBILITY_DATE,
                }
                # httpx builds the multipart/form-data body + boundary. The
                # ``metadata`` part is a JSON blob; the module part is named by its
                # filename (== main_module) and typed as an ES module so CF treats
                # it as the entrypoint, not a plain asset.
                files = {
                    "metadata": (None, json.dumps(metadata), "application/json"),
                    _MAIN_MODULE: (
                        _MAIN_MODULE,
                        bundle,
                        "application/javascript+module",
                    ),
                }
                resp = await client.put(url, files=files)
            else:
                resp = await client.put(
                    url,
                    content=bundle,
                    headers={"Content-Type": "application/javascript+module"},
                )
        self._unwrap(resp)
        return True

    def _script_url(self, script_name: str, target: str = DISPATCH_TARGET) -> str:
        """The script's API URL for ``target``. ``account`` is the regular Worker
        upload (https://developers.cloudflare.com/api/resources/workers/subresources/scripts/methods/update/),
        ``dispatch`` the same contract inside our WfP namespace."""
        if target == ACCOUNT_TARGET:
            return f"{_CF_API}/accounts/{self._account_id}/workers/scripts/{script_name}"
        if target != DISPATCH_TARGET:
            raise ValidationError("sites.bundle_shape", f"unknown script target {target!r}")
        return (
            f"{_CF_API}/accounts/{self._account_id}"
            f"/workers/dispatch/namespaces/{self._namespace}/scripts/{script_name}"
        )

    async def _put_worker_bundle(
        self,
        *,
        script_name: str,
        bundle: bytes,
        modules: Sequence[WorkerModule],
        main_module: str | None,
        bindings: list[dict],
        compatibility_date: str | None,
        compatibility_flags: Sequence[str],
        assets: dict | None,
        target: str = DISPATCH_TARGET,
    ) -> bool:
        if bundle:
            raise ValidationError(
                "sites.bundle_shape", "put_worker takes either bundle or modules, not both"
            )
        if not compatibility_date:
            raise ValidationError(
                "sites.bundle_shape", "a bundle upload needs a compatibility date"
            )
        names = [m.name for m in modules]
        if len(set(names)) != len(names):
            raise ValidationError("sites.bundle_shape", "two modules share a part name")
        if modules and main_module not in names:
            raise ValidationError(
                "sites.bundle_shape", f"main module {main_module!r} is not one of the modules"
            )
        if not modules and not assets:
            raise ValidationError("sites.bundle_shape", "a bundle needs modules or assets")

        metadata: dict = {}
        if modules:
            metadata["main_module"] = main_module
        metadata["bindings"] = list(bindings)
        metadata["compatibility_date"] = compatibility_date
        metadata["compatibility_flags"] = list(compatibility_flags)
        if assets:
            metadata["assets"] = assets
        files: list[tuple[str, tuple[str | None, bytes | str, str]]] = [
            ("metadata", (None, json.dumps(metadata), "application/json"))
        ]
        files.extend((m.name, (m.name, m.content, m.content_type)) for m in modules)
        async with self._client() as client:
            resp = await client.put(self._script_url(script_name, target), files=files)
        self._unwrap(resp)
        return True

    async def upload_assets(
        self,
        *,
        script_name: str,
        assets: Mapping[str, bytes],
        salt: str,
        target: str = DISPATCH_TARGET,
    ) -> str:
        """Upload a script's static assets and return the completion JWT.

        ``target`` picks the session URL the same way ``put_worker`` does. The
        account-level flow is the same three steps
        (https://developers.cloudflare.com/workers/static-assets/direct-upload/).

        ``assets`` maps the served path (``/index.html``) to its bytes. The three
        steps are Cloudflare's documented WfP static-assets flow
        (https://developers.cloudflare.com/cloudflare-for-platforms/workers-for-platforms/configuration/static-assets/):

        1. POST ``.../scripts/{name}/assets-upload-session`` with the manifest
           ``{path: {hash, size}}``. Cloudflare answers with a session JWT and the
           ``buckets`` of hashes it does not already hold.
        2. POST each bucket to ``/workers/assets/upload?base64=true`` as multipart,
           one base64 part per hash, authorised by the SESSION JWT (the account token
           is deliberately not sent there).
        3. The last bucket's response carries the completion JWT. When nothing needs
           uploading the session JWT already is the completion JWT.

        The JWTs live about an hour, so call this right before ``put_worker``."""
        if not salt:
            raise ValidationError("sites.assets_unsalted", "asset uploads need a tenant salt")
        if not assets:
            raise ValidationError("sites.bundle_shape", "no assets to upload")
        manifest: dict[str, dict] = {}
        by_hash: dict[str, tuple[str, bytes]] = {}
        for raw_path, content in assets.items():
            path = "/" + raw_path.lstrip("/")
            digest = asset_hash(content, path, salt)
            manifest[path] = {"hash": digest, "size": len(content)}
            by_hash.setdefault(digest, (path, content))

        upload_url = f"{_CF_API}/accounts/{self._account_id}/workers/assets/upload?base64=true"
        async with self._client() as client:
            session_resp = await client.post(
                f"{self._script_url(script_name, target)}/assets-upload-session",
                json={"manifest": manifest},
            )
            session = self._unwrap(session_resp) or {}
            session_jwt = session.get("jwt")
            if not session_jwt:
                raise ValidationError(
                    "sites.cloudflare_error", "assets upload session returned no token"
                )
            buckets = [b for b in session.get("buckets") or [] if b]
            completion = "" if buckets else session_jwt
            for bucket in buckets:
                files = []
                for digest in bucket:
                    if digest not in by_hash:
                        raise ValidationError(
                            "sites.cloudflare_error",
                            f"Cloudflare asked for an asset hash we never sent ({digest})",
                        )
                    path, content = by_hash[digest]
                    files.append(
                        (digest, (digest, base64.b64encode(content), _asset_part_type(path)))
                    )
                upload_resp = await client.post(
                    upload_url,
                    files=files,
                    headers={"Authorization": f"Bearer {session_jwt}"},
                )
                result = self._unwrap(upload_resp) or {}
                completion = result.get("jwt") or completion
        if not completion:
            raise ValidationError(
                "sites.cloudflare_error", "assets upload finished without a completion token"
            )
        return completion

    async def delete_worker(self, script_name: str) -> None:
        """Remove a user Worker from the dispatch namespace. Idempotent on a 404.

        The inverse of ``put_worker``, and the step in a site teardown that actually
        stops the page being served: the custom hostname and its route only decide
        HOW a request reaches this script, so removing them leaves the site reachable
        on its dispatch address. Nothing deleted a Worker before this existed, which
        is why a site published under ``wfp`` kept serving after every other trace of
        it was gone.

        A 404 is SUCCESS, for the same reason it is in ``delete_custom_hostname``:
        the goal is "this script is not in the namespace", and something already gone
        satisfies it. Raising there would make a resumed teardown fail on the step it
        had already completed — turning a recoverable partial teardown into a
        permanent orphan, which is precisely what this method exists to prevent."""
        url = (
            f"{_CF_API}/accounts/{self._account_id}"
            f"/workers/dispatch/namespaces/{self._namespace}/scripts/{script_name}"
        )
        async with self._client() as client:
            resp = await client.delete(url)
        if resp.status_code == 404:
            return
        self._unwrap(resp)

    async def delete_account_script(self, script_name: str) -> None:
        """Remove an ACCOUNT-LEVEL Worker script (the ``workers`` deploy mode).

        Sibling of ``delete_worker``, and the difference is the deploy mode, not the
        caller's preference. ``wfp`` sites live in the dispatch namespace; ``workers``
        sites were deployed by a ``bunx wrangler deploy`` subprocess to an
        account-level script at a different API path. Read ``Site.deploy_target`` to
        choose — it records what the last successful deploy actually did, and its own
        field comment lists the several ways the configured mode and the deployed
        reality drift apart.

        Deliberately NOT implemented as ``wrangler delete``. The subprocess would need
        a project directory that teardown does not have (the artifact may already be
        purged), and a subprocess seam can only be honestly proven against the real
        binary. One HTTP call has neither problem.

        Idempotent on a 404, same reasoning as every other delete here."""
        url = f"{_CF_API}/accounts/{self._account_id}/workers/scripts/{script_name}"
        async with self._client() as client:
            resp = await client.delete(url)
        if resp.status_code == 404:
            return
        self._unwrap(resp)

    async def enable_workers_dev(self, script_name: str) -> None:
        """Serve an ACCOUNT-LEVEL script on ``<script>.<sub>.workers.dev``.

        ``POST /accounts/{id}/workers/scripts/{name}/subdomain`` with
        ``{"enabled": true, "previews_enabled": false}``
        (https://developers.cloudflare.com/api/resources/workers/subresources/scripts/subresources/subdomain/methods/create/).
        Wrangler does this for ``workers_dev: true``; a script uploaded through the API
        needs it said explicitly. Idempotent, so every publish sends it."""
        url = f"{_CF_API}/accounts/{self._account_id}/workers/scripts/{script_name}/subdomain"
        async with self._client() as client:
            resp = await client.post(url, json={"enabled": True, "previews_enabled": False})
        self._unwrap(resp)

    async def workers_dev_subdomain(self) -> str:
        """The account's workers.dev subdomain (the ``<sub>`` in
        ``<script>.<sub>.workers.dev``), or ``""`` when the account has none.

        ``GET /accounts/{id}/workers/subdomain``
        (https://developers.cloudflare.com/api/resources/workers/subresources/subdomains/methods/get/)."""
        url = f"{_CF_API}/accounts/{self._account_id}/workers/subdomain"
        async with self._client() as client:
            resp = await client.get(url)
        result = self._unwrap(resp)
        return str(result.get("subdomain") or "") if isinstance(result, dict) else ""

    async def list_account_scripts(self) -> list[str]:
        """Names of every ACCOUNT-LEVEL Worker script (the ``workers`` deploy mode).

        ``GET /accounts/{id}/workers/scripts``. Each result's ``id`` is the script name,
        which on workers.dev is also the subdomain. Read before a new site claims a
        Worker name, so it never lands on a script it does not own. Fails closed like
        every other call here (a non-2xx raises); the caller decides to fail open."""
        url = f"{_CF_API}/accounts/{self._account_id}/workers/scripts"
        async with self._client() as client:
            resp = await client.get(url)
        result = self._unwrap(resp)
        rows = result if isinstance(result, list) else []
        return [str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")]

    async def create_custom_hostname(
        self, hostname: str, *, features: set[str] | None = None
    ) -> CustomHostname:
        """Register a Cloudflare-for-SaaS custom hostname, return its single CNAME.

        ``features`` (BC-10) is the site plan tier's ``cloudflare_features`` set.
        When present, the create request carries the premium ``ssl`` payload built
        by ``_ssl_for_features`` (strict-TLS / HTTP-2 toggles for WAF / edge-cache).
        When None/empty (the BASE tier), the request is the basic DV ``ssl`` payload.

        **Refuses without a configured CNAME target.** The returned ``cname_target``
        is the single instruction the customer acts on, and this used to construct
        ``{zone_id}.cdn.cloudflare.net`` — a name with no DNS records at all. A
        hostname created against a dead target can never validate, and nothing
        downstream can tell the customer why, so an unconfigured target is refused
        here instead of producing a hostname that is guaranteed to fail. Cloudflare
        wants a proxied record on our own zone; only the operator knows which one.
        """
        if not self._cname_target:
            raise ValidationError(
                "sites.cloudflare_unconfigured",
                "Custom domains need a CNAME target configured (PAW_CF_CNAME_TARGET) "
                "— a proxied hostname on the Paw zone for customers to point at.",
            )
        url = f"{_CF_API}/zones/{self._zone_id}/custom_hostnames"
        async with self._client() as client:
            resp = await client.post(
                url,
                json={"hostname": hostname, "ssl": _ssl_for_features(features)},
            )
        result = self._unwrap(resp)
        return CustomHostname(
            id=result["id"],
            hostname=result["hostname"],
            status=_map_status(
                result.get("status", ""), (result.get("ssl") or {}).get("status", "")
            ),
            cname_target=self._cname_target,
        )

    async def get_hostname_status(self, hostname_id: str) -> HostnameStatus:
        url = f"{_CF_API}/zones/{self._zone_id}/custom_hostnames/{hostname_id}"
        async with self._client() as client:
            resp = await client.get(url)
        result = self._unwrap(resp)
        return _map_status(result.get("status", ""), (result.get("ssl") or {}).get("status", ""))

    async def delete_custom_hostname(self, hostname_id: str) -> None:
        """Remove a custom hostname from the zone. Idempotent on a 404.

        There was no delete path at all before this, which is why a removed site left
        its hostnames on the zone counting against quota and pointing at a Worker that
        no longer existed. A 404 is treated as SUCCESS: the goal is "this hostname is
        not on the zone", and something already gone satisfies it. Raising there would
        make a half-finished teardown permanently un-finishable, which is the exact
        orphan-accumulation this method exists to stop."""
        url = f"{_CF_API}/zones/{self._zone_id}/custom_hostnames/{hostname_id}"
        async with self._client() as client:
            resp = await client.delete(url)
        if resp.status_code == 404:
            return
        self._unwrap(resp)

    async def create_worker_route(self, *, pattern: str, script: str) -> str:
        """Bind a URL pattern on our zone to a Worker, and return the route id.

        This is what makes a custom hostname reach a particular site. Cloudflare for
        SaaS gets the request into our zone; a route scoped to that exact hostname
        (``myportfolio.com/*``) is what decides which Worker answers it. One route per
        custom domain, written by the control plane at add time — see the module
        header for why there is no dispatcher.

        ``POST`` creates; ``PUT`` on this collection is update-by-id and needs a route
        id we do not have yet. The zone allows 1,000 routes, which is far past the
        per-account Worker limit this deploy mode runs into first.

        The id is read with ``result["id"]``, NOT ``.get("id", "")``. A response with no
        id would otherwise return "" while the route exists on the zone: the caller
        stores an empty ``cf_route_id``, reports success, and teardown — which deletes
        BY id — can never remove it. That is the orphan the id is stored to prevent, so
        an id-less success is a failure and must raise like one. ``create_custom_hostname``
        already indexes its id directly; this now matches."""
        url = f"{_CF_API}/zones/{self._zone_id}/workers/routes"
        async with self._client() as client:
            resp = await client.post(url, json={"pattern": pattern, "script": script})
        result = self._unwrap(resp)
        try:
            return result["id"]
        except (KeyError, TypeError) as exc:
            raise ValidationError(
                "sites.cloudflare_error",
                "Cloudflare accepted the Worker route but returned no id — it cannot "
                "be recorded, and an unrecorded route cannot be removed later.",
            ) from exc

    async def update_worker_route(self, route_id: str, *, pattern: str, script: str) -> None:
        """Point an existing Worker route at ``script``, keeping its id and pattern.

        ``PUT /zones/{zone}/workers/routes/{id}``. The rename apply step uses it to move
        a custom domain from a site's old Worker to its new one. A second ``POST`` with
        the same pattern is a 10020 duplicate-route error, and delete-then-create would
        leave the domain serving nothing in between. Fails closed like every call here:
        a non-2xx (including a 404 for a route that is gone) raises, because the caller
        has to know which routes actually moved in order to roll them back."""
        url = f"{_CF_API}/zones/{self._zone_id}/workers/routes/{route_id}"
        async with self._client() as client:
            resp = await client.put(url, json={"pattern": pattern, "script": script})
        self._unwrap(resp)

    async def delete_worker_route(self, route_id: str) -> None:
        """Remove a Worker route. Idempotent on a 404, for the same reason
        ``delete_custom_hostname`` is: a teardown that cannot complete leaves exactly
        the orphan it was called to remove."""
        url = f"{_CF_API}/zones/{self._zone_id}/workers/routes/{route_id}"
        async with self._client() as client:
            resp = await client.delete(url)
        if resp.status_code == 404:
            return
        self._unwrap(resp)

    async def create_database(self, name: str) -> str:
        """Create a Cloudflare D1 database and return its real uuid (DP0-1).

        POSTs to the D1 create endpoint (POST /accounts/{acct}/d1/database) with a
        ``{"name": <name>}`` body. Cloudflare returns the new database in the
        standard envelope; the D1 uuid is at ``result.uuid``. This returns that
        uuid string — the id every later step (migrate, the Worker's D1 binding,
        the generated wrangler.toml ``database_id``) keys on.

        Fail-closed: a non-2xx or a ``success: false`` envelope raises
        ValidationError via ``_unwrap``, so a failed create never silently returns
        an empty/garbage id."""
        url = f"{_CF_API}/accounts/{self._account_id}/d1/database"
        async with self._client() as client:
            resp = await client.post(url, json={"name": name})
        result = self._unwrap(resp)
        return result["uuid"]

    async def delete_database(self, database_id: str) -> None:
        """Destroy a per-tenant D1 database. Idempotent on a 404. IRREVERSIBLE.

        The inverse of ``create_database``, and the only step in a site teardown that
        destroys CUSTOMER DATA rather than infrastructure — a dynamic site's D1 holds
        its bookings, submissions and orders alongside ``_paw_migrations`` /
        ``_paw_handoffs`` / ``_paw_outbox``. There is no Cloudflare-side undelete and
        no retention window, so the export that the delete cascade takes BEFORE
        reaching this step is the only copy that survives it. Do not call this outside
        that cascade, and do not reorder it ahead of the export.

        Keyed on the database uuid rather than the name because the uuid is what
        ``Site.d1_database_id`` stores and what the Worker binding points at; a name
        lookup would be a second way to identify the same database, and the two can
        disagree once a site is renamed.

        A 404 is SUCCESS — the database is gone, which is the goal. A resumed teardown
        re-running this step must not fail on it."""
        url = f"{_CF_API}/accounts/{self._account_id}/d1/database/{database_id}"
        async with self._client() as client:
            resp = await client.delete(url)
        if resp.status_code == 404:
            return
        self._unwrap(resp)

    async def find_database(self, name: str) -> str | None:
        """The uuid of the D1 database named exactly ``name``, or None.

        The provisioner's crash-recovery read, like ``find_kv_namespace``: a database
        created on a publish that died before the Site doc was saved is found here
        instead of created twice. ``name`` on the list endpoint is a search, so rows
        are matched exactly; the loop is bounded."""
        url = f"{_CF_API}/accounts/{self._account_id}/d1/database"
        async with self._client() as client:
            for page in range(1, 51):
                resp = await client.get(url, params={"name": name, "page": page, "per_page": 100})
                rows = self._unwrap(resp)
                rows = rows if isinstance(rows, list) else []
                for row in rows:
                    if isinstance(row, dict) and row.get("name") == name and row.get("uuid"):
                        return str(row["uuid"])
                if len(rows) < 100:
                    return None
        return None

    # -- KV namespaces (binding_provisioner) ----------------------------------
    # https://developers.cloudflare.com/api/resources/kv/subresources/namespaces/

    async def find_kv_namespace(self, title: str) -> str | None:
        """The id of the namespace titled ``title``, or None.

        The provisioner's crash-recovery read: a namespace created on an attempt that
        died before the site doc was saved is found here instead of duplicated.
        ``per_page`` max is 1000 and an account holds at most 1,000 namespaces, so
        one page normally covers it; the loop is bounded anyway."""
        url = f"{_CF_API}/accounts/{self._account_id}/storage/kv/namespaces"
        async with self._client() as client:
            for page in range(1, 51):
                resp = await client.get(url, params={"page": page, "per_page": 1000})
                rows = self._unwrap(resp)
                rows = rows if isinstance(rows, list) else []
                for row in rows:
                    if isinstance(row, dict) and row.get("title") == title and row.get("id"):
                        return str(row["id"])
                if len(rows) < 1000:
                    break
        return None

    async def create_kv_namespace(self, title: str) -> str:
        """Create a KV namespace and return its id. Fails closed like every create."""
        url = f"{_CF_API}/accounts/{self._account_id}/storage/kv/namespaces"
        async with self._client() as client:
            resp = await client.post(url, json={"title": title})
        return str(self._unwrap(resp)["id"])

    async def delete_kv_namespace(self, namespace_id: str) -> None:
        """Delete a KV namespace and its data. Idempotent on a 404."""
        url = f"{_CF_API}/accounts/{self._account_id}/storage/kv/namespaces/{namespace_id}"
        async with self._client() as client:
            resp = await client.delete(url)
        if resp.status_code == 404:
            return
        self._unwrap(resp)

    # -- R2 buckets (binding_provisioner) -------------------------------------
    # https://developers.cloudflare.com/api/resources/r2/subresources/buckets/

    def _bucket_url(self, name: str) -> str:
        return f"{_CF_API}/accounts/{self._account_id}/r2/buckets/{name}"

    async def r2_bucket_exists(self, name: str) -> bool:
        """True when the bucket is in our account (GET bucket), False on a 404."""
        async with self._client() as client:
            resp = await client.get(self._bucket_url(name))
        if resp.status_code == 404:
            return False
        self._unwrap(resp)
        return True

    async def create_r2_bucket(self, name: str) -> str:
        """Create an R2 bucket and return its name."""
        url = f"{_CF_API}/accounts/{self._account_id}/r2/buckets"
        async with self._client() as client:
            resp = await client.post(url, json={"name": name})
        result = self._unwrap(resp)
        return str(result.get("name") or name) if isinstance(result, dict) else name

    async def delete_r2_bucket(self, name: str) -> bool:
        """Delete an R2 bucket. True when it is gone (a 404 included), False when
        Cloudflare refused because it still holds objects.

        Cloudflare only deletes an empty bucket, and its REST API has no object
        list or delete (that is the S3 API, a different credential), so this cannot
        empty one itself. A 409 is the not-empty answer; any other failure raises."""
        async with self._client() as client:
            resp = await client.delete(self._bucket_url(name))
        if resp.status_code == 404:
            return True
        if resp.status_code == 409:
            return False
        self._unwrap(resp)
        return True

    async def expire_r2_bucket_objects(self, name: str, *, max_age_seconds: int = 86400) -> None:
        """Replace the bucket's lifecycle rules with one that deletes every object
        (and aborts every multipart upload) older than ``max_age_seconds``, so a
        bucket that refused deletion empties itself and a later delete succeeds."""
        age = {"type": "Age", "maxAge": max_age_seconds}
        body = {
            "rules": [
                {
                    "id": "paw-teardown-expire-all",
                    "enabled": True,
                    "conditions": {"prefix": ""},
                    "deleteObjectsTransition": {"condition": age},
                    "abortMultipartUploadsTransition": {"condition": age},
                }
            ]
        }
        async with self._client() as client:
            resp = await client.put(f"{self._bucket_url(name)}/lifecycle", json=body)
        self._unwrap(resp)

    async def capture_screenshot(
        self,
        *,
        url: str = "",
        html: str = "",
        viewport: dict | None = None,
        goto_options: dict | None = None,
        screenshot_options: dict | None = None,
    ) -> bytes:
        """Screenshot a page and return the raw image bytes (SC-1, SC-2).

        POSTs to the Browser Rendering screenshot endpoint
        (POST /accounts/{acct}/browser-rendering/screenshot). The endpoint takes
        EITHER ``url`` (render the page at that address — a deployed site, SC-1) or
        ``html`` (render this markup directly — a DRAFT, which by definition has no
        address, SC-2); exactly one is required, and passing both or neither is a
        caller bug, so it raises here rather than letting Cloudflare answer 400.

        An ``html`` body renders at ``about:blank``, so NOTHING relative in it
        resolves — the markup has to arrive self-contained (see
        ``sites.draft_markup``). The three option dicts ride through untouched as
        ``viewport`` (width / height / deviceScaleFactor), ``gotoOptions``
        (waitUntil / timeout) and ``screenshotOptions`` (fullPage / type / ...).
        Omitted options are left off the body entirely so Cloudflare's own
        defaults apply (a 1920x1080 viewport, a full-quality png).

        ``screenshot_options`` is passed through rather than assembled here on
        purpose — but note the one combination Cloudflare rejects: ``quality`` is
        incompatible with the DEFAULT png and returns 400. A caller that wants
        ``quality`` must also set ``type`` to ``"jpeg"`` or ``"webp"``.

        Unlike every other method here the SUCCESS path is not the JSON envelope:
        a rendered screenshot comes back as the image itself, so a 2xx with an
        ``image/*`` content type returns ``resp.content`` directly. Everything
        else fails closed — a non-2xx goes through the shared ``_unwrap`` (the
        standard ValidationError), and a 2xx that is not an image (an error
        envelope, an empty body) raises too, so an HTML error page can never be
        persisted as a site's preview image."""
        if bool(url) == bool(html):
            raise ValidationError(
                "sites.cloudflare_error",
                "Browser Rendering needs exactly one of url or html.",
            )
        api_url = f"{_CF_API}/accounts/{self._account_id}/browser-rendering/screenshot"
        payload: dict = {"url": url} if url else {"html": html}
        if screenshot_options:
            payload["screenshotOptions"] = screenshot_options
        if viewport:
            payload["viewport"] = viewport
        if goto_options:
            payload["gotoOptions"] = goto_options
        async with self._client() as client:
            resp = await client.post(api_url, json=payload)
        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if resp.status_code // 100 == 2 and content_type.startswith("image/") and resp.content:
            return resp.content
        if resp.status_code // 100 != 2:
            # Non-2xx: the shared envelope check raises the standard error. It
            # never reaches ``resp.json()`` on this branch, so a non-JSON error
            # page still surfaces as a clean ValidationError.
            self._unwrap(resp)
        raise ValidationError(
            "sites.cloudflare_error",
            f"Browser Rendering returned no image (content-type {content_type or 'unknown'!r})",
        )

    async def query_d1(
        self, *, database_id: str, sql: str, params: list | None = None
    ) -> list[dict]:
        """Run ONE parameterized SQL statement against a D1 database and return its
        rows (DS-3 — the control-plane read of a dynamic site's data).

        POSTs to the Cloudflare D1 query endpoint with a ``{sql, params}`` body.
        The D1 query response wraps each statement's output in a ``result`` ARRAY
        (one element per statement; a single ``sql`` returns one element), each
        carrying its own ``results`` rows + ``meta``. This sends a single
        statement and returns the FIRST element's ``results`` (the rows) as a list
        of dicts — empty when the table has no rows.

        SQL safety: this method NEVER builds SQL itself. The caller (the service)
        passes a fully-formed statement whose table identifier it has already
        validated against the site's declared ``objects`` (an unknown table is
        rejected BEFORE this is reached); every value rides ``params`` as a bound
        placeholder, never string-interpolated. ``params`` defaults to an empty
        list so a value-less listing query is sent cleanly.

        Fail-closed: a non-2xx, a ``success: false`` envelope, or a D1
        statement-level failure raises ValidationError via ``_unwrap`` /
        the per-statement ``success`` check, so a failed read never silently
        reports empty rows."""
        url = f"{_CF_API}/accounts/{self._account_id}/d1/database/{database_id}/query"
        async with self._client() as client:
            resp = await client.post(url, json={"sql": sql, "params": params or []})
        # ``_unwrap`` checks the outer envelope (HTTP status + top-level success);
        # for a query the ``result`` is an ARRAY of per-statement outcomes.
        result = self._unwrap(resp)
        statements = result if isinstance(result, list) else []
        if not statements:
            return []
        first = statements[0] if isinstance(statements[0], dict) else {}
        # A per-statement failure (e.g. malformed SQL) sets success=false on the
        # element even when the HTTP envelope is 200 — fail closed on it too.
        if first.get("success") is False:
            raise ValidationError("sites.cloudflare_error", "D1 query statement failed")
        rows = first.get("results")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    async def query_d1_batch(
        self, *, database_id: str, statements: Sequence[tuple[str, list]]
    ) -> list[list[dict]]:
        """Run several parameterized statements as ONE D1 batch and return each
        statement's rows, in order.

        Uses the query endpoint's ``batch`` body (``[{sql, params}, ...]``) so a
        project migration and the row that records it travel together. Same SQL
        rules as ``query_d1``: this never builds SQL, values ride ``params``.

        Fail-closed: a non-2xx or ``success: false`` envelope raises through
        ``_unwrap`` (with Cloudflare's own reason), and so does any statement that
        reports ``success: false``, naming its position in the batch."""
        url = f"{_CF_API}/accounts/{self._account_id}/d1/database/{database_id}/query"
        body = {"batch": [{"sql": sql, "params": list(params or [])} for sql, params in statements]}
        async with self._client() as client:
            resp = await client.post(url, json=body)
        result = self._unwrap(resp)
        outcomes = result if isinstance(result, list) else []
        rows: list[list[dict]] = []
        for index, outcome in enumerate(outcomes):
            outcome = outcome if isinstance(outcome, dict) else {}
            if outcome.get("success") is False:
                raise ValidationError(
                    "sites.cloudflare_error",
                    f"D1 batch statement {index + 1} of {len(statements)} failed",
                )
            found = outcome.get("results")
            found = found if isinstance(found, list) else []
            rows.append([r for r in found if isinstance(r, dict)])
        return rows

    async def query_analytics_sql(self, sql: str) -> list[dict]:
        """Run ONE SQL query against Workers Analytics Engine and return its rows
        (SA-4 — the read half of Paw Sites visitor analytics).

        POSTs to ``/accounts/{acct}/analytics_engine/sql``. Two things about this
        endpoint are unlike every other method on this client, and both are load-
        bearing rather than trivia:

        * **The body is RAW SQL, not JSON.** Cloudflare's own example is
          ``curl ... --data "SELECT ..."``. There is no parameter binding, so the
          caller MUST NOT interpolate anything a user controls into the statement.
          ``sites.service`` builds the only statements that reach here, and the one
          value it substitutes is a site id it has already round-tripped through
          ``ObjectId`` — 24 hex characters, checked again at the call site.
        * **The response is NOT the Cloudflare success envelope.** A query answers
          ``{"meta": [...], "data": [...], "rows": N}`` with no ``success`` key at
          all, so ``_unwrap`` cannot read it: that helper raises on a body whose
          ``success`` is absent, which would turn every SUCCESSFUL query into a
          Cloudflare error. Only the failure branch is shared, through
          ``_error_detail``.

        Returns the ``data`` rows as a list of dicts. An empty ``data`` is a real,
        legitimate answer — the query matched nothing — and is returned as ``[]``.

        FAIL-CLOSED, and here that is the whole requirement rather than a habit. A
        non-2xx, a body that is not JSON, and a 2xx whose ``data`` is missing or not
        a list all raise ValidationError. The alternative — returning ``[]`` — would
        render a customer's dashboard as "0 visitors" when the truth is "the read
        failed", and a panel that reports an outage as a quiet week is worse than one
        that reports nothing at all.

        The API token needs ``Account Analytics Read``, which is a DIFFERENT
        permission from the Workers and SSL scopes the deploy paths use. A token
        without it answers 403, and ``_error_detail`` puts Cloudflare's own sentence
        (and its code) in the raised message so the missing scope is diagnosable
        rather than a bare status."""
        url = f"{_CF_API}/accounts/{self._account_id}/analytics_engine/sql"
        async with self._client() as client:
            resp = await client.post(
                url, content=sql.encode("utf-8"), headers={"Content-Type": "text/plain"}
            )
        if resp.status_code // 100 != 2:
            raise ValidationError(
                "sites.cloudflare_error",
                f"Analytics Engine SQL {resp.status_code}: {_error_detail(resp)}",
            )
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValidationError(
                "sites.cloudflare_error",
                "Analytics Engine returned a non-JSON body for a SQL query.",
            ) from exc
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            # A 2xx with no ``data`` array is a contract break, not an empty result:
            # an empty result IS a ``data`` of ``[]``. Reporting it as no rows would
            # manufacture the zeros this method exists to never manufacture.
            raise ValidationError(
                "sites.cloudflare_error",
                "Analytics Engine returned no data array for a SQL query.",
            )
        return [r for r in rows if isinstance(r, dict)]
