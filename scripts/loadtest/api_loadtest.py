"""Step-load the whole cloud API (signup, auth, browse, writes, uploads, the
Paw Bar public routes, chat, and realtime sockets) and find the knee.

Chat throughput under a real agent backend is ``chat_loadtest.py``'s job; this
driver measures everything around it, plus a sim-backend chat scenario so chat
shows up next to the rest. Pair it with ``serve_sim.py``, which boots the real
cloud app against a scratch Mongo DB and exposes ``/__loadtest/metrics``.

How it works:

  * Scenarios live in ``SCENARIOS``: one async journey function + a weight.
    Adding one is one function and one registry line. ``--scenarios`` takes
    ``mixed`` (the request/response set), ``all`` (mixed + chat), or a list.
  * Virtual users (VUs) are persistent. Each stage in ``--stages`` raises the
    VU count; a VU loops weighted journeys until the run ends. ``realtime``
    VUs open one socket and hold it, so its stages are held-open counts.
    ``--mode soak`` holds ``--vus`` for ``--duration`` in ``--stage-seconds``
    windows, so memory growth shows up window by window.
  * Journeys other than ``signup`` run as a pool of pre-registered users, so
    browse numbers are not dominated by signup cost.
  * Every VU sends a stable synthetic ``X-Forwarded-For`` (10.x.y.z). The cloud
    limiters key on it, and serve_sim's uvicorn trusts it from 127.0.0.1, so
    each VU gets its own bucket the way distinct users would. ``--single-ip``
    sends ONE non-loopback address for everyone: that shows the per-IP policy
    ceiling. Omitting the header would read as genuine localhost instead.
  * Errors are split three ways: 429 = policy, 5xx/timeouts/connection errors
    = capacity, any other 4xx = rig (a payload bug, reported loudly). A stage
    fails on capacity errors > ``--max-error-pct`` or an endpoint p95 over
    ``--slo-p95-ms``; the ramp stops there and the knee is printed.
  * Per stage it polls the server probe (loop lag, RSS, pid), samples driver
    and server CPU with psutil, and optionally Mongo opcounters.

Latency uses ``time.perf_counter()`` (the Windows wall clock ticks at ~15.6ms)
and one shared ``httpx.AsyncClient`` (a client per request exhausts loopback
ports on Windows).

Usage:
    python scripts/loadtest/api_loadtest.py --seed out/loadtest_seed.json \\
        --scenarios mixed --stages 10,25,50,100,200 --stage-seconds 30 --out out/api-mixed
    python scripts/loadtest/api_loadtest.py --self-test
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import statistics
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from typing import Any

import httpx

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

try:
    import psutil
except ImportError:  # pragma: no cover - optional dep
    psutil = None  # type: ignore[assignment]

API = "/api/v1"
# One password for every synthetic account. The server's breached-password
# check caches by hash, so a shared password costs one lookup, not one per user.
PASSWORD = "Lt-Api-Load-7Qx!9z"
# Endpoints whose latency is long by design; they report but never trip the SLO.
SLO_EXEMPT = {"chat.run"}
MIN_N_FOR_SLO = 10


# --------------------------------------------------------------------------
# records and classification
# --------------------------------------------------------------------------


@dataclass
class Rec:
    """One request. ``ms`` is wall latency from send to full body read."""

    stage: int
    scenario: str
    endpoint: str
    status: int | None
    ms: float
    outcome: str  # ok | policy | capacity | rig
    bytes_in: int = 0
    bytes_out: int = 0
    t: float = 0.0
    err: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def classify(status: int | None, exc: BaseException | None = None) -> str:
    """429 is policy; 5xx, timeouts and dropped connections are capacity; any
    other 4xx means the rig sent something wrong."""
    if exc is not None or status is None:
        return "capacity"
    if status == 429:
        return "policy"
    if status >= 500:
        return "capacity"
    if status >= 400:
        return "rig"
    return "ok"


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
    return round(ordered[idx], 1)


# --------------------------------------------------------------------------
# run state shared by every VU
# --------------------------------------------------------------------------


@dataclass
class PoolUser:
    email: str
    token: str
    workspace_id: str
    pocket_id: str = ""
    agent_id: str = ""


class Run:
    def __init__(self, args: argparse.Namespace, seed: dict[str, Any]) -> None:
        self.args = args
        self.seed = seed
        self.base = args.base_url.rstrip("/")
        self.stage = 0
        self.records: list[Rec] = []
        self.pool: list[PoolUser] = []
        self.stop = asyncio.Event()
        # A pocket runs one chat at a time: a new run supersedes the previous
        # one in the same scope, so two VUs sharing a pocket measure truncation.
        self.chat_pockets: asyncio.Queue[str] = asyncio.Queue()
        for pid in [p for p in str(seed.get("pocket_ids", "")).split(",") if p]:
            self.chat_pockets.put_nowait(pid)
        self.payload = (b" " * 1023 + b"\n") * max(1, args.upload_kb)
        jar = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
        # Shared across VUs, so it must never store a cookie: a paw_auth cookie
        # from one user's login would ride along on every other user's request.
        limits = httpx.Limits(max_connections=4000, max_keepalive_connections=2000)
        self.http = httpx.AsyncClient(
            limits=limits, timeout=httpx.Timeout(args.timeout), cookies=jar
        )
        self.chat_http: httpx.AsyncClient | None = None
        self.ws_open = 0

    def ip(self, vu: int) -> str:
        if self.args.single_ip:
            return "10.0.0.1"
        return f"10.{(vu >> 16) & 255}.{(vu >> 8) & 255}.{(vu & 255) or 1}"

    def headers(self, vu: int, user: PoolUser | None = None) -> dict[str, str]:
        h = {"X-Forwarded-For": self.ip(vu)}
        if user is not None:
            h["Authorization"] = f"Bearer {user.token}"
            h["X-Workspace-Id"] = user.workspace_id
        return h

    async def call(
        self,
        scenario: str,
        endpoint: str,
        method: str,
        path: str,
        vu: int,
        user: PoolUser | None = None,
        *,
        bytes_out: int = 0,
        **kw: Any,
    ) -> httpx.Response | None:
        """Send one request, record it, and return the response (None on a
        transport error)."""
        headers = {**self.headers(vu, user), **kw.pop("headers", {})}
        stage = self.stage
        t0 = time.perf_counter()
        try:
            resp = await self.http.request(method, self.base + path, headers=headers, **kw)
            ms = (time.perf_counter() - t0) * 1000
        except Exception as exc:  # noqa: BLE001 - a load driver must never die
            ms = (time.perf_counter() - t0) * 1000
            self.records.append(
                Rec(
                    stage,
                    scenario,
                    endpoint,
                    None,
                    ms,
                    "capacity",
                    0,
                    bytes_out,
                    time.time(),
                    f"{type(exc).__name__}: {exc}"[:300],
                )
            )
            return None
        outcome = classify(resp.status_code)
        self.records.append(
            Rec(
                stage,
                scenario,
                endpoint,
                resp.status_code,
                ms,
                outcome,
                len(resp.content),
                bytes_out,
                time.time(),
                None if outcome == "ok" else resp.text[:300],
            )
        )
        return resp

    def user_for(self, vu: int) -> PoolUser:
        return self.pool[vu % len(self.pool)]


# --------------------------------------------------------------------------
# account helpers (used by signup and by pool setup)
# --------------------------------------------------------------------------


async def create_account(run: Run, vu: int, scenario: str) -> PoolUser | None:
    """register -> bearer login -> workspace -> set-active. Returns None if any
    step failed (the failure is already recorded)."""
    email = f"lt-{uuid.uuid4().hex[:12]}@loadtest.example"
    r = await run.call(
        scenario,
        "auth.register",
        "POST",
        f"{API}/auth/register",
        vu,
        json={"email": email, "password": PASSWORD, "full_name": "Load Test"},
    )
    if r is None or r.status_code != 201:
        return None
    r = await run.call(
        scenario,
        "auth.login",
        "POST",
        f"{API}/auth/bearer/login",
        vu,
        data={"username": email, "password": PASSWORD},
    )
    if r is None or r.status_code != 200:
        return None
    user = PoolUser(email=email, token=r.json()["access_token"], workspace_id="")
    r = await run.call(
        scenario,
        "workspace.create",
        "POST",
        f"{API}/workspaces",
        vu,
        user,
        json={"name": "LT WS", "slug": f"lt-{uuid.uuid4().hex[:10]}"},
    )
    if r is None or r.status_code not in (200, 201):
        return None
    user.workspace_id = r.json()["_id"]
    r = await run.call(
        scenario,
        "auth.set_active",
        "POST",
        f"{API}/auth/set-active-workspace",
        vu,
        user,
        json={"workspace_id": user.workspace_id},
    )
    if r is None or r.status_code >= 400:
        return None
    return user


def _id(payload: dict[str, Any]) -> str:
    return str(payload.get("_id") or payload.get("id") or "")


async def setup_pool(run: Run, n: int) -> None:
    """Pre-register ``n`` users, each with a workspace, a pocket and an agent.
    Setup rows are recorded under stage -1 and excluded from stage stats."""
    run.stage = -1

    async def one(i: int) -> PoolUser | None:
        user = await create_account(run, 100_000 + i, "setup")
        if user is None:
            return None
        r = await run.call(
            "setup",
            "pocket.create",
            "POST",
            f"{API}/pockets",
            100_000 + i,
            user,
            json={"name": "LT pool pocket"},
        )
        if r is not None and r.status_code in (200, 201):
            user.pocket_id = _id(r.json())
        r = await run.call(
            "setup",
            "agent.create",
            "POST",
            f"{API}/agents",
            100_000 + i,
            user,
            json={
                "name": "LT agent",
                "slug": f"lt-agent-{uuid.uuid4().hex[:8]}",
                "backend": "sim",
                "system_prompt": "Load test.",
            },
        )
        if r is not None and r.status_code in (200, 201):
            user.agent_id = _id(r.json())
        return user

    users = await asyncio.gather(*[one(i) for i in range(n)])
    run.pool = [u for u in users if u is not None and u.pocket_id]
    if not run.pool:
        bad = [r for r in run.records if r.outcome != "ok"][:3]
        raise SystemExit(
            f"[driver] pool setup failed: {[(b.endpoint, b.status, b.err) for b in bad]}"
        )
    print(f"[driver] pool ready: {len(run.pool)}/{n} users")


# --------------------------------------------------------------------------
# scenarios — one journey per call. Register new ones in SCENARIOS.
# --------------------------------------------------------------------------


async def sc_signup(run: Run, vu: int) -> None:
    await create_account(run, vu, "signup")


async def sc_browse(run: Run, vu: int) -> None:
    u = run.user_for(vu)
    await run.call("browse", "auth.me", "GET", f"{API}/auth/me", vu, u)
    await run.call("browse", "pockets.list", "GET", f"{API}/pockets", vu, u)
    await run.call("browse", "pockets.get", "GET", f"{API}/pockets/{u.pocket_id}", vu, u)
    await run.call("browse", "files.list", "GET", f"{API}/files", vu, u)


async def sc_read(run: Run, vu: int) -> None:
    """The rest of the everyday read surface, one call per journey so the mix
    stays proportional."""
    u = run.user_for(vu)
    choices: list[tuple[str, str]] = [
        ("agents.list", f"{API}/agents"),
        ("sessions.list", f"{API}/sessions"),
        ("notifications.list", f"{API}/notifications"),
        ("notifications.unread", f"{API}/notifications/unread-count"),
        ("workspace.members", f"{API}/workspaces/{u.workspace_id}/members"),
        ("chat.groups", f"{API}/chat/groups"),
        ("chat.unreads", f"{API}/chat/unreads"),
    ]
    if u.agent_id:
        choices.append(("agents.get", f"{API}/agents/{u.agent_id}"))
    endpoint, path = random.choice(choices)
    await run.call("read", endpoint, "GET", path, vu, u)


async def sc_write(run: Run, vu: int) -> None:
    u = run.user_for(vu)
    r = await run.call(
        "write", "pockets.create", "POST", f"{API}/pockets", vu, u, json={"name": f"LT write {vu}"}
    )
    if r is None or r.status_code not in (200, 201):
        return
    pid = _id(r.json())
    await run.call(
        "write",
        "pockets.update",
        "PATCH",
        f"{API}/pockets/{pid}",
        vu,
        u,
        json={"name": f"LT write {vu} renamed"},
    )
    await run.call("write", "pockets.delete", "DELETE", f"{API}/pockets/{pid}", vu, u)


async def sc_upload(run: Run, vu: int) -> None:
    """Upload, download, delete. The payload is whitespace on purpose: text
    extraction comes back empty, so the upload listener skips KB ingest (an LLM
    compile). Bytes still cross the full multipart + storage path."""
    u = run.user_for(vu)
    body = run.payload
    r = await run.call(
        "upload",
        "uploads.create",
        "POST",
        f"{API}/uploads",
        vu,
        u,
        bytes_out=len(body),
        files={"files": ("loadtest.txt", body, "text/plain")},
    )
    if r is None or r.status_code != 200:
        return
    uploaded = r.json().get("uploaded") or []
    if not uploaded:
        run.records[-1].outcome = "rig"
        run.records[-1].err = f"nothing uploaded: {r.text[:200]}"
        return
    fid = uploaded[0]["id"]
    await run.call("upload", "uploads.download", "GET", f"{API}/uploads/{fid}", vu, u)
    await run.call("upload", "uploads.delete", "DELETE", f"{API}/uploads/{fid}", vu, u)


async def sc_pawbar(run: Run, vu: int) -> None:
    """The public widget surface a site visitor's browser hits. No auth."""
    wid = run.seed.get("widget_id")
    if not wid:
        return
    await run.call("pawbar", "pawbar.widget_js", "GET", f"{API}/paw-bar/widget.js", vu)
    await run.call("pawbar", "pawbar.spec", "GET", f"{API}/paw-bar/spec/{wid}", vu)
    await run.call(
        "pawbar",
        "pawbar.event",
        "POST",
        f"{API}/paw-bar/events/{wid}",
        vu,
        json={"type": "page_view", "payload": {"path": "/"}, "customer_ref": f"ltvisitor{vu:06d}"},
    )


