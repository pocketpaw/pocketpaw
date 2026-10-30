#!/usr/bin/env python
# scripts/sg9_daytona_roundtrip.py — LIVE probe: build a real react project in a real
# Daytona sandbox and report the measured S / (I+B) / D terms.
#
# Created 2026-08-09 (SG-9i slice 1). This is deliberately a script and not a test: it
# costs sandbox time, needs live credentials, and its output is MEASUREMENTS rather than
# assertions. The driver's sequencing contract is unit-tested against a fake in
# tests/ee/sites/test_daytona_runner.py; a fake passing is not evidence the lane works,
# which is why this exists.
#
# Usage (from the repo root, with DAYTONA_* in .env):
#
#     uv run python scripts/sg9_daytona_roundtrip.py <project-dir> [engine] [out.tgz]
#
# ``out.tgz`` writes the extracted artifact to disk so it can be inspected — the point
# of this slice is evidence, and "artifact_bytes > 0" is weaker evidence than a tarball
# you can list.
#
# ``engine`` is react (default) or svelte. Both run bun install + Vite, and extraction
# is parameterised on ``static_output_rel(engine)`` — react's ``dist`` and svelte's
# ``.svelte-kit/cloudflare`` — so the build path is identical for both. What is NOT
# identical is the DEPLOY shape: ``emits_server_worker()`` is False for react and True
# for svelte, so svelte's output carries a ``_worker.js`` server entry that has to be
# deployed as a Worker script rather than as static files. Deploy is out of scope here.
#
# The project directory should be REAL generator output — produce it with
# ``materializeReact`` (react) or ``scaffoldProject`` + ``materializeSource`` (svelte)
# from paw-sites rather than hand-writing one, so the dependency set and the build
# script are the ones production actually uses (notably react's build runs
# ``bun paw-prerender.mjs``, which is why bun is not optional).
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

# Vite prints U+2713 on success and the Windows console defaults to cp1252, which
# raises UnicodeEncodeError mid-report — losing the measurements AFTER the build has
# already been paid for. Reconfigure rather than sanitize each print: the build log is
# arbitrary third-party output and there is no reason to assume it is ASCII.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass

# Text extensions only: the generated react project is all source. A binary asset would
# need bytes, and bulk_upload accepts them — this probe just does not need it.
_SKIP_DIRS = {"node_modules", ".git", "dist", ".paw-ssr", ".svelte-kit"}

#: Teardown is eventually consistent (see the verification block), so poll rather than
#: check once. ~30s total is well past the few seconds observed in practice.
_TEARDOWN_POLL_ATTEMPTS = 10
_TEARDOWN_POLL_SECONDS = 3


