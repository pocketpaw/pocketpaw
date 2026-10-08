# scripts/sites_do_spike.py: staging spike for Durable Objects on Paw Sites
# (design: paw-workspace docs/design/drafts/2026-10-08-sites-durable-objects.md,
# slice 1). Answers three questions against a REAL staging account, using our own
# CloudflareClient (never wrangler):
#   A. Once a script has a migration tag, does an upload WITHOUT ``migrations`` pass?
#   B. Tombstone (``deleted_classes``) then ``DELETE ?force=true``: are the script's
#      DO namespaces gone afterwards?
#   C. ``DELETE ?force=true`` alone, no tombstone: are the namespaces gone?
#
# How to run (staging account only, from the pocketpaw repo root):
#   PAW_CF_ACCOUNT_ID=<staging account id> PAW_CF_API_TOKEN=<token with Workers
#   Scripts:Edit> uv run --group ee python scripts/sites_do_spike.py
# Optional: PAW_SPIKE_WRITE=1 also enables workers.dev on each spike script and
# writes one row into the DO before teardown, so storage really exists.
#
# Every script is named ``paw-spike-do-<hex>`` on the ACCOUNT target and is force
# deleted in a ``finally``. Output is non-secret only: script names, tags, status
# codes, Cloudflare error text and namespace rows (id, class, script). The token is
# read from env by name and never printed.
from __future__ import annotations

import asyncio
import os
import secrets
import sys

import httpx
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.cloudflare_client import ACCOUNT_TARGET, CloudflareClient, WorkerModule

CLASS = "SpikeRoom"
COMPAT = "2026-09-01"
WITH_DO = f"""
import {{ DurableObject }} from "cloudflare:workers";
export class {CLASS} extends DurableObject {{
  async fetch(request) {{
    this.ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS t (v TEXT)");
    this.ctx.storage.sql.exec("INSERT INTO t VALUES ('x')");
    const n = this.ctx.storage.sql.exec("SELECT count(*) AS n FROM t").one().n;
    return new Response(String(n));
  }}
}}
export default {{
  fetch(request, env) {{ return env.ROOM.getByName("spike").fetch(request); }}
}};
"""
TOMBSTONE = 'export default { fetch() { return new Response("gone", { status: 410 }); } };\n'
BINDINGS = [{"type": "durable_object_namespace", "name": "ROOM", "class_name": CLASS}]


def _module(code: str) -> list[WorkerModule]:
    return [WorkerModule("index.js", code.encode(), "application/javascript+module")]


def _say(step: str, outcome: object) -> None:
    print(f"[{step}] {outcome}", flush=True)


async def _put(cf: CloudflareClient, name: str, code: str, bindings, migrations) -> str:
    try:
        upload = await cf.put_worker(
            script_name=name,
            modules=_module(code),
            main_module="index.js",
            bindings=bindings,
            compatibility_date=COMPAT,
            target=ACCOUNT_TARGET,
            migrations=migrations,
        )
        return f"accepted, migration_tag={getattr(upload, 'migration_tag', None)!r}"
    except ValidationError as exc:
        return f"REFUSED: {exc.message}"


async def _namespaces(cf: CloudflareClient, name: str) -> list[dict]:
    rows = await cf.list_durable_object_namespaces()
    return [
        {k: r.get(k) for k in ("id", "class", "script", "use_sqlite")}
        for r in rows
        if r.get("script") == name
    ]


async def _write_row(cf: CloudflareClient, name: str) -> None:
    if os.environ.get("PAW_SPIKE_WRITE") != "1":
        return
    await cf.enable_workers_dev(name)
    sub = await cf.workers_dev_subdomain()
    url = f"https://{name}.{sub}.workers.dev/"
    async with httpx.AsyncClient(timeout=10) as http:
        for _ in range(12):
            try:
                resp = await http.get(url)
                if resp.status_code == 200:
                    _say("write", f"rows in DO = {resp.text}")
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(5)
    _say("write", "workers.dev never answered 200; storage may be empty")


async def scenario_tombstone(cf: CloudflareClient) -> None:
    name = f"paw-spike-do-{secrets.token_hex(3)}"
    try:
        v1 = {"new_tag": "v1", "steps": [{"new_sqlite_classes": [CLASS]}]}
        _say("A1 upload v1", await _put(cf, name, WITH_DO, BINDINGS, v1))
        await _write_row(cf, name)
        _say("A1 namespaces", await _namespaces(cf, name))
        _say("A2 redeploy, no migrations", await _put(cf, name, WITH_DO, BINDINGS, None))
        tomb = {
            "old_tag": "v1",
            "new_tag": "paw-tombstone",
            "steps": [{"deleted_classes": [CLASS]}],
        }
        _say("B1 tombstone", await _put(cf, name, TOMBSTONE, [], tomb))
        _say("B1 namespaces after tombstone", await _namespaces(cf, name))
        await cf.delete_account_script(name, force=True)
        _say("B2 force delete", "done")
        _say("B2 namespaces after delete", await _namespaces(cf, name))
    finally:
        await cf.delete_account_script(name, force=True)


async def scenario_force_only(cf: CloudflareClient) -> None:
    name = f"paw-spike-do-{secrets.token_hex(3)}"
    try:
        v1 = {"new_tag": "v1", "steps": [{"new_sqlite_classes": [CLASS]}]}
        _say("C1 upload v1", await _put(cf, name, WITH_DO, BINDINGS, v1))
        await _write_row(cf, name)
        _say("C1 namespaces", await _namespaces(cf, name))
        await cf.delete_account_script(name, force=True)
        _say("C2 force delete, no tombstone", "done")
        _say("C2 namespaces after delete", await _namespaces(cf, name))
    finally:
        await cf.delete_account_script(name, force=True)


async def main() -> int:
    account = os.environ.get("PAW_CF_ACCOUNT_ID", "")
    token = os.environ.get("PAW_CF_API_TOKEN", "")
    if not account or not token:
        print("set PAW_CF_ACCOUNT_ID and PAW_CF_API_TOKEN (staging account)", file=sys.stderr)
        return 2
    cf = CloudflareClient(account_id=account, api_token=token, zone_id="", dispatch_namespace="")
    await scenario_tombstone(cf)
    await scenario_force_only(cf)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