async def sc_chat(run: Run, vu: int) -> None:
    """One sim-backend agent run through ``chat_loadtest.drive_one``, on a pocket
    no other VU is using."""
    from chat_loadtest import drive_one

    if run.chat_http is None:
        return
    stage = run.stage
    pid = await run.chat_pockets.get()
    try:
        body: dict[str, Any] = {
            "content": f"Load test message from VU {vu}.",
            "client_message_id": f"lt-{uuid.uuid4().hex}",
        }
        if run.seed.get("agent_id"):
            body["agent_id"] = run.seed["agent_id"]
        rec = await drive_one(
            run.chat_http,
            seq=vu,
            url=f"{run.base}{API}/cloud/chat/pocket/{pid}/agent",
            body=body,
            timeout=run.args.chat_timeout,
            inflight={"n": 0},
        )
    finally:
        run.chat_pockets.put_nowait(pid)
    if rec.outcome == "completed":
        outcome = "ok"
    elif rec.outcome.startswith("http_"):
        outcome = classify(rec.http_status)
    else:  # error / interrupted / truncated / timeout / conn_error
        outcome = "capacity"
    extra = {"ttft_ms": rec.ttft_ms, "text_chars": rec.text_chars, "chat_outcome": rec.outcome}
    now = time.time()
    run.records.append(
        Rec(
            stage,
            "chat",
            "chat.accept",
            rec.http_status,
            rec.accept_ms or rec.total_ms or 0.0,
            "ok" if rec.http_status == 200 else outcome,
            t=now,
        )
    )
    run.records.append(
        Rec(
            stage,
            "chat",
            "chat.run",
            rec.http_status,
            rec.total_ms or 0.0,
            outcome,
            t=now,
            err=rec.error_message or rec.error_code,
            extra=extra,
        )
    )


