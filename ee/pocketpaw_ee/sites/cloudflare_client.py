# ee/pocketpaw_ee/sites/cloudflare_client.py — async Cloudflare API client for
# the Sites control plane. Five surfaces:
#   * Workers for Platforms — PUT a user Worker into our dispatch namespace
#     (one synchronous call per site; live on 200; no per-account script cap).
#   * Cloudflare for SaaS — create a custom hostname, return the single CNAME
#     the client pastes, poll validation + TLS status.
#   * D1 provisioning (DP0-1) — create a per-tenant D1 database and return its
#     real uuid, so a Dynamic Paw Site's data plane can be stood up; see
#     create_database.
#   * D1 (DS-3) — query a dynamic site's per-tenant D1 over the HTTP API so the
#     control plane can READ its data (the operator data-view); see query_d1.
#   * Browser Rendering (SC-1) — screenshot a deployed site's live URL so its
#     gallery card can show the page instead of a title and three pills; see
#     capture_screenshot.
# httpx-based; account id + token come from settings (env), not per-tenant rows
# in v1. Non-2xx raises a CloudError so the standard envelope applies.
#
# Secret handling: the CF API token lives only in the in-memory Authorization
# header — never logged, never written to disk. All errors fail closed (raise
# ValidationError), so a failed CF call never silently reports success.
# Created: 2026-05-30 (feat/paw-sites-backend, Task 2.2).
#
# Updated 2026-07-08 (DP0-1 — D1 provisioning for Dynamic Paw Sites Phase 0):
# added create_database(). It POSTs to the Cloudflare D1 create endpoint
# (POST /accounts/{acct}/d1/database) with a ``{"name": <name>}`` body and returns
# the new database's real uuid (``result.uuid`` in the envelope). It mirrors the
# existing client style exactly: the CF token in the in-memory Authorization header
# only, and fail-closed via the shared ``_unwrap`` (a non-2xx or success:false
# raises ValidationError). This is the FIRST step of the durable provision job: the
# returned uuid is persisted immediately so a retry reuses the same D1 instead of
# orphaning a second one.
#
# Updated 2026-06-20 (DS-3 — control-plane read of a dynamic site's D1): added
# query_d1(). It POSTs to the Cloudflare D1 query endpoint
# (POST /accounts/{acct}/d1/database/{db_id}/query) with a PARAMETERIZED
# {sql, params} body and returns the rows from the FIRST statement's
# ``result[0].results``. It mirrors the existing client style exactly: injectable
# transport, the CF token in the in-memory Authorization header only, and
# fail-closed via the shared ``_unwrap`` (a non-2xx or success:false raises
# ValidationError). It NEVER interpolates SQL — the table identifier is validated
# against the site's known objects by the service BEFORE it reaches here, and all
# values bind through ``params``. The service layer (DS-3) owns that validation;
# this method is a thin, SQL-agnostic transport.
# Updated 2026-06-20 (DS-2 — dynamic-site D1 bindings): ``put_worker`` gained an
# optional ``bindings`` param. A DYNAMIC Paw Site is backed by a per-tenant
# Cloudflare D1; its deployed Worker needs a D1 binding to reach that DB. When
# bindings are supplied, the upload switches to the Workers multipart/form-data
# contract — a ``metadata`` JSON part (main_module + bindings + compatibility_date)
# plus the module file part referenced by its filename. When NO bindings are
# supplied (a static site), it keeps the prior single-module upload byte-for-byte,
# so the static path never regresses. The fail-closed + in-memory-token handling
# is identical on both paths.
# Updated 2026-06-24 (BC-10 — resell Cloudflare features by site-plan tier):
# ``create_custom_hostname`` gained an optional ``features`` param (the
# ``cloudflare_features`` set from the site's plan tier — e.g. ``{"waf",
# "edge_cache", ...}``). When features are present, the custom-hostname request
# carries the corresponding premium SSL/settings fields via ``_ssl_for_features``
# (a pure feature-set → CF ``ssl`` payload map) plus a ``custom_metadata`` block
# recording the resold feature set, so a HIGHER tier provisions paid security
# (WAF / strict TLS / edge cache) at hostname-create time. When ``features`` is
# None/empty (the BASE tier), the request is the prior basic
# ``{"method": "http", "type": "dv"}`` ssl payload byte-for-byte, so a basic-tier
# site never regresses. The mapping is intentionally MINIMAL + documented (a real
# CF account tunes the exact toggles); the contract is "feature set in → those
# CF fields out", asserted with a mocked transport.
# Updated 2026-08-07 (SC-1 — a site's card shows its own screenshot): added
# ``capture_screenshot``. It POSTs to the Browser Rendering screenshot endpoint
# (POST /accounts/{acct}/browser-rendering/screenshot) with a ``{url,
# screenshotOptions, viewport, gotoOptions}`` body and returns the raw image
# BYTES. It is the one method here whose happy path is NOT the JSON envelope: a
# successful render replies with the image itself (``image/png``), so the shared
# ``_unwrap`` only governs the failure branch. Anything that is not 2xx + an
# ``image/*`` body raises ValidationError, so a Cloudflare error page or an empty
# body can never be stored as a site's preview. Callers must NOT pass a
# ``quality`` in ``screenshot_options`` without also passing a ``type`` of jpeg /
# webp — quality is incompatible with the default png and Cloudflare answers 400.
# Updated 2026-08-07 (SC-2 — drafts get art too): ``capture_screenshot`` now takes
# ``html`` as an alternative to ``url``. A DRAFT site has no address to point a
# browser at — that is what makes it a draft — so it is rendered from its own
# markup instead. Exactly one of the two is required; the method raises on both or
# neither rather than letting Cloudflare answer 400. Note that an ``html`` body
# renders at ``about:blank``, so nothing relative inside it resolves: assembling a
# SELF-CONTAINED document is the caller's job (``sites.draft_markup``).

