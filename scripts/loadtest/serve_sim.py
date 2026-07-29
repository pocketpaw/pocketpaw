"""Boot the cloud stack against a chosen agent backend and seed a workspace, so
``chat_loadtest.py`` has something to hammer.

Created 2026-07-29 — the load-test rig's server half. It:

  1. Mints a local license + auth secret and points Beanie at a SCRATCH Mongo
     database (dropped on exit unless ``--keep-db``). The drop runs in a
     ``finally``, so a hard kill leaves the database behind — clean up with
     ``db.getMongo().getDBNames().filter(n => n.startsWith('loadtest_'))``.
  2. Selects the backend under test. Default ``--backend sim`` registers
     ``sim_backend.SimBackend``, so no provider key is needed and no tokens are
     spent. Any other value selects a backend already in the registry
     (``deep_agents``, ``claude_agent_sdk``, …) and then the run is real: it
     needs a working provider key in the environment and it spends money.
  3. Mounts the real cloud FastAPI app — real router, real run executor, real
     Redis stream transport, real Mongo writes, real SSE.
  4. Seeds user → workspace → agent → pocket over the real HTTP API.
  5. Serves it with uvicorn and prints the exact ``chat_loadtest.py`` command,
     with token / workspace / scope id already filled in.

Updated 2026-07-29 (same day) for the baseline measurement: added ``--backend``
and ``--model`` so a registered backend can be measured instead of the
simulator, and added the ``/__loadtest/metrics`` probe. The probe reports event
loop lag sampled from INSIDE this process, which is the number that decides the
in-process ceiling — a client-side measurement sees the network and the
driver's own loop instead, and would flatter the server.

What this measures: YOUR stack's concurrency ceiling. With ``--backend sim`` it
does NOT measure provider rate limits or the memory cost of a real Claude Code
CLI subprocess per run (set ``PAW_SIM_SUBPROC=1`` / ``PAW_SIM_RSS_MB`` to
approximate the latter).

Usage:
    uv run python scripts/loadtest/serve_sim.py --port 8099
    uv run python scripts/loadtest/serve_sim.py --backend deep_agents --pockets 250
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
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - optional dep
    psutil = None  # type: ignore[assignment]

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# A Windows console defaults to cp1252, which has no U+2192. Without this,
# --help dies in the encoder before printing a single option, because the
# module docstring below contains arrows.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

# Bounded so a long run cannot grow this without limit. At the 50ms probe
# interval below, 20k samples is ~17 minutes of history, and the driver drains
# it on every poll anyway.
_LAG_SAMPLES: deque[float] = deque(maxlen=20_000)


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
            "POCKETPAW_AGENT_BACKEND": args.backend,
            # Billing off: a load test should hit capacity limits, not the
            # 402 credit gate at run start.
            "POCKETPAW_BILLING_ENFORCED": "0",
            "POCKETPAW_CLOUD_RUN_EXECUTOR": args.executor,
        }
    )
    os.environ.pop("POCKETPAW_MEMORY_BACKEND", None)
    if args.redis_url:
        os.environ["POCKETPAW_REDIS_URL"] = args.redis_url
    if args.model:
        # Per-backend model attribute, not a single global. Omitting a backend
        # from _BACKEND_MODEL_ATTR silently drops the per-agent model, so set
        # the generic key too and let the backend read whichever it honours.
        os.environ["POCKETPAW_AGENT_MODEL"] = args.model
        os.environ[f"POCKETPAW_{args.backend.upper()}_MODEL"] = args.model
    return uri


async def _loop_lag_probe(interval: float = 0.05) -> None:
    """Record event-loop scheduling delay, sampled inside the server process.

    Sleep for a known interval and measure the overshoot. On an idle loop that
    is near zero; under load it is how long a ready callback waited behind
    other work, which is the thing that actually gives out first when the agent
    tier runs in-process rather than in a subprocess.
    """
    loop = asyncio.get_running_loop()
    while True:
        t0 = loop.time()
        await asyncio.sleep(interval)
        _LAG_SAMPLES.append(max(0.0, (loop.time() - t0 - interval) * 1000))


def _install_probe(app) -> None:
    """Expose the loop-lag samples and this process's RSS to the driver.

    Registered BEFORE ``mount_cloud`` so a catch-all route cannot shadow it.
    Reading drains the buffer, so each poll describes the window since the
    previous poll rather than the whole run to date.
    """

    @app.get("/__loadtest/metrics")
    async def _loadtest_metrics() -> dict:  # pyright: ignore[reportUnusedFunction]
        samples = sorted(_LAG_SAMPLES)
        _LAG_SAMPLES.clear()

        def pct(p: float) -> float | None:
            if not samples:
                return None
            idx = min(len(samples) - 1, int(round((p / 100) * (len(samples) - 1))))
            return round(samples[idx], 2)

        rss_mb = None
        if psutil is not None:
            with contextlib.suppress(Exception):
                rss_mb = round(psutil.Process().memory_info().rss / 1_048_576, 1)
        return {
            "loop_lag_p50_ms": pct(50),
            "loop_lag_p95_ms": pct(95),
            "loop_lag_max_ms": round(samples[-1], 2) if samples else None,
            "loop_lag_n": len(samples),
            "srv_rss_mb": rss_mb,
        }


async def _seed(app, base_url: str, n_pockets: int, backend: str) -> dict[str, str]:
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
                "name": f"Loadtest Agent ({backend})",
                "slug": f"loadtest-agent-{uuid.uuid4().hex[:6]}",
                "backend": backend,
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

            # Attach the loadtest agent, or a chat addressed to it comes back
            # 400 agent.not_in_scope — and an unaddressed one resolves to the
            # pocket's default agent, which is NOT the backend under test.
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

    from pocketpaw.agents.registry import _BACKEND_REGISTRY, register_backend

    if args.backend == "sim":
        register_backend("sim", "sim_backend", "SimBackend")
    elif args.backend not in _BACKEND_REGISTRY:
        known = ", ".join(sorted(_BACKEND_REGISTRY))
        raise SystemExit(f"[serve] unknown backend {args.backend!r}. Registered: {known}")
    else:
        print(
            f"[serve] REAL BACKEND {args.backend} — this spends provider tokens and "
            "needs a working key in the environment.",
            flush=True,
        )
    lic_mod._cached_license = None
    lic_mod._license_error = None

    print(f"[serve] mongo   {uri}")
    print(f"[serve] backend {args.backend}   model {args.model or '(backend default)'}")
    print(f"[serve] executor {args.executor}   redis {os.environ.get('POCKETPAW_REDIS_URL')}")
    await init_beanie(connection_string=uri, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    register_default_backend()

    app = FastAPI(title="pocketpaw loadtest rig")
    _install_probe(app)
    mount_cloud(app)

    print("[serve] seeding workspace…", flush=True)
    seed = await _seed(app, f"http://127.0.0.1:{args.port}", args.pockets, args.backend)
    seed["mongo_db"] = db_name
    seed["port"] = str(args.port)
    seed["backend"] = args.backend
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
    briefs = _HERE / "briefs" / "site-briefs.txt"
    briefs_rel = briefs.relative_to(Path.cwd()) if briefs.is_relative_to(Path.cwd()) else briefs
    print(
        "\n"
        + "=" * 78
        + "\nREADY — run this in another shell:\n\n"
        + f"""uv run python scripts/loadtest/chat_loadtest.py \\
    --base-url http://127.0.0.1:{args.port} \\
    --token {seed["token"]} \\
    --workspace {seed["workspace_id"]} \\
    --scope pocket --scope-id {seed["pocket_ids"]}{agent_flag} \\
    --surface sites --prompt-file {briefs_rel.as_posix()} \\
    --mode open --rate 1 --duration 60 --jitter \\
    --redis-url {os.environ.get("POCKETPAW_REDIS_URL", "redis://localhost:6379")} \\
    --mongo-url {args.mongo_url} --mongo-db {db_name} \\
    --out out/{args.backend}-1rps"""
        + "\n\n"
        + "=" * 78
        + "\n"
    )

    import uvicorn

    config = uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_level=args.log_level, access_log=False
    )
    server = uvicorn.Server(config)
    probe = asyncio.create_task(_loop_lag_probe())
    try:
        await server.serve()
    finally:
        probe.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await probe
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
    p.add_argument(
        "--backend",
        default="sim",
        help=(
            "agent backend under test. 'sim' (default) is the free simulator; "
            "anything else must already be in the registry (deep_agents, "
            "claude_agent_sdk, …) and needs a real provider key"
        ),
    )
    p.add_argument(
        "--model", default=None, help="model id for a real backend (default: the backend's own)"
    )
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