async def sc_realtime(run: Run, vu: int) -> None:
    """Mint a ticket, open /ws/cloud, and hold it until the run ends. Records
    the ticket call and the handshake; each VU is one online user."""
    import websockets

    # Spread this stage's new connects over the first half of the stage. All at
    # once measures a reconnect storm (every client back after a deploy), not
    # how many users can stay online.
    await asyncio.sleep(random.uniform(0, run.args.stage_seconds / 2))
    u = run.user_for(vu)
    r = await run.call("realtime", "ws.ticket", "POST", f"{API}/auth/ws/ticket", vu, u)
    if r is None or r.status_code != 200:
        await run.stop.wait()
        return
    ticket = r.json()["ticket"]
    url = run.base.replace("http", "ws", 1) + f"/ws/cloud?token={ticket}"
    stage = run.stage
    t0 = time.perf_counter()
    try:
        ws = await asyncio.wait_for(
            websockets.connect(
                url,
                additional_headers={"X-Forwarded-For": run.ip(vu)},
                open_timeout=30,
                max_queue=64,
            ),
            timeout=35,
        )
    except Exception as exc:  # noqa: BLE001
        run.records.append(
            Rec(
                stage,
                "realtime",
                "ws.connect",
                None,
                (time.perf_counter() - t0) * 1000,
                "capacity",
                t=time.time(),
                err=f"{type(exc).__name__}: {exc}"[:300],
            )
        )
        await run.stop.wait()
        return
    run.records.append(
        Rec(
            stage,
            "realtime",
            "ws.connect",
            101,
            (time.perf_counter() - t0) * 1000,
            "ok",
            t=time.time(),
        )
    )
    run.ws_open += 1
    try:
        reader = asyncio.create_task(_drain(ws))
        await run.stop.wait()
        reader.cancel()
        with contextlib.suppress(BaseException):
            await reader
    finally:
        run.ws_open -= 1
        with contextlib.suppress(Exception):
            await ws.close()