from __future__ import annotations

import json

import httpx

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus

_CF_API = "https://api.cloudflare.com/client/v4"

# Filename the module part is uploaded under (and named by ``main_module`` in the
# metadata). The Workers multipart upload references modules by filename; the
# bundle our generator emits is a single ESM entry, so one ``*.mjs`` part suffices.
_MAIN_MODULE = "index.mjs"

# Workers compatibility date for the multipart upload's metadata. Matches the
# date the generator bakes into the (otherwise-ignored-on-direct-API-upload)
# wrangler.toml so the runtime semantics are identical to a wrangler deploy.
_COMPATIBILITY_DATE = "2024-09-23"

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

    No features (base tier) → the prior basic ``{"method": "http", "type": "dv"}``
    payload, unchanged. With features, the basic payload gains an ``ssl.settings``
    block that is the UNION of every known feature's fragment (``_FEATURE_SSL_SETTINGS``)
    plus a ``custom_metadata`` recording the full resold feature set (sorted, so the
    wire payload is deterministic). A feature with no ssl fragment still lands on
    ``custom_metadata`` — the tier is visible even when it provisions no toggle.
    """
    if not features:
        return dict(_BASIC_SSL)
    settings: dict = {}
    for feat in features:
        settings.update(_FEATURE_SSL_SETTINGS.get(feat, {}))
    ssl: dict = dict(_BASIC_SSL)
    if settings:
        ssl["settings"] = settings
    # Record the resold feature set on the hostname so the tier is auditable on
    # the CF side (sorted → deterministic wire payload).
    ssl["custom_metadata"] = {"resold_features": ",".join(sorted(features))}
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
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._account_id = account_id
        self._zone_id = zone_id
        self._namespace = dispatch_namespace
        self._headers = {"Authorization": f"Bearer {api_token}"}
        self._transport = _transport  # tests inject a MockTransport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, transport=self._transport, timeout=30.0)

    @staticmethod
    def _unwrap(resp: httpx.Response) -> dict:
        if resp.status_code // 100 != 2:
            raise ValidationError("sites.cloudflare_error", f"Cloudflare API {resp.status_code}")
        body = resp.json()
        if not body.get("success", False):
            errs = body.get("errors") or [{"message": "unknown"}]
            raise ValidationError("sites.cloudflare_error", str(errs[0].get("message")))
        return body.get("result", {})

    async def put_worker(
        self,
        *,
        script_name: str,
        bundle: bytes,
        bindings: list[dict] | None = None,
    ) -> bool:
        """Upload a user Worker into the dispatch namespace. Live on 200.

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
        by filename)."""
        url = (
            f"{_CF_API}/accounts/{self._account_id}"
            f"/workers/dispatch/namespaces/{self._namespace}/scripts/{script_name}"
        )
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

    async def create_custom_hostname(
        self, hostname: str, *, features: set[str] | None = None
    ) -> CustomHostname:
        """Register a Cloudflare-for-SaaS custom hostname, return its single CNAME.

        ``features`` (BC-10) is the site plan tier's ``cloudflare_features`` set.
        When present, the create request carries the premium ``ssl`` payload built
        by ``_ssl_for_features`` (strict-TLS / HTTP-2 toggles for WAF / edge-cache
        + a ``custom_metadata`` recording the resold set), so a HIGHER-tier site
        provisions paid security at hostname-create time. When None/empty (the BASE
        tier), the request is the prior basic DV ``ssl`` payload, unchanged — a
        base-tier site never regresses.
        """
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
            cname_target=f"{self._zone_id}.cdn.cloudflare.net",
        )

    async def get_hostname_status(self, hostname_id: str) -> HostnameStatus:
        url = f"{_CF_API}/zones/{self._zone_id}/custom_hostnames/{hostname_id}"
        async with self._client() as client:
            resp = await client.get(url)
        result = self._unwrap(resp)
        return _map_status(result.get("status", ""), (result.get("ssl") or {}).get("status", ""))

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
