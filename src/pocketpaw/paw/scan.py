# Project scanning — learns about a project via agent or heuristic fallback.
# Created: 2026-03-02
# SCAN_PROMPT template, run_agent_scan(), heuristic_scan().

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from soul_protocol import Soul

logger = logging.getLogger(__name__)

SCAN_PROMPT = """You are scanning a new project to learn about it. Your job is to understand:
1. What language/framework this project uses
2. What the project does (purpose)
3. How to build/run/test it
4. Key files and directory structure
5. Any notable patterns or conventions

Use the file reading tools to examine:
- README.md or README files
- pyproject.toml, package.json, Cargo.toml, go.mod (build configs)
- Top-level source files and directory names
- .env.example or similar config templates (DO NOT read .env itself)

For each important fact you discover, use the soul_remember tool to store it.
Be thorough but efficient — focus on the most important structural facts.

Project root: {project_path}
"""


async def run_agent_scan(soul: Soul, project_path: Path, provider: str) -> None:
    """Run a full agent-powered scan of the project.

    Uses the configured LLM provider to intelligently scan the project,
    reading files and storing discoveries in the soul's memory.
    """

    SCAN_PROMPT.format(project_path=project_path)

    # For now, delegate to heuristic scan + remember.
    # Full agent-powered scan requires wiring AgentRouter which is a bigger lift.
    # This is the recommended path for v1 — do the heuristic scan and store results.
    logger.info("Running heuristic project scan (agent scan coming in next version)")
    await heuristic_scan(project_path, soul)


async def heuristic_scan(project_path: Path, soul: Soul) -> None:
    """Offline fallback that reads common project files and stores facts.

    Examines README, pyproject.toml, package.json, and .env.example
    to bootstrap the soul's knowledge about the project.
    """
    facts: list[str] = []

    # README
    for readme_name in ("README.md", "README.rst", "README.txt", "README"):
        readme_path = project_path / readme_name
        if readme_path.exists():
            content = readme_path.read_text(errors="replace")[:2000]
            facts.append(f"Project README ({readme_name}):\n{content}")
            break

    # pyproject.toml
    pyproject = project_path / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(errors="replace")[:1500]
        facts.append(f"Python project config (pyproject.toml):\n{content}")

    # package.json
    pkg_json = project_path / "package.json"
    if pkg_json.exists():
        content = pkg_json.read_text(errors="replace")[:1500]
        facts.append(f"Node.js project config (package.json):\n{content}")

    # Cargo.toml
    cargo = project_path / "Cargo.toml"
    if cargo.exists():
        content = cargo.read_text(errors="replace")[:1500]
        facts.append(f"Rust project config (Cargo.toml):\n{content}")

    # go.mod
    gomod = project_path / "go.mod"
    if gomod.exists():
        content = gomod.read_text(errors="replace")[:500]
        facts.append(f"Go module config (go.mod):\n{content}")

    # .env.example (safe to read — no secrets)
    env_example = project_path / ".env.example"
    if env_example.exists():
        content = env_example.read_text(errors="replace")[:500]
        var_names = [
            line.split("=")[0].strip()
            for line in content.splitlines()
            if "=" in line and not line.strip().startswith("#")
        ]
        if var_names:
            facts.append(f"Environment variables used: {', '.join(var_names)}")

    # Directory structure (top-level only)
    try:
        entries = sorted(p.name for p in project_path.iterdir() if not p.name.startswith("."))
        dirs = [e for e in entries if (project_path / e).is_dir()]
        files = [e for e in entries if (project_path / e).is_file()]
        facts.append(
            f"Top-level directories: {', '.join(dirs[:20])}\n"
            f"Top-level files: {', '.join(files[:20])}"
        )
    except OSError:
        pass

    # Store each fact in the soul
    for fact in facts:
        importance = 8 if "README" in fact or "project config" in fact else 6
        try:
            await soul.remember(fact, importance=importance)
        except Exception as e:
            logger.warning("Failed to store scan fact: %s", e)

    logger.info("Heuristic scan complete — stored %d facts", len(facts))