async def _drain(ws: Any) -> None:
    """Keep reading so pings are answered and a server-side close is seen."""
    with contextlib.suppress(Exception):
        async for _ in ws:
            pass


@dataclass
class Scenario:
    fn: Callable[[Run, int], Awaitable[None]]
    weight: float
    held: bool = False  # one long-lived iteration per VU (realtime)


# Weights model a real day: reads dwarf writes, signups and uploads are rare.
SCENARIOS: dict[str, Scenario] = {
    "signup": Scenario(sc_signup, 1),
    "browse": Scenario(sc_browse, 6),
    "read": Scenario(sc_read, 10),
    "write": Scenario(sc_write, 1),
    "upload": Scenario(sc_upload, 1),
    "pawbar": Scenario(sc_pawbar, 3),
    "chat": Scenario(sc_chat, 1),
    "realtime": Scenario(sc_realtime, 1, held=True),
}
MIXED = ["signup", "browse", "read", "write", "upload", "pawbar"]


def parse_scenarios(spec: str) -> list[str]:
    if spec == "mixed":
        names = list(MIXED)
    elif spec == "all":
        names = [*MIXED, "chat"]
    else:
        names = [s.strip() for s in spec.split(",") if s.strip()]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s) {unknown}; known: {', '.join(SCENARIOS)}")
    held = [n for n in names if SCENARIOS[n].held]
    if held and len(names) > 1:
        raise SystemExit(f"{held} holds a connection per VU; run it on its own")
    return names


