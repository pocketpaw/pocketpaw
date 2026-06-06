# ee/pocketpaw_ee/sites/cloudflare_client.py — async Cloudflare API client for
# the Sites control plane. Surfaces:
#   * Workers for Platforms — deploy a generated SvelteKit site (adapter-cloudflare
#     output) into our dispatch namespace as one user Worker tagged by site id.
#   * Cloudflare for SaaS — create a custom hostname, return the single CNAME the
#     client pastes, poll validation + TLS status, verify it end to end.
# httpx-based; account id + token come from settings (env), not per-tenant rows
# in v1. Non-2xx raises a ValidationError so the standard envelope applies.
#
# Updated 2026-06-06 (feat/1346-cf-deploy — Cloudflare deploy pipeline):
#   * NEW deploy_site(): the REAL edge-deploy entry point the publish path uses.
#     A SvelteKit adapter-cloudflare build is NOT a single module — it emits a
#     _worker.js entry, a _routes.json, and a static-assets tree (_app/…, the
#     prerendered marketing HTML). A bare single-module PUT (the old put_worker)
#     would deploy a worker with NO static assets and NO bindings, so the
#     prerendered routes 404 and the form endpoint's D1/Queue bindings are
#     missing — which is exactly why "the preview URL is dead" (issue #1346).
#     deploy_site does the correct upload:
#       1. Workers Assets two-step — open an upload session for the static tree,
#          upload the files the API reports missing, receive a completion JWT.
#       2. Multipart script PUT to the dispatch namespace with a `metadata` part
#          (main_module, compatibility_date/flags, the bindings parsed from the
#          generated wrangler.toml, and the assets {jwt, config}) + the worker
#          module part. Live on 200.
#     bindings + compatibility settings are read FROM the generated wrangler.toml
#     (the generator stays the source of truth) via _read_wrangler_config().
#   * NEW verify_domain(): re-poll a custom hostname and report whether it has
#     reached LIVE (CNAME seen + TLS active) — the backend half of the
#     DomainsPanel "Verify" action. Thin wrapper over get_hostname_status so the
#     status mapping stays in one place.
#   * NEW live_url_for(): the stable *.workers.dev-style URL a deployed script is
#     reachable at, derived from the configured dispatch base. Returned by
#     deploy_site so the caller can persist a real openable address.
#   * put_worker() is kept verbatim for back-compat (callers/tests that upload a
#     raw single module). deploy_site is the path publish() should take.
#
# Secret handling: the CF API token lives only in the in-memory Authorization
# header — never logged, never written to disk. All errors fail closed (raise
# ValidationError), so a failed CF call never silently reports success.
# Created: 2026-05-30 (feat/paw-sites-backend, Task 2.2).

from __future__ import annotations

import base64
import hashlib
import json
import os
import tomllib
from pathlib import Path
from typing import Any

import httpx

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus

_CF_API = "https://api.cloudflare.com/client/v4"

# adapter-cloudflare emits the deployable artifact here (same dir the local
# server serves and the bundle reader reads _worker.js from).
_CLOUDFLARE_BUILD_REL = ".svelte-kit/cloudflare"
_WORKER_ENTRY = "_worker.js"
# Files the SvelteKit Cloudflare build emits that are NOT user-servable static
# assets — they configure the deploy, not the site, so they are excluded from the
# Workers Assets upload.
_NON_ASSET_FILES = {_WORKER_ENTRY, "_routes.json", "_headers", "_redirects"}


def _map_status(cf_status: str, ssl_status: str) -> HostnameStatus:
    if cf_status == "active" and ssl_status == "active":
        return HostnameStatus.LIVE
    if cf_status in {"pending", "pending_deletion"}:
        return HostnameStatus.PENDING
    if cf_status in {"active"} or ssl_status in {"pending_validation", "initializing"}:
        return HostnameStatus.VERIFYING
    return HostnameStatus.ERROR


def _read_wrangler_config(project_dir: str) -> dict[str, Any]:
    """Parse the generated wrangler.toml into the deploy metadata's
    compatibility settings + bindings. The generator is the source of truth for
    what the worker binds (D1, Queues), so we read its config rather than
    hardcode it here. Missing file → empty config (a static-only worker with no
    bindings still deploys)."""
    path = Path(project_dir, "wrangler.toml")
    if not path.is_file():
        return {"compatibility_date": None, "compatibility_flags": [], "bindings": []}
    raw = tomllib.loads(path.read_text())
    bindings: list[dict[str, Any]] = []
    for d1 in raw.get("d1_databases", []) or []:
        # A site with an unprovisioned D1 (database_id still the __TOKEN__ or
        # empty) is static-only — skip the binding so the deploy doesn't fail on
        # a non-existent database. Dynamic sites carry a real id (RFC 12 A2).
        db_id = (d1.get("database_id") or "").strip()
        if not db_id or db_id.startswith("__"):
            continue
        bindings.append({"type": "d1", "name": d1["binding"], "id": db_id})
    for prod in raw.get("queues", {}).get("producers", []) or []:
        bindings.append({"type": "queue", "name": prod["binding"], "queue_name": prod["queue"]})
    return {
        "compatibility_date": raw.get("compatibility_date"),
        "compatibility_flags": raw.get("compatibility_flags", []) or [],
        "bindings": bindings,
    }


