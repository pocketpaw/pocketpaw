# scripts/sites_do_spike.py: staging spike for Durable Objects on Paw Sites
# (design: paw-workspace docs/design/drafts/2026-10-08-sites-durable-objects.md,
# slice 1). Answers three questions against a REAL staging account, using our own
# CloudflareClient (never wrangler):
#   A. Once a script has a migration tag, does an upload WITHOUT ``migrations`` pass?
#   B. Tombstone (``deleted_classes``) then ``DELETE ?force=true``: are the script's
#      DO namespaces gone afterwards?
#   C. ``DELETE ?force=true`` alone, no tombstone: are the namespaces gone?
#   D. The GraphQL analytics schema ``sites.do_metering`` relies on: the
#      durableObjects* datasets on ``account`` and their dimensions / sum / max
#      fields (introspection), then ``do_metering.read_usage`` for a live spike
#      script, so a renamed field shows up here before the usage sweep fails open.
#   E. ``durable_objects.set_platform_vars_live`` on a live script with a
#      ``secret_text`` binding and a plain_text var: after the settings PATCH, is the
#      secret still listed (by name; its value is never read back), can the Worker
#      still read it, and did the var change (in settings and in the Worker)? Prints
#      booleans only. MUST print all True before PAW_SITES_DURABLE_OBJECTS goes on.
#
# How to run (staging account only, from the pocketpaw repo root):
#   PAW_CF_ACCOUNT_ID=<staging account id> PAW_CF_API_TOKEN=<token with Workers
#   Scripts:Edit and Account Analytics:Read> uv run --group ee python scripts/sites_do_spike.py
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
from datetime import UTC, datetime

import httpx
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import do_metering, durable_objects
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


_TYPE_FIELDS = """query T($name: String!) { __type(name: $name) { fields { name
  type { name kind ofType { name kind ofType { name kind ofType { name } } } } } } }"""


def _leaf(t: dict | None) -> str:
    while t and not t.get("name"):
        t = t.get("ofType")
    return (t or {}).get("name") or "?"


async def _fields(cf: CloudflareClient, type_name: str) -> dict[str, str]:
    data = await cf.query_graphql(_TYPE_FIELDS, {"name": type_name})
    rows = ((data.get("__type") or {}).get("fields")) or []
    return {f["name"]: _leaf(f.get("type")) for f in rows}


async def scenario_analytics(cf: CloudflareClient) -> None:
    try:
        account = await _fields(cf, "account")
        datasets = {k: v for k, v in account.items() if k.startswith("durableObjects")}
        _say("D1 datasets", sorted(datasets))
        for name, type_name in sorted(datasets.items()):
            group = await _fields(cf, type_name)
            for part in ("dimensions", "sum", "max"):
                if part in group:
                    _say(f"D2 {name}.{part}", sorted(await _fields(cf, group[part])))
    except ValidationError as exc:
        _say("D introspection", f"REFUSED: {exc.message}")
    name = f"paw-spike-do-{secrets.token_hex(3)}"
    try:
        v1 = {"new_tag": "v1", "steps": [{"new_sqlite_classes": [CLASS]}]}
        _say("D3 upload v1", await _put(cf, name, WITH_DO, BINDINGS, v1))
        await _write_row(cf, name)
        try:
            usage = await do_metering.read_usage(cf, [name], datetime.now(UTC).date())
            _say("D3 read_usage (analytics lag: zeros are normal right away)", usage)
        except ValidationError as exc:
            _say("D3 read_usage", f"REFUSED: {exc.message}")
    finally:
        await cf.delete_account_script(name, force=True)


_E_VAR = "PAW_DO_THROTTLED"


async def _worker_view(cf: CloudflareClient, name: str) -> dict:
    """``{"secret_ok": bool, "var_is_one": bool}`` from the spike Worker over
    workers.dev, polled until the var reads "1" or about a minute passes."""
    await cf.enable_workers_dev(name)
    sub = await cf.workers_dev_subdomain()
    if not sub:
        return {}
    seen: dict = {}
    async with httpx.AsyncClient(timeout=10) as http:
        for _ in range(12):
            try:
                resp = await http.get(f"https://{name}.{sub}.workers.dev/")
                if resp.status_code == 200:
                    seen = resp.json()
                    if seen.get("var_is_one"):
                        return seen
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(5)
    return seen


async def scenario_live_vars(cf: CloudflareClient) -> None:
    name = f"paw-spike-do-{secrets.token_hex(3)}"
    # A throwaway value made here, compared inside the Worker; never printed.
    expected = secrets.token_hex(16)
    code = (
        "export default { fetch(request, env) { return Response.json({"
        f'secret_ok: env.SECRET === "{expected}", var_is_one: env.{_E_VAR} === "1"'
        "}); } };\n"
    )
    bindings = [
        {"type": "secret_text", "name": "SECRET", "text": expected},
        {"type": "plain_text", "name": _E_VAR, "text": "0"},
    ]
    try:
        uploaded = (await _put(cf, name, code, bindings, None)).startswith("accepted")
        _say("E upload ok", uploaded)
        if not uploaded:
            return
        try:
            await durable_objects.set_platform_vars_live(
                cf, name, target=ACCOUNT_TARGET, values={_E_VAR: "1"}
            )
            pushed = True
        except ValidationError:
            pushed = False
        _say("E settings push ok", pushed)
        try:
            listed = (await cf.get_script_settings(name, target=ACCOUNT_TARGET)).get(
                "bindings"
            ) or []
        except ValidationError:
            listed = []
        by_name = {b.get("name"): b for b in listed if isinstance(b, dict)}
        _say("E secret still listed", by_name.get("SECRET", {}).get("type") == "secret_text")
        _say("E var changed in settings", by_name.get(_E_VAR, {}).get("text") == "1")
        view = await _worker_view(cf, name)
        _say("E worker reads the secret", view.get("secret_ok") is True)
        _say("E worker sees the new var", view.get("var_is_one") is True)
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
    await scenario_analytics(cf)
    await scenario_live_vars(cf)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