# --------------------------------------------------------------------------
# sampling
# --------------------------------------------------------------------------


class Sampler:
    """Per-second CPU samples for the driver, the server and the box, plus the
    server probe and Mongo opcounters polled at stage boundaries."""

    def __init__(self, run: Run, mongo_url: str | None) -> None:
        self.run = run
        self.rows: list[dict[str, Any]] = []
        self.srv_proc = None
        self.mongo = None
        self.mongo_url = mongo_url
        self.me = psutil.Process() if psutil else None

    async def probe(self) -> dict[str, Any]:
        try:
            r = await self.run.http.get(f"{self.run.base}/__loadtest/metrics", timeout=10)
            data = r.json()
        except Exception:  # noqa: BLE001
            return {}
        if psutil and self.srv_proc is None and data.get("pid"):
            with contextlib.suppress(Exception):
                self.srv_proc = psutil.Process(int(data["pid"]))
                self.srv_proc.cpu_percent(None)
        return data

    async def opcounters(self) -> dict[str, int]:
        if not self.mongo_url:
            return {}
        try:
            if self.mongo is None:
                from motor.motor_asyncio import AsyncIOMotorClient

                self.mongo = AsyncIOMotorClient(self.mongo_url).admin
            status = await self.mongo.command("serverStatus")
            return {k: int(v) for k, v in status["opcounters"].items()}
        except Exception:  # noqa: BLE001
            return {}

    async def loop(self) -> None:
        if not psutil:
            return
        self.me.cpu_percent(None)
        psutil.cpu_percent(None)
        while not self.run.stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.run.stop.wait(), timeout=1.0)
            row = {
                "stage": self.run.stage,
                "driver_cpu": self.me.cpu_percent(None),
                "sys_cpu": psutil.cpu_percent(None),
            }
            if self.srv_proc is not None:
                with contextlib.suppress(Exception):
                    row["srv_cpu"] = self.srv_proc.cpu_percent(None)
            self.rows.append(row)


# --------------------------------------------------------------------------
# stage stats and the knee
# --------------------------------------------------------------------------


