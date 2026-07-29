"""Boot the cloud stack with the simulated agent backend and seed a workspace,
so ``chat_loadtest.py`` has something to hammer at zero token cost.

Created 2026-07-29 — the load-test rig's server half. It:

  1. Mints a local license + auth secret and points Beanie at a SCRATCH Mongo
     database (dropped on exit unless ``--keep-db``).
  2. Registers ``sim_backend.SimBackend`` under the backend name ``sim`` and
     selects it, so no Anthropic key is needed and no tokens are spent.
  3. Mounts the real cloud FastAPI app — real router, real run executor, real
     Redis stream transport, real Mongo writes, real SSE.
  4. Seeds user → workspace → agent → pocket over the real HTTP API.
  5. Serves it with uvicorn and prints the exact ``chat_loadtest.py`` command,
     with token / workspace / scope id already filled in.

What this measures: YOUR stack's concurrency ceiling. What it does NOT measure:
Anthropic rate limits, or the memory cost of a real Claude Code CLI subprocess
per run (set ``PAW_SIM_SUBPROC=1`` / ``PAW_SIM_RSS_MB`` to approximate that).

Usage:
    uv run python scripts/loadtest/serve_sim.py --port 8099
    # then, in another shell, paste the command it prints.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _license_key(secret: str) -> str:
    payload = {
        "org": "loadtest",
        "plan": "enterprise",
        "seats": 1000,
        "exp": (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d"),  # noqa: DTZ005
    }
    body = json.dumps(payload)
    sig = hashlib.sha256(f"{secret}:{body}".encode()).hexdigest()
    return base64.b64encode(f"{body}.{sig}".encode()).decode()


def _configure_env(args: argparse.Namespace, db_name: str) -> str:
    secret = "loadtest-license-secret"
    uri = f"{args.mongo_url.rstrip('/')}/{db_name}"
    os.environ.update(
        {
            "POCKETPAW_LICENSE_KEY": _license_key(secret),
            "POCKETPAW_LICENSE_SECRET": secret,
            "AUTH_SECRET": "loadtest-auth-secret",
            "POCKETPAW_CLOUD_MONGO_URI": uri,
            "CLOUD_MONGODB_URI": uri,
            "POCKETPAW_AGENT_BACKEND": "sim",
            # Billing off: a load test should hit capacity limits, not the
            # 402 credit gate at run start.
            "POCKETPAW_BILLING_ENFORCED": "0",
            "POCKETPAW_CLOUD_RUN_EXECUTOR": args.executor,
        }
    )
    os.environ.pop("POCKETPAW_MEMORY_BACKEND", None)
    if args.redis_url:
        os.environ["POCKETPAW_REDIS_URL"] = args.redis_url
    return uri


async def _seed(app, base_url: str, n_pockets: int) -> dict[str, str]:
    """Create user → workspace → agent → pocket over the real API."""
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url=base_url) as http:
        email = f"loadtest-{uuid.uuid4().hex[:8]}@test.example"
        # Random: the register endpoint rejects known-breached passwords.
        password = f"Lt-{uuid.uuid4().hex}-9Z!"

        r = await http.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "full_name": "Load Test"},
        )
        if r.status_code != 201:
            raise RuntimeError(f"register failed {r.status_code}: {r.text[:400]}")

        r = await http.post(
            "/api/v1/auth/bearer/login", data={"username": email, "password": password}
        )
        if r.status_code != 200:
            raise RuntimeError(f"login failed {r.status_code}: {r.text[:400]}")
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        slug = f"loadtest-{uuid.uuid4().hex[:6]}"
        r = await http.post(
            "/api/v1/workspaces", json={"name": "Load Test WS", "slug": slug}, headers=headers
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"workspace failed {r.status_code}: {r.text[:400]}")
        workspace_id = r.json()["_id"]
        await http.post(
            "/api/v1/auth/set-active-workspace",
            json={"workspace_id": workspace_id},
            headers=headers,
        )

        r = await http.post(
            "/api/v1/agents",
            json={
                "name": "Sim Agent",
                "slug": f"sim-agent-{uuid.uuid4().hex[:6]}",
                "backend": "sim",
                "system_prompt": "You are a test agent.",
            },
            headers=headers,
        )
        agent_id = ""
        if r.status_code in (200, 201):
            payload = r.json()
            agent_id = str(payload.get("_id") or payload.get("id") or "")
        else:
            print(f"[seed] WARN agent create {r.status_code}: {r.text[:200]}", file=sys.stderr)

        # Seed MANY pockets, not one. A chat run supersedes the previous run in
        # the same scope, so N sends into one pocket is one user typing N times
        # — every earlier run gets cut short and the throughput number is
        # meaningless. One pocket per concurrent virtual user is what actually
        # models N users.
        pocket_ids: list[str] = []
        for i in range(max(1, n_pockets)):
            r = await http.post(
                "/api/v1/pockets", json={"name": f"Load Test Pocket {i + 1}"}, headers=headers
            )
            if r.status_code not in (200, 201):
                raise RuntimeError(f"pocket {i} failed {r.status_code}: {r.text[:400]}")
            payload = r.json()
            pocket_id = str(payload.get("_id") or payload.get("id"))
            pocket_ids.append(pocket_id)

            # Attach the sim agent, or a chat addressed to it comes back 400
            # agent.not_in_scope — and an unaddressed one resolves to the
            # pocket's default agent, which is NOT the sim backend.
            if agent_id:
                r = await http.post(
                    f"/api/v1/pockets/{pocket_id}/agents",
                    json={"agentId": agent_id},
                    headers=headers,
                )
                if r.status_code not in (200, 201, 204):
                    print(
                        f"[seed] WARN attach agent->pocket {r.status_code}: {r.text[:200]}",
                        file=sys.stderr,
                    )

        return {
            "token": token,
            "workspace_id": workspace_id,
            "agent_id": agent_id,
            "pocket_id": pocket_ids[0],
            "pocket_ids": ",".join(pocket_ids),
            "email": email,
        }


async def main_async(args: argparse.Namespace) -> int:
    db_name = args.db or f"loadtest_{uuid.uuid4().hex[:8]}"
    uri = _configure_env(args, db_name)

    import pocketpaw_ee.cloud.license as lic_mod
    from beanie import init_beanie
    from fastapi import FastAPI
    from motor.motor_asyncio import AsyncIOMotorClient
    from pocketpaw_ee.cloud import mount_cloud
    from pocketpaw_ee.cloud.memory.bootstrap import register_default_backend
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    from pocketpaw.agents.registry import register_backend

    register_backend("sim", "sim_backend", "SimBackend")
    lic_mod._cached_license = None
    lic_mod._license_error = None

    print(f"[serve] mongo   {uri}")
    print(f"[serve] executor {args.executor}   redis {os.environ.get('POCKETPAW_REDIS_URL')}")
    await init_beanie(connection_string=uri, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    register_default_backend()

    app = FastAPI(title="pocketpaw loadtest rig")
    mount_cloud(app)

    print("[serve] seeding workspace…", flush=True)
    seed = await _seed(app, f"http://127.0.0.1:{args.port}", args.pockets)
    seed["mongo_db"] = db_name
    seed["port"] = str(args.port)
    n_pk = len(seed["pocket_ids"].split(","))
    print(f"[serve] workspace={seed['workspace_id']} pockets={n_pk}", flush=True)
    if seed["agent_id"]:
        print(f"[serve] agent={seed['agent_id']}", flush=True)

    # Also drop the credentials on disk — stdout is buffered when the rig runs
    # detached, and the driver needs these to be scriptable.
    seed_path = Path(args.seed_out)
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    seed_path.write_text(json.dumps(seed, indent=2), encoding="utf-8")
    print(f"[serve] seed written to {seed_path}", flush=True)

    agent_flag = f" \\\n    --agent-id {seed['agent_id']}" if seed["agent_id"] else ""
    print(
        "\n"
        + "=" * 78
        + "\nREADY — run this in another shell:\n\n"
        + f"""uv run python scripts/loadtest/chat_loadtest.py \\
    --base-url http://127.0.0.1:{args.port} \\
    --token {seed["token"]} \\
    --workspace {seed["workspace_id"]} \\
    --scope pocket --scope-id {seed["pocket_ids"]}{agent_flag} \\
    --surface sites --prompt "Build a landing page for a dentist {{n}}" \\
    --mode open --rate 1 --duration 60 --jitter \\
    --redis-url {os.environ.get("POCKETPAW_REDIS_URL", "redis://localhost:6379")} \\
    --mongo-url {args.mongo_url} --mongo-db {db_name} \\
    --out out/sim-1rps"""
        + "\n\n"
        + "=" * 78
        + "\n"
    )

    import uvicorn

    config = uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_level=args.log_level, access_log=False
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        if not args.keep_db:
            print(f"\n[serve] dropping scratch db {db_name}")
            client = AsyncIOMotorClient(args.mongo_url)
            with contextlib.suppress(Exception):
                await client.drop_database(db_name)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--mongo-url", default="mongodb://localhost:27017")
    p.add_argument(
        "--redis-url", default=os.environ.get("POCKETPAW_REDIS_URL", "redis://localhost:6379")
    )
    p.add_argument("--executor", default="inprocess", choices=["inprocess", "arq"])
    p.add_argument("--db", default=None, help="scratch db name (default: random)")
    p.add_argument("--keep-db", action="store_true")
    p.add_argument("--seed-out", default="out/loadtest_seed.json")
    p.add_argument(
        "--pockets",
        type=int,
        default=32,
        help="distinct pockets to seed — one per concurrent virtual user",
    )
    p.add_argument("--log-level", default="warning")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[serve] stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