def _collect_assets(project_dir: str) -> dict[str, Path]:
    """Map each static asset's server path ("/_app/…", "/index.html", …) to its
    file on disk, walking the adapter-cloudflare output and excluding the worker
    entry + the deploy-config files. These are what the Workers Assets session
    uploads so the prerendered marketing routes are actually served."""
    root = Path(project_dir, _CLOUDFLARE_BUILD_REL)
    assets: dict[str, Path] = {}
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(root).as_posix()
        if rel in _NON_ASSET_FILES:
            continue
        assets["/" + rel] = f
    return assets


def _hash_asset(data: bytes) -> str:
    """The content hash the Workers Assets API keys files by (sha256 hex,
    truncated to 32 chars — Cloudflare's manifest hash length)."""
    return hashlib.sha256(data).hexdigest()[:32]


class CloudflareClient:
    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        zone_id: str,
        dispatch_namespace: str,
        dispatch_base: str | None = None,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._account_id = account_id
        self._zone_id = zone_id
        self._namespace = dispatch_namespace
        # The hostname deployed scripts are reachable at, e.g.
        # "paw-sites.example.workers.dev" or a Cloudflare-for-SaaS fallback
        # origin. Per-site URL is "https://<base>/<script_name>/". Configured via
        # PAW_CF_DISPATCH_BASE; falls back to the namespace name so a URL is
        # always returned (the custom domain is the real public address).
        self._dispatch_base = dispatch_base or os.environ.get(
            "PAW_CF_DISPATCH_BASE", f"{dispatch_namespace}.workers.dev"
        )
        self._headers = {"Authorization": f"Bearer {api_token}"}
        self._transport = _transport  # tests inject a MockTransport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, transport=self._transport, timeout=60.0)

    @staticmethod
    def _unwrap(resp: httpx.Response) -> dict:
        if resp.status_code // 100 != 2:
            raise ValidationError("sites.cloudflare_error", f"Cloudflare API {resp.status_code}")
        body = resp.json()
        if not body.get("success", False):
            errs = body.get("errors") or [{"message": "unknown"}]
            raise ValidationError("sites.cloudflare_error", str(errs[0].get("message")))
        return body.get("result", {})

    def live_url_for(self, script_name: str) -> str:
        """The stable URL a deployed dispatch-namespace script is served at. The
        custom domain (Cloudflare for SaaS) is the real public address; this is
        the always-available fallback so the Site carries an openable URL."""
        return f"https://{self._dispatch_base}/{script_name}/"

    # ---- Workers for Platforms: deploy a generated SvelteKit site -----------

    async def deploy_site(self, *, script_name: str, project_dir: str) -> str:
        """Deploy the generated adapter-cloudflare build into the dispatch
        namespace as one Worker (script + static assets + bindings) and return
        its stable live URL. Live on 200.

        This is the publish path's real deploy step. Unlike put_worker (a raw
        single-module PUT), it ships the whole SvelteKit artifact: the static
        assets the prerendered marketing routes need AND the D1/Queue bindings
        the form endpoint reads, taken from the generated wrangler.toml. Fails
        closed (raises ValidationError) on any non-2xx — a failed deploy never
        reports success."""
        root = Path(project_dir, _CLOUDFLARE_BUILD_REL)
        worker_path = root / _WORKER_ENTRY
        if not worker_path.is_file():
            raise ValidationError("sites.cloudflare_error", f"no worker entry at {worker_path}")
        config = _read_wrangler_config(project_dir)
        assets = _collect_assets(project_dir)
        assets_jwt = await self._upload_assets(script_name, assets) if assets else None
        await self._put_script(
            script_name=script_name,
            worker_module=worker_path.read_bytes(),
            config=config,
            assets_jwt=assets_jwt,
        )
        return self.live_url_for(script_name)

    async def _upload_assets(self, script_name: str, assets: dict[str, Path]) -> str:
        """Run the Workers Assets two-step for a dispatch-namespace script and
        return the completion JWT the script PUT references.

        Step 1 — POST the manifest (path → {hash, size}); the API replies with an
        upload JWT and the buckets of hashes it still needs.
        Step 2 — for each non-empty bucket, upload the file bodies (base64) keyed
        by hash; the final 2xx carries the completion JWT.
        If the API already has every file (no buckets), step 1's JWT is the
        completion JWT and no upload round-trip is needed."""
        # ``assets`` is already {server_path -> file}, so read each body and key
        # the manifest by that server path. by_hash lets an upload bucket (a list
        # of content hashes) map back to the file bytes.
        contents: dict[str, bytes] = {sp: path.read_bytes() for sp, path in assets.items()}
        manifest = {
            server_path: {"hash": _hash_asset(data), "size": len(data)}
            for server_path, data in contents.items()
        }
        by_hash = {_hash_asset(data): data for data in contents.values()}

        session_url = (
            f"{_CF_API}/accounts/{self._account_id}"
            f"/workers/dispatch/namespaces/{self._namespace}"
            f"/scripts/{script_name}/assets-upload-session"
        )
        async with self._client() as client:
            resp = await client.post(session_url, json={"manifest": manifest})
            result = self._unwrap(resp)
            jwt = result.get("jwt", "")
            buckets = result.get("buckets") or []
            if not buckets:
                # Nothing to upload — the session JWT is the completion token.
                return jwt
            upload_url = f"{_CF_API}/accounts/{self._account_id}/workers/assets/upload?base64=true"
            completion_jwt = jwt
            for bucket in buckets:
                files = {h: base64.b64encode(by_hash[h]).decode() for h in bucket if h in by_hash}
                up = await client.post(
                    upload_url,
                    headers={"Authorization": f"Bearer {jwt}"},
                    json={"files": files},
                )
                done = self._unwrap(up)
                completion_jwt = done.get("jwt") or completion_jwt
            return completion_jwt

    async def _put_script(
        self,
        *,
        script_name: str,
        worker_module: bytes,
        config: dict[str, Any],
        assets_jwt: str | None,
    ) -> None:
        """Multipart script PUT into the dispatch namespace: a `metadata` JSON
        part (main_module, compatibility settings, bindings, assets) plus the
        worker ESM module part."""
        metadata: dict[str, Any] = {
            "main_module": _WORKER_ENTRY,
            "compatibility_date": config.get("compatibility_date") or "2024-09-23",
            "compatibility_flags": config.get("compatibility_flags") or [],
            "bindings": config.get("bindings") or [],
        }
        if assets_jwt:
            # html_handling/not_found_handling make the static prerendered routes
            # serve cleanly (pretty URLs, SPA-less 404 → the worker).
            metadata["assets"] = {
                "jwt": assets_jwt,
                "config": {"html_handling": "auto-trailing-slash"},
            }
        url = (
            f"{_CF_API}/accounts/{self._account_id}"
            f"/workers/dispatch/namespaces/{self._namespace}/scripts/{script_name}"
        )
        files = {
            "metadata": (None, json.dumps(metadata), "application/json"),
            _WORKER_ENTRY: (
                _WORKER_ENTRY,
                worker_module,
                "application/javascript+module",
            ),
        }
        async with self._client() as client:
            resp = await client.put(url, files=files)
        self._unwrap(resp)

    async def put_worker(self, *, script_name: str, bundle: bytes) -> bool:
        """Upload a raw single-module user Worker into the dispatch namespace.
        Live on 200. Kept for back-compat; the full publish path uses
        deploy_site (which also ships static assets + bindings)."""
        url = (
            f"{_CF_API}/accounts/{self._account_id}"
            f"/workers/dispatch/namespaces/{self._namespace}/scripts/{script_name}"
        )
        async with self._client() as client:
            resp = await client.put(
                url,
                content=bundle,
                headers={"Content-Type": "application/javascript+module"},
            )
        self._unwrap(resp)
        return True

    # ---- Cloudflare for SaaS: custom hostnames -----------------------------

    async def create_custom_hostname(self, hostname: str) -> CustomHostname:
        url = f"{_CF_API}/zones/{self._zone_id}/custom_hostnames"
        async with self._client() as client:
            resp = await client.post(
                url,
                json={"hostname": hostname, "ssl": {"method": "http", "type": "dv"}},
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

    async def verify_domain(self, hostname_id: str) -> bool:
        """Re-poll a custom hostname and report whether it is fully verified
        (CNAME seen + TLS active → LIVE). The backend half of the DomainsPanel
        "Verify" action: the client pastes the CNAME, then this confirms
        Cloudflare has validated it end to end. Status mapping stays in
        get_hostname_status so there is one source of truth."""
        return await self.get_hostname_status(hostname_id) is HostnameStatus.LIVE
