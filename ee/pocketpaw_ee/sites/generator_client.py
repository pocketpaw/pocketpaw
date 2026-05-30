# ee/pocketpaw_ee/sites/generator_client.py — Python bridge to the Node/Bun
# generator (paw-sites-gen). build() runs the generate CLI, then the workerd
# smoke render; if the smoke gate fails the site does NOT proceed to deploy
# (Contract clause 4). The subprocess calls are isolated behind a _runner so
# the orchestration is unit-testable without Bun/workerd present.
# Created: 2026-05-30 (feat/paw-sites-backend, Task 2.3).

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Protocol


class SmokeGateFailed(RuntimeError):
    """Raised when the workerd smoke render fails — the site is not deployed."""


@dataclass(frozen=True)
class BuildResult:
    project_dir: str
    ripple_version: str


class _Runner(Protocol):
    async def generate(self, input_json: dict[str, Any], out_dir: str) -> dict[str, Any]: ...
    async def smoke(self, project_dir: str) -> tuple[bool, str]: ...


class _SubprocessRunner:
    """Real runner: shells out to `paw-sites-gen` (Bun) and the smoke render."""

    def __init__(self, gen_cmd: str = "paw-sites-gen") -> None:
        self._gen_cmd = gen_cmd

    async def generate(self, input_json: dict[str, Any], out_dir: str) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(input_json, fh)
            input_path = fh.name
        try:
            proc = await asyncio.create_subprocess_exec(
                self._gen_cmd,
                "build",
                "--input",
                input_path,
                "--out",
                out_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"generator failed: {stderr.decode()}")
            return json.loads(stdout.decode().strip().splitlines()[-1])
        finally:
            os.unlink(input_path)

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        # Build the generated project; a non-zero exit OR a known workerd marker
        # in the output fails the gate. Mirrors paw-sites/src/smoke.ts markers.
        proc = await asyncio.create_subprocess_exec(
            "bun",
            "run",
            "build",
            cwd=project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        haystack = stdout.decode() + "\n" + stderr.decode()
        for marker in ("window is not defined", "document is not defined", "No such module"):
            if marker in haystack:
                return False, f"workerd SSR failure: {marker}"
        if proc.returncode != 0:
            return False, f"build failed (exit {proc.returncode})"
        return True, "ok"


class GeneratorClient:
    def __init__(self, _runner: _Runner | None = None) -> None:
        self._runner = _runner or _SubprocessRunner()

    async def build(
        self,
        *,
        ripple_spec: dict[str, Any],
        theme: dict[str, Any],
        site_id: str,
        title: str,
        capture_api_base: str,
        capture_signed_key: str,
    ) -> BuildResult:
        out_dir = tempfile.mkdtemp(prefix=f"paw-site-{site_id}-")
        input_json = {
            "rippleSpec": ripple_spec,
            "theme": theme,
            "siteConfig": {
                "siteId": site_id,
                "title": title,
                "captureApiBase": capture_api_base,
                "captureSignedKey": capture_signed_key,
            },
        }
        gen = await self._runner.generate(input_json, out_dir)
        ok, reason = await self._runner.smoke(gen["projectDir"])
        if not ok:
            raise SmokeGateFailed(reason)
        return BuildResult(project_dir=gen["projectDir"], ripple_version=gen["rippleVersion"])
