"""Stress-test the cloud chat agent path (/sites, /code, or any surface) and
record end-to-end latency + host saturation metrics side by side.

Created 2026-07-29 — capacity harness for the Claude-Agent-SDK-backed chat
path. What it does:

  * Drives ``POST /api/v1/cloud/chat/{scope}/{scope_id}/agent`` — the real
    surface the client uses — in either OPEN loop (fixed arrival rate, the right
    shape for finding a capacity ceiling) or CLOSED loop (fixed concurrent
    virtual users, the right shape for measuring steady-state latency).
  * Consumes each run's SSE stream and times the frames that matter:
    accept (POST headers), ``message.persisted``, first ``text`` token,
    first ``tool_start``, and the terminal frame
    (``stream_end`` / ``error`` / ``interrupted``).
  * Samples the HOST in parallel on a fixed interval — Claude Code CLI
    subprocess count and RSS, web-process RSS/CPU, node/bun build processes
    (site publish), system CPU/mem, agent-jail disk, arq queue depth (Redis),
    and ChatRunDoc status counts (Mongo) — so a latency cliff can be attributed
    to a specific resource rather than guessed at.
  * Writes ``requests.jsonl`` + ``samples.csv`` and prints a percentile summary
    with an error breakdown and a saturation verdict.

Run it against a deploy you are allowed to saturate. It costs real tokens.

Usage:
    uv run python scripts/loadtest/chat_loadtest.py \
        --base-url https://api.example.com \
        --token "$PAW_TOKEN" --workspace "$PAW_WORKSPACE" \
        --scope pocket --scope-id 68f... --surface sites \
        --prompt "Build a landing page for a dentist in Austin" \
        --mode open --rate 0.5 --duration 300 \
        --out out/sites-open-0.5rps

Optional host sampling (run ON the app box, or point at its Redis/Mongo):
    --redis-url redis://localhost:6379/0 --mongo-url mongodb://localhost:27017 \
    --mongo-db pocketpaw --jail-root ~/.pocketpaw/workspaces
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import os
import random
import shutil
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

try:  # host sampling is optional — the driver still works without it
    import psutil
except ImportError:  # pragma: no cover - optional dep
    psutil = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
# per-request record
# --------------------------------------------------------------------------


@dataclass
class RequestRecord:
    """One agent run, from POST to terminal frame. All times in ms."""

    seq: int
    started_at: float
    accept_ms: float | None = None
    persisted_ms: float | None = None
    ttft_ms: float | None = None
    first_tool_ms: float | None = None
    total_ms: float | None = None
    http_status: int | None = None
    run_id: str | None = None
    outcome: str = "pending"
    error_code: str | None = None
    error_message: str | None = None
    frames: dict[str, int] = field(default_factory=dict)
    text_chars: int = 0
    usage: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# SSE parsing
# --------------------------------------------------------------------------


async def _iter_sse(response: httpx.Response):
    """Yield ``(event, data)`` pairs from a text/event-stream response."""
    event: str | None = None
    async for raw in response.aiter_lines():
        line = raw.rstrip("\r")
        if not line:
            event = None
            continue
        if line.startswith(":"):  # heartbeat
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            payload = line[5:].strip()
            if event is None:
                continue
            try:
                data = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                data = {"_raw": payload}
            yield event, data


TERMINAL = {"stream_end", "error", "interrupted"}


async def drive_one(
    client: httpx.AsyncClient,
    *,
    seq: int,
    url: str,
    body: dict[str, Any],
    timeout: float,
    inflight: dict[str, int],
) -> RequestRecord:
    """Fire one agent run and time every frame that matters.

    ``timeout`` is a WALL-CLOCK deadline, enforced with ``asyncio.timeout``.
    It cannot be an httpx timeout: the SSE stream sends ``: ping`` heartbeats,
    and an httpx read timeout resets on every byte — so a run that hangs
    forever would keep the connection alive and never trip it.
    """
    rec = RequestRecord(seq=seq, started_at=time.time())
    t0 = time.perf_counter()
    inflight["n"] += 1
    try:
        async with asyncio.timeout(timeout), client.stream("POST", url, json=body) as resp:
            rec.accept_ms = (time.perf_counter() - t0) * 1000
            rec.http_status = resp.status_code
            if resp.status_code != 200:
                await resp.aread()
                rec.outcome = f"http_{resp.status_code}"
                rec.error_message = resp.text[:500]
                rec.total_ms = (time.perf_counter() - t0) * 1000
                return rec

            async for event, data in _iter_sse(resp):
                rec.frames[event] = rec.frames.get(event, 0) + 1
                now = (time.perf_counter() - t0) * 1000

                if event == "message.persisted":
                    rec.persisted_ms = now
                    rec.run_id = data.get("run_id")
                elif event in ("chunk", "text"):
                    # The cloud run emits assistant text as ``chunk``; ``text``
                    # is accepted too so this keeps working if that is renamed.
                    if rec.ttft_ms is None:
                        rec.ttft_ms = now
                    rec.text_chars += len(str(data.get("content") or data.get("text") or ""))
                elif event == "tool_start" and rec.first_tool_ms is None:
                    rec.first_tool_ms = now

                if event in TERMINAL:
                    rec.total_ms = now
                    if event == "stream_end":
                        rec.outcome = "completed"
                        rec.usage = data.get("usage") or {}
                    elif event == "interrupted":
                        rec.outcome = "interrupted"
                        rec.error_code = data.get("reason")
                    else:
                        rec.outcome = "error"
                        rec.error_code = data.get("code") or data.get("error")
                        rec.error_message = str(data.get("message") or "")[:500]
                    return rec

            # stream closed with no terminal frame
            rec.total_ms = (time.perf_counter() - t0) * 1000
            rec.outcome = "truncated"
            return rec
    except (TimeoutError, httpx.TimeoutException):
        rec.total_ms = (time.perf_counter() - t0) * 1000
        rec.outcome = "timeout"
        return rec
    except Exception as exc:  # noqa: BLE001 - a load driver must never die
        rec.total_ms = (time.perf_counter() - t0) * 1000
        rec.outcome = "conn_error"
        rec.error_message = f"{type(exc).__name__}: {exc}"[:500]
        return rec
    finally:
        inflight["n"] -= 1


# --------------------------------------------------------------------------
# host sampler
# --------------------------------------------------------------------------

_CLI_HINTS = ("claude", "claude-code")
_BUILD_HINTS = ("vite", "svelte-kit", "rollup", "esbuild", "wrangler")


class HostSampler:
    """Samples process / system / queue metrics on a fixed interval."""

    def __init__(
        self,
        *,
        interval: float,
        jail_root: str | None,
        redis_url: str | None,
        mongo_url: str | None,
        mongo_db: str,
        inflight: dict[str, int],
    ) -> None:
        self.interval = interval
        self.jail_root = os.path.expanduser(jail_root) if jail_root else None
        self.redis_url = redis_url
        self.mongo_url = mongo_url
        self.mongo_db = mongo_db
        self.inflight = inflight
        self.rows: list[dict[str, Any]] = []
        self._redis = None
        self._mongo = None

    async def _connect(self) -> None:
        if self.redis_url:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(self.redis_url, decode_responses=True)
                await self._redis.ping()
            except Exception as exc:  # noqa: BLE001
                print(f"[sampler] redis disabled: {exc}", file=sys.stderr)
                self._redis = None
        if self.mongo_url:
            try:
                from motor.motor_asyncio import AsyncIOMotorClient

                self._mongo = AsyncIOMotorClient(self.mongo_url)[self.mongo_db]
                await self._mongo.command("ping")
            except Exception as exc:  # noqa: BLE001
                print(f"[sampler] mongo disabled: {exc}", file=sys.stderr)
                self._mongo = None

    def _process_metrics(self) -> dict[str, Any]:
        """Count Claude CLI subprocesses, python web procs, and node build procs."""
        out = {
            "cli_procs": 0,
            "cli_rss_mb": 0.0,
            "web_procs": 0,
            "web_rss_mb": 0.0,
            "build_procs": 0,
            "total_procs": 0,
        }
        if psutil is None:
            return out
        for proc in psutil.process_iter(["name", "cmdline", "memory_info"]):
            try:
                info = proc.info
                name = (info.get("name") or "").lower()
                cmdline = " ".join(info.get("cmdline") or []).lower()
                mem = info.get("memory_info")
                rss_mb = (mem.rss / 1_048_576) if mem else 0.0
                out["total_procs"] += 1

                # Claude Code CLI: a node process whose argv names the CLI.
                if any(h in cmdline for h in _CLI_HINTS) and ("node" in name or "claude" in name):
                    out["cli_procs"] += 1
                    out["cli_rss_mb"] += rss_mb
                # Site build subprocesses (svelte publish path).
                elif any(h in cmdline for h in _BUILD_HINTS):
                    out["build_procs"] += 1
                # Backend web/worker processes.
                elif "python" in name and (
                    "pocketpaw" in cmdline or "uvicorn" in cmdline or "arq" in cmdline
                ):
                    out["web_procs"] += 1
                    out["web_rss_mb"] += rss_mb
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        out["cli_rss_mb"] = round(out["cli_rss_mb"], 1)
        out["web_rss_mb"] = round(out["web_rss_mb"], 1)
        return out

    async def _queue_metrics(self) -> dict[str, Any]:
        out: dict[str, Any] = {"arq_queued": None, "runs_queued": None, "runs_running": None}
        if self._redis is not None:
            try:
                out["arq_queued"] = await self._redis.zcard("arq:queue")
            except Exception:  # noqa: BLE001
                out["arq_queued"] = None
        if self._mongo is not None:
            try:
                col = self._mongo["chat_runs"]
                out["runs_queued"] = await col.count_documents({"status": "queued"})
                out["runs_running"] = await col.count_documents({"status": "running"})
            except Exception:  # noqa: BLE001
                pass
        return out

    async def run(self, stop: asyncio.Event) -> None:
        await self._connect()
        if psutil is not None:
            psutil.cpu_percent(interval=None)  # prime the counter
        while not stop.is_set():
            row: dict[str, Any] = {
                "ts": round(time.time(), 3),
                "inflight": self.inflight["n"],
            }
            if psutil is not None:
                row["sys_cpu_pct"] = psutil.cpu_percent(interval=None)
                row["sys_mem_pct"] = psutil.virtual_memory().percent
            row.update(self._process_metrics())
            if self.jail_root and os.path.isdir(self.jail_root):
                try:
                    usage = shutil.disk_usage(self.jail_root)
                    row["jail_disk_free_gb"] = round(usage.free / 1_073_741_824, 2)
                    row["jail_disk_used_pct"] = round(100 * usage.used / usage.total, 1)
                except OSError:
                    pass
            row.update(await self._queue_metrics())
            self.rows.append(row)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval)

    def write_csv(self, path: Path) -> None:
        if not self.rows:
            return
        cols: list[str] = []
        for row in self.rows:
            for key in row:
                if key not in cols:
                    cols.append(key)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=cols)
            writer.writeheader()
            writer.writerows(self.rows)


# --------------------------------------------------------------------------
# load shapes
# --------------------------------------------------------------------------


async def run_open_loop(
    client: httpx.AsyncClient,
    *,
    urls: list[str],
    make_body,
    rate: float,
    duration: float,
    timeout: float,
    inflight: dict[str, int],
    jitter: bool,
) -> list[RequestRecord]:
    """Fire requests at a fixed arrival rate regardless of completion.

    This is the shape that finds a ceiling: if the system can't keep up,
    in-flight count and latency climb without bound instead of the driver
    silently throttling itself.
    """
    tasks: list[asyncio.Task] = []
    seq = 0
    deadline = time.perf_counter() + duration
    while time.perf_counter() < deadline:
        seq += 1
        tasks.append(
            asyncio.create_task(
                drive_one(
                    client,
                    seq=seq,
                    url=urls[(seq - 1) % len(urls)],
                    body=make_body(seq),
                    timeout=timeout,
                    inflight=inflight,
                )
            )
        )
        gap = 1.0 / rate
        if jitter:  # Poisson arrivals — bursty like real traffic
            gap = random.expovariate(rate)
        await asyncio.sleep(gap)
    print(f"[driver] arrivals done ({seq} sent), draining {len(tasks)} in-flight…")
    return list(await asyncio.gather(*tasks))


async def run_closed_loop(
    client: httpx.AsyncClient,
    *,
    urls: list[str],
    make_body,
    vus: int,
    iterations: int,
    timeout: float,
    inflight: dict[str, int],
) -> list[RequestRecord]:
    """N virtual users, each looping sequentially. Measures steady-state
    latency at a known concurrency."""
    records: list[RequestRecord] = []
    counter = {"n": 0}
    lock = asyncio.Lock()

    async def worker(vu: int) -> None:
        # One scope per VU — a second run in the same scope supersedes the first.
        vu_url = urls[vu % len(urls)]
        for _ in range(iterations):
            async with lock:
                counter["n"] += 1
                seq = counter["n"]
            rec = await drive_one(
                client, seq=seq, url=vu_url, body=make_body(seq), timeout=timeout, inflight=inflight
            )
            records.append(rec)

    await asyncio.gather(*[worker(i) for i in range(vus)])
    return records


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
    return round(ordered[idx], 1)


def _latency_row(label: str, values: list[float]) -> str:
    if not values:
        return f"  {label:<16} —"
    return (
        f"  {label:<16} n={len(values):<5} "
        f"p50={_pct(values, 50):<9} p90={_pct(values, 90):<9} "
        f"p95={_pct(values, 95):<9} p99={_pct(values, 99):<9} "
        f"max={round(max(values), 1)}"
    )


def summarize(records: list[RequestRecord], sampler: HostSampler, wall_s: float) -> str:
    lines: list[str] = []
    total = len(records)
    completed = [r for r in records if r.outcome == "completed"]
    lines.append("")
    lines.append("=" * 78)
    lines.append("RESULTS")
    lines.append("=" * 78)
    lines.append(f"  requests sent      {total}")
    pct_ok = 100 * len(completed) / max(total, 1)
    lines.append(f"  completed          {len(completed)} ({pct_ok:.1f}%)")
    lines.append(f"  wall clock         {wall_s:.1f}s")
    if completed:
        lines.append(f"  throughput         {60 * len(completed) / wall_s:.2f} completed runs/min")

    lines.append("")
    lines.append("LATENCY (ms)")
    lines.append(_latency_row("accept", [r.accept_ms for r in records if r.accept_ms is not None]))
    lines.append(
        _latency_row("persisted", [r.persisted_ms for r in records if r.persisted_ms is not None])
    )
    lines.append(_latency_row("first token", [r.ttft_ms for r in records if r.ttft_ms is not None]))
    lines.append(
        _latency_row(
            "first tool", [r.first_tool_ms for r in records if r.first_tool_ms is not None]
        )
    )
    lines.append(_latency_row("total (ok)", [r.total_ms for r in completed if r.total_ms]))

    outcomes: dict[str, int] = {}
    for rec in records:
        key = rec.outcome
        if rec.error_code:
            key = f"{rec.outcome}:{rec.error_code}"
        outcomes[key] = outcomes.get(key, 0) + 1
    lines.append("")
    lines.append("OUTCOMES")
    for key, count in sorted(outcomes.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {key:<40} {count}")

    msgs = [r.error_message for r in records if r.error_message]
    if msgs:
        lines.append("")
        lines.append("SAMPLE ERRORS (first 3 distinct)")
        for msg in list(dict.fromkeys(msgs))[:3]:
            lines.append(f"  {msg}")

    tokens_in = sum(int(r.usage.get("input_tokens") or 0) for r in completed)
    tokens_out = sum(int(r.usage.get("output_tokens") or 0) for r in completed)
    if tokens_in or tokens_out:
        lines.append("")
        lines.append("TOKENS")
        lines.append(f"  input   {tokens_in:,}   ({tokens_in * 60 / max(wall_s, 1):,.0f}/min)")
        lines.append(f"  output  {tokens_out:,}   ({tokens_out * 60 / max(wall_s, 1):,.0f}/min)")
        lines.append("  ^ compare against your org's ITPM / OTPM limit for the model in use")

    if sampler.rows:
        lines.append("")
        lines.append("HOST PEAKS")

        def peak(col: str) -> Any:
            vals = [r[col] for r in sampler.rows if r.get(col) is not None]
            return max(vals) if vals else None

        def mean(col: str) -> Any:
            vals = [r[col] for r in sampler.rows if r.get(col) is not None]
            return round(statistics.fmean(vals), 1) if vals else None

        for col, label in (
            ("inflight", "in-flight runs"),
            ("cli_procs", "claude CLI procs"),
            ("cli_rss_mb", "claude CLI RSS (MB)"),
            ("build_procs", "site-build procs"),
            ("web_rss_mb", "web/worker RSS (MB)"),
            ("sys_cpu_pct", "system CPU %"),
            ("sys_mem_pct", "system mem %"),
            ("arq_queued", "arq queue depth"),
            ("runs_queued", "mongo runs queued"),
            ("runs_running", "mongo runs running"),
            ("jail_disk_free_gb", "jail disk free (GB)"),
        ):
            pk = peak(col)
            if pk is None:
                continue
            lines.append(f"  {label:<24} peak={pk:<10} mean={mean(col)}")
        lines.append(
            "  (process counts are for THIS host — run the driver on the app box, or "
            "sample it separately, or these numbers describe your laptop)"
        )

    lines.append("")
    lines.append("SATURATION VERDICT")
    for line in _verdict(records, sampler):
        lines.append(f"  {line}")
    lines.append("=" * 78)
    return "\n".join(lines)


def _verdict(records: list[RequestRecord], sampler: HostSampler) -> list[str]:
    """Point at the resource that gave out first, instead of leaving it to guesswork."""
    out: list[str] = []
    rows = sampler.rows

    def peak(col: str) -> float:
        vals = [r[col] for r in rows if isinstance(r.get(col), int | float)]
        return max(vals) if vals else 0.0

    if any(r.outcome.startswith("http_429") for r in records):
        out.append("HTTP 429 seen — an ingress/app rate limit fired before capacity did.")
    if any(r.outcome == "http_402" for r in records):
        out.append("HTTP 402 — credits/quota gate rejected runs; top up before re-testing.")
    if any(r.error_code == "agent.jail_over_quota" for r in records):
        out.append(
            "agent.jail_over_quota — per-workspace jail hit POCKETPAW_AGENT_JAIL_QUOTA_MB "
            "(default 2048MB). Raise it or shorten the scenario."
        )
    if any(r.error_code == "agent.backend_error" for r in records):
        out.append(
            "agent.backend_error — check the backend log for Anthropic 429/529: that is the "
            "API-side rate limit, not your box."
        )
    if any(r.outcome == "timeout" for r in records):
        out.append("Client timeouts — raise --timeout or the run genuinely stalled.")

    if peak("arq_queued") > 0:
        out.append(
            f"arq queue depth peaked at {peak('arq_queued'):.0f} — the worker is the bottleneck. "
            "WorkerSettings sets no max_jobs, so arq's default of 10 concurrent runs/worker "
            "applies. Scale workers or set max_jobs."
        )
    if peak("sys_cpu_pct") > 85:
        out.append(f"System CPU peaked at {peak('sys_cpu_pct'):.0f}% — CPU-bound.")
    if peak("sys_mem_pct") > 85:
        out.append(
            f"System memory peaked at {peak('sys_mem_pct'):.0f}% — each concurrent run holds a "
            "Claude Code CLI (Node) subprocess; memory is usually the first wall."
        )
    cli_peak = peak("cli_procs")
    if cli_peak:
        out.append(f"Peak concurrent Claude CLI subprocesses: {cli_peak:.0f}.")
    if not out:
        out.append("No saturation signal — the system absorbed this load. Raise --rate and re-run.")
    return out


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", required=True, help="e.g. http://localhost:8000")
    p.add_argument("--token", default=os.environ.get("PAW_TOKEN", ""), help="Bearer token")
    p.add_argument("--workspace", default=os.environ.get("PAW_WORKSPACE", ""), help="workspace id")
    p.add_argument("--scope", default="pocket", choices=["pocket", "group", "dm"])
    p.add_argument("--scope-id", required=True)
    p.add_argument("--surface", default=None, help="sites | code | home | pockets | …")
    p.add_argument("--surface-meta", default=None, help='JSON dict, e.g. \'{"pocket_id":"…"}\'')
    p.add_argument("--agent-id", default=None)
    p.add_argument("--prompt", default=None, help="prompt text (use {n} for the request number)")
    p.add_argument("--prompt-file", default=None, help="file with one prompt per line, cycled")
    p.add_argument("--api-prefix", default="/api/v1")

    p.add_argument("--mode", default="open", choices=["open", "closed"])
    p.add_argument("--rate", type=float, default=0.5, help="open mode: arrivals/sec")
    p.add_argument("--duration", type=float, default=120, help="open mode: seconds of arrivals")
    p.add_argument("--jitter", action="store_true", help="open mode: Poisson arrivals")
    p.add_argument("--vus", type=int, default=5, help="closed mode: concurrent virtual users")
    p.add_argument("--iterations", type=int, default=3, help="closed mode: runs per VU")
    p.add_argument("--timeout", type=float, default=900, help="per-request timeout (s)")

    p.add_argument("--sample-interval", type=float, default=2.0)
    p.add_argument("--jail-root", default=None, help="e.g. ~/.pocketpaw/workspaces")
    p.add_argument("--redis-url", default=os.environ.get("POCKETPAW_REDIS_URL"))
    p.add_argument("--mongo-url", default=None)
    p.add_argument("--mongo-db", default="pocketpaw")
    p.add_argument("--out", default=None, help="output dir (default: out/<timestamp>)")
    return p


async def main_async(args: argparse.Namespace) -> int:
    prompts: list[str]
    if args.prompt_file:
        raw = Path(args.prompt_file).read_text("utf-8").splitlines()
        prompts = [ln.strip() for ln in raw if ln.strip()]
    elif args.prompt:
        prompts = [args.prompt]
    else:
        print("error: pass --prompt or --prompt-file", file=sys.stderr)
        return 2

    surface_meta = json.loads(args.surface_meta) if args.surface_meta else None
    root = f"{args.base_url.rstrip('/')}{args.api_prefix}"
    # --scope-id accepts a comma-separated list. A new run supersedes the
    # previous run in the SAME scope, so driving every request at one scope
    # measures one user typing fast, not N users. Round-robin across scopes.
    scope_ids = [s.strip() for s in args.scope_id.split(",") if s.strip()]
    urls = [f"{root}/cloud/chat/{args.scope}/{sid}/agent" for sid in scope_ids]
    url = urls[0]
    if len(urls) > 1:
        print(f"[driver] round-robin across {len(urls)} scopes")

    def make_body(seq: int) -> dict[str, Any]:
        text = prompts[(seq - 1) % len(prompts)].replace("{n}", str(seq))
        body: dict[str, Any] = {
            "content": text,
            # unique per send so create_run's idempotency key never collapses
            # two load-test runs into one
            "client_message_id": f"loadtest-{int(time.time())}-{seq}",
        }
        if args.surface:
            body["surface"] = args.surface
        if surface_meta:
            body["surface_meta"] = surface_meta
        if args.agent_id:
            body["agent_id"] = args.agent_id
        return body

    headers = {"Accept": "text/event-stream"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    if args.workspace:
        headers["X-Workspace-Id"] = args.workspace

    out_dir = Path(args.out or f"out/{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)

    inflight = {"n": 0}
    sampler = HostSampler(
        interval=args.sample_interval,
        jail_root=args.jail_root,
        redis_url=args.redis_url,
        mongo_url=args.mongo_url,
        mongo_db=args.mongo_db,
        inflight=inflight,
    )
    stop = asyncio.Event()
    sampler_task = asyncio.create_task(sampler.run(stop))

    shape = (
        f"open loop @ {args.rate}/s for {args.duration}s"
        if args.mode == "open"
        else f"closed loop, {args.vus} VUs x {args.iterations}"
    )
    print(f"[driver] {shape}")
    print(f"[driver] POST {url}")
    print(f"[driver] surface={args.surface or '(none)'}  out={out_dir}")
    if psutil is None:
        print("[driver] psutil not installed — host process metrics disabled", file=sys.stderr)

    limits = httpx.Limits(max_connections=1000, max_keepalive_connections=200)
    started = time.perf_counter()
    async with httpx.AsyncClient(headers=headers, limits=limits, timeout=None) as client:
        if args.mode == "open":
            records = await run_open_loop(
                client,
                urls=urls,
                make_body=make_body,
                rate=args.rate,
                duration=args.duration,
                timeout=args.timeout,
                inflight=inflight,
                jitter=args.jitter,
            )
        else:
            records = await run_closed_loop(
                client,
                urls=urls,
                make_body=make_body,
                vus=args.vus,
                iterations=args.iterations,
                timeout=args.timeout,
                inflight=inflight,
            )
    wall = time.perf_counter() - started

    stop.set()
    with contextlib.suppress(asyncio.CancelledError):
        await sampler_task

    with (out_dir / "requests.jsonl").open("w", encoding="utf-8") as handle:
        for rec in sorted(records, key=lambda r: r.seq):
            handle.write(json.dumps(asdict(rec)) + "\n")
    sampler.write_csv(out_dir / "samples.csv")

    report = summarize(records, sampler, wall)
    print(report)
    (out_dir / "summary.txt").write_text(report, encoding="utf-8")
    print(f"\n[driver] wrote {out_dir}/requests.jsonl, samples.csv, summary.txt")

    failed = sum(1 for r in records if r.outcome != "completed")
    return 1 if failed and failed == len(records) else 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[driver] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