def stage_stats(recs: list[Rec], seconds: float) -> dict[str, dict[str, Any]]:
    by: dict[str, list[Rec]] = {}
    for r in recs:
        by.setdefault(r.endpoint, []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for ep, rows in sorted(by.items()):
        ok = [r.ms for r in rows if r.outcome == "ok"]
        counts = {
            k: sum(1 for r in rows if r.outcome == k) for k in ("ok", "policy", "capacity", "rig")
        }
        entry: dict[str, Any] = {
            "n": len(rows),
            "rps": round(len(rows) / seconds, 2),
            "ok_rps": round(counts["ok"] / seconds, 2),
            "p50": pct(ok, 50),
            "p95": pct(ok, 95),
            "p99": pct(ok, 99),
            **counts,
            "mb_in": round(sum(r.bytes_in for r in rows) / 1_048_576, 2),
            "mb_out": round(sum(r.bytes_out for r in rows) / 1_048_576, 2),
        }
        ttft = [r.extra["ttft_ms"] for r in rows if r.extra.get("ttft_ms") is not None]
        if ttft:
            entry["ttft_p50"] = pct(ttft, 50)
            entry["ttft_p95"] = pct(ttft, 95)
        if any("text_chars" in r.extra for r in rows):
            entry["empty_runs"] = sum(
                1 for r in rows if r.outcome == "ok" and not r.extra.get("text_chars")
            )
        errs = list(dict.fromkeys(r.err for r in rows if r.err))[:2]
        if errs:
            entry["sample_errors"] = errs
        out[ep] = entry
    return out


def judge(stats: dict[str, dict[str, Any]], slo_ms: float, max_err_pct: float) -> list[str]:
    """Return why a stage failed; an empty list means it was healthy."""
    total = sum(s["n"] for s in stats.values())
    cap = sum(s["capacity"] for s in stats.values())
    reasons: list[str] = []
    if total and 100 * cap / total > max_err_pct:
        worst = max(stats.items(), key=lambda kv: kv[1]["capacity"])[0]
        reasons.append(f"capacity errors {100 * cap / total:.1f}% (worst: {worst})")
    for ep, s in stats.items():
        if ep in SLO_EXEMPT or s["ok"] < MIN_N_FOR_SLO or s["p95"] is None:
            continue
        if s["p95"] > slo_ms:
            reasons.append(f"{ep} p95 {s['p95']}ms > {slo_ms:.0f}ms")
    return reasons


def find_knee(stages: list[dict[str, Any]]) -> dict[str, Any]:
    """Last healthy stage and the first failing one (with its reasons)."""
    healthy = None
    for st in stages:
        if st["fail_reasons"]:
            return {"last_healthy": healthy, "first_failing": st}
        healthy = st
    return {"last_healthy": healthy, "first_failing": None}


# --------------------------------------------------------------------------
# the ramp
# --------------------------------------------------------------------------


async def vu_loop(run: Run, vu: int, names: list[str], weights: list[float]) -> None:
    if SCENARIOS[names[0]].held:
        await SCENARIOS[names[0]].fn(run, vu)
        return
    while not run.stop.is_set():
        name = random.choices(names, weights)[0]
        try:
            await SCENARIOS[name].fn(run, vu)
        except Exception as exc:  # noqa: BLE001 - one broken journey must not end the VU
            run.records.append(
                Rec(
                    run.stage,
                    name,
                    f"{name}.journey",
                    None,
                    0.0,
                    "rig",
                    t=time.time(),
                    err=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
            await asyncio.sleep(0.5)


async def ramp(run: Run, names: list[str], plan: list[int], sampler: Sampler) -> list[dict]:
    weights = [SCENARIOS[n].weight for n in names]
    tasks: list[asyncio.Task] = []
    stages: list[dict[str, Any]] = []
    await sampler.probe()  # drain the lag buffer so stage 0 starts clean
    base_rss = (await sampler.probe()).get("srv_rss_mb")
    ops_prev = await sampler.opcounters()
    for i, vus in enumerate(plan):
        run.stage = i
        while len(tasks) < vus:
            tasks.append(asyncio.create_task(vu_loop(run, len(tasks), names, weights)))
        t0 = time.perf_counter()
        await asyncio.sleep(run.args.stage_seconds)
        secs = time.perf_counter() - t0
        probe = await sampler.probe()
        ops = await sampler.opcounters()
        recs = [r for r in run.records if r.stage == i]
        stats = stage_stats(recs, secs)
        cpu = [r for r in sampler.rows if r["stage"] == i]
        st: dict[str, Any] = {
            "stage": i,
            "vus": vus,
            "seconds": round(secs, 1),
            "total_rps": round(len(recs) / secs, 1),
            "endpoints": stats,
            "loop_lag_p99_ms": probe.get("loop_lag_p99_ms"),
            "loop_lag_max_ms": probe.get("loop_lag_max_ms"),
            "srv_rss_mb": probe.get("srv_rss_mb"),
            "driver_cpu_pct": _mean([r["driver_cpu"] for r in cpu]),
            "srv_cpu_pct": _mean([r["srv_cpu"] for r in cpu if "srv_cpu" in r]),
            "sys_cpu_pct": _mean([r["sys_cpu"] for r in cpu]),
        }
        if ops and ops_prev:
            st["mongo_ops_per_s"] = {k: round((ops[k] - ops_prev.get(k, 0)) / secs, 1) for k in ops}
        ops_prev = ops
        if SCENARIOS[names[0]].held:
            st["ws_open"] = run.ws_open
            if base_rss and st["srv_rss_mb"] and run.ws_open:
                st["rss_mb_per_1k_sockets"] = round(
                    (st["srv_rss_mb"] - base_rss) / run.ws_open * 1000, 1
                )
        st["fail_reasons"] = (
            []
            if run.args.mode == "soak"
            else judge(stats, run.args.slo_p95_ms, run.args.max_error_pct)
        )
        stages.append(st)
        print(_stage_line(st), flush=True)
        if st["fail_reasons"]:
            print(f"[driver] stopping ramp: {'; '.join(st['fail_reasons'])}", flush=True)
            break
    run.stage = len(plan)  # anything still in flight lands outside every stage
    run.stop.set()
    _, pending = await asyncio.wait(tasks, timeout=max(30.0, run.args.chat_timeout))
    for t in pending:
        t.cancel()
    return stages


def _mean(vals: list[float]) -> float | None:
    return round(statistics.fmean(vals), 1) if vals else None


def _stage_line(st: dict[str, Any]) -> str:
    eps = st["endpoints"]
    p95s = [s["p95"] for e, s in eps.items() if s["p95"] is not None and e not in SLO_EXEMPT]
    pol = sum(s["policy"] for s in eps.values())
    cap = sum(s["capacity"] for s in eps.values())
    rig = sum(s["rig"] for s in eps.values())
    line = (
        f"[stage {st['stage']}] vus={st['vus']:<5} rps={st['total_rps']:<8} "
        f"worst_p95={max(p95s) if p95s else '-':<8} 429={pol:<5} cap_err={cap:<5} rig={rig:<4} "
        f"lag_p99={st['loop_lag_p99_ms']}ms rss={st['srv_rss_mb']}MB "
        f"cpu srv/drv={st['srv_cpu_pct']}/{st['driver_cpu_pct']}%"
    )
    if "ws_open" in st:
        line += f" ws_open={st['ws_open']} rss/1k={st.get('rss_mb_per_1k_sockets')}MB"
    if rig:
        line += "  <-- RIG ERRORS: check sample_errors in stages.json"
    return line


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def summarize(args: argparse.Namespace, names: list[str], stages: list[dict[str, Any]]) -> str:
    knee = find_knee(stages)
    lines = [
        f"# API load test: {', '.join(names)}",
        "",
        f"mode={args.mode} single_ip={args.single_ip} upload_kb={args.upload_kb} "
        f"slo_p95={args.slo_p95_ms}ms stage={args.stage_seconds}s "
        f"at {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        "| stage | VUs | rps | worst p95 ms | 429 | capacity err | rig err | loop lag p99 ms "
        "| srv RSS MB | srv CPU % | driver CPU % |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for st in stages:
        eps = st["endpoints"]
        p95s = [s["p95"] for e, s in eps.items() if s["p95"] is not None and e not in SLO_EXEMPT]
        pol, cap, rig = (sum(s[k] for s in eps.values()) for k in ("policy", "capacity", "rig"))
        lines.append(
            f"| {st['stage']} | {st['vus']} | {st['total_rps']} | {max(p95s) if p95s else '-'} "
            f"| {pol} | {cap} | {rig} | {st['loop_lag_p99_ms']} | {st['srv_rss_mb']} "
            f"| {st['srv_cpu_pct']} | {st['driver_cpu_pct']} |"
        )
    lines += ["", "## Knee", ""]
    lh, ff = knee["last_healthy"], knee["first_failing"]
    lines.append(
        f"- last healthy stage: {lh['vus']} VUs at {lh['total_rps']} rps"
        if lh
        else "- no healthy stage"
    )
    lines.append(
        f"- first failing: {ff['vus']} VUs: {'; '.join(ff['fail_reasons'])}"
        if ff
        else "- no stage failed; raise --stages"
    )
    drv = [st["driver_cpu_pct"] for st in stages if st["driver_cpu_pct"] is not None]
    if drv and max(drv) > 85:
        lines.append(
            f"- WARNING: driver CPU peaked at {max(drv)}% of a core; the driver may have "
            "saturated before the server did"
        )
    lines += ["", "## Per endpoint, per stage", ""]
    lines.append(
        "| stage | endpoint | n | rps | p50 | p95 | p99 | 429 | cap | rig | MB in | MB out |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for st in stages:
        for ep, s in st["endpoints"].items():
            lines.append(
                f"| {st['stage']} | {ep} | {s['n']} | {s['rps']} | {s['p50']} | {s['p95']} | "
                f"{s['p99']} | {s['policy']} | {s['capacity']} | {s['rig']} | {s['mb_in']} | "
                f"{s['mb_out']} |"
            )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", default="http://127.0.0.1:8099")
    p.add_argument("--seed", default="out/loadtest_seed.json", help="serve_sim's --seed-out file")
    p.add_argument("--scenarios", default="mixed", help="mixed | all | comma list")
    p.add_argument("--mode", default="ramp", choices=["ramp", "soak"])
    p.add_argument("--stages", default="10,25,50,100,200", help="ramp: VUs per stage")
    p.add_argument("--stage-seconds", type=float, default=30, help="stage length (soak: window)")
    p.add_argument("--vus", type=int, default=50, help="soak: fixed VUs")
    p.add_argument("--duration", type=float, default=300, help="soak: total seconds")
    p.add_argument("--slo-p95-ms", type=float, default=1000)
    p.add_argument("--max-error-pct", type=float, default=5.0)
    p.add_argument("--pool-users", type=int, default=16)
    p.add_argument("--upload-kb", type=int, default=256, help="upload payload size in KB")
    p.add_argument("--single-ip", action="store_true", help="every VU shares one client IP")
    p.add_argument("--timeout", type=float, default=30, help="per-request timeout (s)")
    p.add_argument("--chat-timeout", type=float, default=120, help="per chat run (s)")
    p.add_argument("--mongo-url", default=None, help="sample serverStatus opcounters")
    p.add_argument("--out", default=None)
    p.add_argument("--self-test", action="store_true")
    return p


async def main_async(args: argparse.Namespace) -> int:
    names = parse_scenarios(args.scenarios)
    seed = json.loads(Path(args.seed).read_text("utf-8"))
    if args.mode == "soak":
        n = max(1, int(round(args.duration / args.stage_seconds)))
        plan = [args.vus] * n
    else:
        plan = [int(x) for x in args.stages.split(",") if x.strip()]
    out = Path(args.out or f"out/api-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}")
    out.mkdir(parents=True, exist_ok=True)

    run = Run(args, seed)
    if "chat" in names:
        n_pockets = run.chat_pockets.qsize()
        print(f"[driver] chat: {n_pockets} pockets, one run per pocket at a time")
        run.chat_http = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {seed['token']}",
                "X-Workspace-Id": seed["workspace_id"],
                "Accept": "text/event-stream",
                "X-Forwarded-For": "10.250.0.1",
            },
            limits=httpx.Limits(max_connections=2000, max_keepalive_connections=500),
            timeout=None,
        )
    if any(n not in ("signup", "pawbar") for n in names):
        await setup_pool(run, args.pool_users)
    sampler = Sampler(run, args.mongo_url)
    sampler_task = asyncio.create_task(sampler.loop())
    print(f"[driver] {names} plan={plan} x {args.stage_seconds}s single_ip={args.single_ip}")

    stages = await ramp(run, names, plan, sampler)
    with contextlib.suppress(BaseException):
        await sampler_task
    await run.http.aclose()
    if run.chat_http:
        await run.chat_http.aclose()

    with (out / "requests.jsonl").open("w", encoding="utf-8") as fh:
        for r in run.records:
            fh.write(json.dumps(asdict(r)) + "\n")
    (out / "stages.json").write_text(json.dumps(stages, indent=2), encoding="utf-8")
    report = summarize(args, names, stages)
    (out / "summary.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"[driver] wrote {out}/requests.jsonl, stages.json, summary.md")
    return 0


def self_test() -> int:
    assert pct([], 95) is None
    assert pct([5.0], 99) == 5.0
    assert pct(list(map(float, range(1, 101))), 50) == 51.0  # round-half-even index 49.5 -> 50
    assert pct(list(map(float, range(1, 101))), 95) == 95.0
    assert classify(200) == "ok" and classify(204) == "ok"
    assert classify(429) == "policy"
    assert classify(503) == "capacity" and classify(None) == "capacity"
    assert classify(200, TimeoutError()) == "capacity"
    assert classify(404) == "rig" and classify(403) == "rig"

    def recs(ep: str, n: int, ms: float, outcome: str = "ok") -> list[Rec]:
        return [Rec(0, "x", ep, 200, ms, outcome) for _ in range(n)]

    good = stage_stats(recs("a", 50, 100.0), 10.0)
    assert good["a"]["rps"] == 5.0 and good["a"]["p95"] == 100.0
    assert judge(good, 1000, 5) == []
    slow = stage_stats(recs("a", 50, 1500.0), 10.0)
    assert judge(slow, 1000, 5) == ["a p95 1500.0ms > 1000ms"]
    # 429s are policy, not capacity: they never fail a stage on their own.
    limited = stage_stats(recs("a", 50, 10.0) + recs("a", 50, 1.0, "policy"), 10.0)
    assert judge(limited, 1000, 5) == []
    broken = stage_stats(recs("a", 90, 10.0) + recs("a", 10, 1.0, "capacity"), 10.0)
    assert judge(broken, 1000, 5)[0].startswith("capacity errors 10.0%")
    # Long-by-design endpoints never trip the SLO; tiny samples do not either.
    assert judge(stage_stats(recs("chat.run", 50, 9000.0), 10.0), 1000, 5) == []
    assert judge(stage_stats(recs("a", 3, 9000.0), 10.0), 1000, 5) == []

    st = [{"vus": v, "fail_reasons": r} for v, r in ((10, []), (25, []), (50, ["x"]))]
    k = find_knee(st)
    assert k["last_healthy"]["vus"] == 25 and k["first_failing"]["vus"] == 50
    assert find_knee(st[:2])["first_failing"] is None
    assert find_knee([{"vus": 10, "fail_reasons": ["x"]}])["last_healthy"] is None
    assert parse_scenarios("all") == [*MIXED, "chat"]
    print("self-test ok")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        return self_test()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[driver] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