def collect(project: Path) -> dict[str, str | bytes]:
    files: dict[str, str | bytes] = {}
    for path in sorted(project.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(project).as_posix()
        if any(seg in _SKIP_DIRS for seg in rel.split("/")):
            continue
        files[rel] = path.read_text(encoding="utf-8")
    return files


def _describe_artifact(blob: bytes) -> None:
    """List what actually came back, and check the two things that matter.

    ``node_modules`` absent is the include-list working (it is excluded by construction
    because the output dir is a subdirectory, not by any filter). ``_worker.js`` present
    is svelte's server entry — the shape difference react does not have, and the reason
    svelte cannot be deployed as pure static assets.
    """
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
    workers = [n for n in names if n.endswith("_worker.js")]
    node_modules = [n for n in names if "node_modules" in n]
    print(f"  entries: {len(names)}")
    for name in sorted(names)[:12]:
        print(f"    {name}")
    if len(names) > 12:
        print(f"    ... and {len(names) - 12} more")
    print(f"  _worker.js       : {workers or 'absent (expected for react)'}")
    print(f"  node_modules     : {len(node_modules)} entries (must be 0)")


def load_env() -> None:
    """Load .env the way the app does, and report WHICH credential won.

    The repo's .env carries duplicate DAYTONA_API_KEY entries pointing at different
    accounts; python-dotenv resolves the LAST assignment. Printing the fingerprint (not
    the key) makes it unambiguous which account a given run was billed to.
    """
    from dotenv import dotenv_values

    vals = dotenv_values(".env")
    for name in ("DAYTONA_API_KEY", "DAYTONA_API_URL"):
        val = vals.get(name)
        if val:
            os.environ[name] = val
    key = os.environ.get("DAYTONA_API_KEY", "")
    print(f"  api_url = {os.environ.get('DAYTONA_API_URL')}")
    print(f"  api_key = <len {len(key)}, sha256:{hashlib.sha256(key.encode()).hexdigest()[:12]}>")


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    project = Path(sys.argv[1])
    if not project.is_dir():
        print(f"not a directory: {project}")
        return 2

    engine = (sys.argv[2] if len(sys.argv) > 2 else "react").strip().lower()

    from pocketpaw_ee.sites.daytona_build import resolve_build_timeout_seconds

    timeout_seconds = resolve_build_timeout_seconds(engine)

    print("=== credentials ===")
    load_env()

    from pocketpaw_ee.cloud.daytona.client import get_daytona_client

    files = collect(project)
    print(f"\n=== input ===\n  {len(files)} files from {project}")
    for rel in files:
        print(f"    {rel}")

    client = get_daytona_client()
    if client is None:
        print("\nBLOCKED: Daytona is not configured")
        return 1

    # One try/finally around everything that uses the client, so the aiohttp session is
    # closed even when reporting blows up. The first run of this probe leaked one
    # because the close sat after a print that raised.
    try:
        return await _run_and_report(client, files, timeout_seconds, engine)
    finally:
        await client.close()


async def _run_and_report(
    client: Any, files: dict[str, str | bytes], timeout_seconds: int, engine: str
) -> int:
    from pocketpaw_ee.sites.daytona_runner import run_build

    print(f"\n=== running (timeout {timeout_seconds}s) ===")
    try:
        result = await run_build(
            files,
            engine=engine,
            timeout_seconds=timeout_seconds,
            client=client,
        )
    except Exception as exc:  # noqa: BLE001 — a probe reports rather than crashes
        print(f"\nBLOCKED: could not run the build: {type(exc).__name__}: {exc}")
        return 1

    print("\n=== outcome ===")
    print(f"  outcome        : {result.classification.outcome}")
    print(f"  reason         : {result.classification.reason}")
    print(f"  blames_user    : {result.classification.blames_user}")
    print(f"  retryable      : {result.classification.retryable}")
    print(f"  artifact_bytes : {result.artifact_bytes}")
    print(f"  sandbox_id     : {result.sandbox_id}")
    print(f"  deleted        : {result.sandbox_deleted}")
    if result.classification.stderr_tail:
        tail = result.classification.stderr_tail[-1500:]
        print(f"\n  --- stderr tail ---\n{tail}")

    # Land the artifact somewhere inspectable. "artifact_bytes > 0" is weaker evidence
    # than a tarball whose entries can be listed — for svelte in particular, the thing
    # worth confirming is that ``_worker.js`` came through and ``node_modules`` did not.
    out_path = sys.argv[3] if len(sys.argv) > 3 else None
    if out_path and result.artifact:
        Path(out_path).write_bytes(result.artifact)
        print(f"\n  artifact written to {out_path}")
        _describe_artifact(result.artifact)

    print("\n=== measured timings (seconds) ===")
    for k, v in result.timings.as_dict().items():
        print(f"  {k:<12} {v}")

    # Prove the sandbox is actually gone rather than trusting the delete call.
    #
    # POLLED, not checked once. Measured 2026-08-09: Daytona's delete is
    # EVENTUALLY CONSISTENT — a sandbox deleted successfully still appeared in
    # ``list()`` immediately afterwards and drained a few seconds later. A single check
    # therefore reports a false "still present" and would fail this slice's acceptance
    # criterion on a teardown that actually worked. It also means an account-level
    # concurrent-sandbox count lags reality, which the concurrency cap has to allow for.
    if result.sandbox_id:
        print("\n=== teardown verification ===")
        gone = False
        remaining: list[Any] = []
        for attempt in range(_TEARDOWN_POLL_ATTEMPTS):
            try:
                remaining = await client.list_sandboxes()
            except Exception as exc:  # noqa: BLE001
                print(f"  could not verify: {exc}")
                break
            if result.sandbox_id not in {s.id for s in remaining}:
                gone = True
                print(f"  gone from the account listing after ~{attempt * _TEARDOWN_POLL_SECONDS}s")
                break
            await asyncio.sleep(_TEARDOWN_POLL_SECONDS)
        if not gone:
            waited = _TEARDOWN_POLL_ATTEMPTS * _TEARDOWN_POLL_SECONDS
            print(f"  STILL PRESENT after ~{waited}s — teardown did not take effect")
        print(f"  total sandboxes now: {len(remaining)}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
