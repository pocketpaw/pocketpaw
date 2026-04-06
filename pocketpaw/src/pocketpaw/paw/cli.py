# Paw CLI — Click-based universal entry point for PocketPaw with soul-protocol.
# Created: 2026-03-02
# Updated: 2026-03-02 — Fixed _print() call without args in doctor command.
# Commands: init, ask, chat, serve, status, doctor, os, channels.

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click

logger = logging.getLogger(__name__)

# CLI exit commands recognised in the interactive chat loop
_EXIT_COMMANDS: frozenset[str] = frozenset({"exit", "quit", "bye"})


def _run_async(coro):
    """Bridge sync Click commands to async internals. When already inside a running event loop
    (e.g. pytest-asyncio), run the coro in a thread to avoid 'Runner.run() cannot be called
    from a running event loop'."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def _get_console():
    """Lazy-import rich Console."""
    try:
        from rich.console import Console

        return Console()
    except ImportError:
        return None


def _print(message: str, style: str | None = None) -> None:
    """Print with rich if available, plain otherwise."""
    console = _get_console()
    if console and style:
        console.print(message, style=style)
    elif console:
        console.print(message)
    else:
        click.echo(message)


def _check_soul_protocol() -> bool:
    """Check if soul-protocol is installed."""
    try:
        import soul_protocol  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Main group
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.pass_context
@click.version_option(package_name="pocketpaw", prog_name="paw")
def main(ctx: click.Context) -> None:
    """paw — your AI companion that lives in your project."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# paw init
# ---------------------------------------------------------------------------


@main.command()
@click.option("--name", "-n", default=None, help="Name for the soul (default: Paw)")
@click.option("--provider", "-p", default=None, help="LLM provider: claude, openai, ollama, none")
@click.option("--scan/--no-scan", default=True, help="Scan project on init (default: yes)")
def init(name: str | None, provider: str | None, scan: bool) -> None:
    """Initialize paw in the current project directory."""
    if not _check_soul_protocol():
        _print(
            "soul-protocol is not installed. Install with:\n  pip install pocketpaw[soul]",
            style="bold red",
        )
        raise SystemExit(1)

    _run_async(_init_async(name, provider, scan))


async def _init_async(name: str | None, provider: str | None, scan: bool) -> None:
    """Async implementation of paw init."""

    from pocketpaw.paw.config import PawConfig

    project_root = Path.cwd()
    config = PawConfig.load(project_root)

    if name:
        config.soul_name = name
    if provider:
        config.provider = provider

    # Ensure .paw directory
    config.paw_dir.mkdir(parents=True, exist_ok=True)

    _print(f"\nInitializing paw in {project_root}", style="bold cyan")
    _print(f"  Soul name: {config.soul_name}", style="dim")
    _print(f"  Provider:  {config.provider}", style="dim")

    # Birth the soul
    from soul_protocol import Soul

    soul_path = config.soul_path or config.default_soul_path
    if soul_path.exists():
        _print(f"\n  Soul already exists at {soul_path}", style="yellow")
        soul = await Soul.awaken(soul_path)
    else:
        _print(f"\n  Birthing soul: {config.soul_name}...", style="green")
        soul = await Soul.birth(
            name=config.soul_name,
            archetype="Project Assistant",
            persona=f"I am {config.soul_name}, the resident AI for this project.",
        )
        soul_path.parent.mkdir(parents=True, exist_ok=True)
        await soul.save(soul_path)
        _print(f"  Soul saved to {soul_path}", style="green")

    # Write paw.yaml config
    yaml_path = project_root / "paw.yaml"
    if not yaml_path.exists():
        yaml_content = (
            f"# Paw configuration\n"
            f"soul_name: {config.soul_name}\n"
            f"provider: {config.provider}\n"
            f"soul_path: {soul_path}\n"
        )
        yaml_path.write_text(yaml_content)
        _print(f"  Config written to {yaml_path}", style="green")

    # Scan project
    if scan:
        _print("\n  Scanning project...", style="cyan")
        from pocketpaw.paw.scan import heuristic_scan

        await heuristic_scan(project_root, soul)
        await soul.save(soul_path)
        _print("  Scan complete. Soul updated with project knowledge.", style="green")

    _print("\n  paw is ready. Try: paw ask 'what is this project?'\n", style="bold green")


# ---------------------------------------------------------------------------
# paw ask
# ---------------------------------------------------------------------------


@main.command()
@click.argument("question")
def ask(question: str) -> None:
    """Ask paw a one-shot question about your project."""
    if not _check_soul_protocol():
        _print("soul-protocol is not installed. Run: pip install pocketpaw[soul]", style="red")
        raise SystemExit(1)

    _run_async(_ask_async(question))


async def _ask_async(question: str) -> None:
    """Async implementation of paw ask."""
    from pocketpaw.paw.agent import get_paw_agent

    try:
        agent = await get_paw_agent()
    except Exception as e:
        _print(f"Failed to initialize paw: {e}", style="red")
        _print("Run 'paw init' first to set up your project.", style="dim")
        raise SystemExit(1)

    # Recall relevant memories
    memories = await agent.bridge.recall(question, limit=5)
    context_parts = []
    if memories:
        context_parts.append("Relevant memories:\n" + "\n".join(f"- {m}" for m in memories))

    # Get bootstrap context
    bootstrap = await agent.bootstrap_provider.get_context()
    system_prompt = bootstrap.to_system_prompt()

    if context_parts:
        system_prompt += "\n\n" + "\n".join(context_parts)

    # For now, print the system prompt context and note that full agent routing
    # requires provider-specific wiring
    _print(f"\n[{agent.config.soul_name}]", style="bold cyan")

    if agent.config.provider == "none":
        _print(
            "Provider is set to 'none'. Showing recalled memories only:\n",
            style="dim",
        )
        if memories:
            for m in memories:
                _print(f"  - {m}")
        else:
            _print("  No relevant memories found. Run 'paw init --scan' first.", style="dim")
        return

    # Use PocketPaw's agent router for the actual query
    try:
        from pocketpaw.agents.router import AgentRouter
        from pocketpaw.config import get_settings

        settings = get_settings()
        router = AgentRouter(settings)

        response_parts: list[str] = []
        async for event in router.run(question, system_prompt=system_prompt):
            if event.type == "message":
                response_parts.append(event.content)
            elif event.type == "error":
                _print(f"Error: {event.content}", style="red")

        if response_parts:
            _print("".join(response_parts))

        # Observe the interaction
        full_response = "".join(response_parts)
        if full_response:
            await agent.bridge.observe(question, full_response)
            await agent.soul.save(agent.config.soul_path or agent.config.default_soul_path)
    except Exception as e:
        _print(f"Agent error: {e}", style="red")
        _print("Falling back to memory recall only.", style="dim")
        if memories:
            for m in memories:
                _print(f"  - {m}")


# ---------------------------------------------------------------------------
# paw chat
# ---------------------------------------------------------------------------


@main.command()
def chat() -> None:
    """Start an interactive chat session with paw."""
    if not _check_soul_protocol():
        _print("soul-protocol is not installed. Run: pip install pocketpaw[soul]", style="red")
        raise SystemExit(1)

    _run_async(_chat_async())


async def _chat_async() -> None:
    """Async implementation of paw chat REPL."""
    from pocketpaw.paw.agent import get_paw_agent

    try:
        agent = await get_paw_agent()
    except Exception as e:
        _print(f"Failed to initialize paw: {e}", style="red")
        _print("Run 'paw init' first.", style="dim")
        raise SystemExit(1)

    _print(
        f"\n  Chat with {agent.config.soul_name} (type 'exit' or Ctrl+C to quit)\n",
        style="bold cyan",
    )

    try:
        from pocketpaw.agents.router import AgentRouter
        from pocketpaw.config import get_settings

        settings = get_settings()
        router = AgentRouter(settings)
    except Exception:
        router = None

    soul_path = agent.config.soul_path or agent.config.default_soul_path

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            _print("\nGoodbye!", style="dim")
            break

        if not user_input:
            continue
        if user_input.lower() in _EXIT_COMMANDS:
            _print("Goodbye!", style="dim")
            break

        # Recall context
        memories = await agent.bridge.recall(user_input, limit=5)
        bootstrap = await agent.bootstrap_provider.get_context()
        system_prompt = bootstrap.to_system_prompt()

        if memories:
            system_prompt += "\n\nRelevant memories:\n" + "\n".join(f"- {m}" for m in memories)

        if router:
            response_parts: list[str] = []
            async for event in router.run(user_input, system_prompt=system_prompt):
                if event.type == "message":
                    response_parts.append(event.content)
                    # Stream to terminal
                    sys.stdout.write(event.content)
                    sys.stdout.flush()
                elif event.type == "error":
                    _print(f"\nError: {event.content}", style="red")

            print()  # newline after streamed response
            full_response = "".join(response_parts)
            if full_response:
                await agent.bridge.observe(user_input, full_response)
        else:
            _print("(No agent backend available — showing memories only)", style="dim")
            if memories:
                for m in memories:
                    _print(f"  - {m}")
            else:
                _print("  No relevant memories found.", style="dim")

    # Save soul state on exit
    try:
        await agent.soul.save(soul_path)
        _print("Soul state saved.", style="dim")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# paw serve
# ---------------------------------------------------------------------------


@main.command()
@click.option("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
@click.option("--port", "-p", default=8888, type=int, help="Port to bind (default: 8888)")
def serve(host: str, port: int) -> None:
    """Start MCP server (placeholder — full implementation coming soon)."""
    _print(f"MCP server placeholder — would bind to {host}:{port}", style="yellow")
    _print("Full MCP server integration coming in a future release.", style="dim")


# ---------------------------------------------------------------------------
# paw status
# ---------------------------------------------------------------------------


@main.command()
def status() -> None:
    """Show the soul's current state and project info."""
    if not _check_soul_protocol():
        _print("soul-protocol is not installed.", style="red")
        raise SystemExit(1)

    _run_async(_status_async())


async def _status_async() -> None:
    """Async implementation of paw status."""
    from pocketpaw.paw.agent import get_paw_agent

    try:
        agent = await get_paw_agent()
    except Exception as e:
        _print(f"Not initialized: {e}", style="red")
        _print("Run 'paw init' first.", style="dim")
        raise SystemExit(1)

    config = agent.config
    soul = agent.soul
    state = soul.state

    _print("\n  paw status", style="bold cyan")
    _print(f"  {'─' * 40}")
    _print(f"  Soul:     {config.soul_name}")
    _print(f"  Provider: {config.provider}")
    _print(f"  Project:  {config.project_root}")

    if hasattr(state, "mood"):
        _print(f"  Mood:     {state.mood}")
    if hasattr(state, "energy"):
        _print(f"  Energy:   {state.energy}")
    if hasattr(state, "social_battery"):
        _print(f"  Social:   {state.social_battery}")

    # Show active domains
    if hasattr(soul, "self_model") and soul.self_model:
        try:
            images = soul.self_model.get_active_self_images(limit=5)
            if images:
                _print("\n  Active domains:")
                for img in images:
                    _print(f"    - {img.domain} (confidence: {img.confidence})")
        except Exception:
            pass

    _print()


# ---------------------------------------------------------------------------
# paw doctor
# ---------------------------------------------------------------------------


@main.command()
def doctor() -> None:
    """Run health checks for paw and PocketPaw."""
    _print("\n  paw doctor", style="bold cyan")
    _print(f"  {'─' * 40}")

    # Check soul-protocol
    if _check_soul_protocol():
        _print("  [OK]   soul-protocol installed", style="green")
    else:
        _print("  [FAIL] soul-protocol not installed", style="red")
        _print("         pip install pocketpaw[soul]", style="dim")

    # Check click
    _print("  [OK]   click installed", style="green")

    # Check rich
    try:
        import rich  # noqa: F401

        _print("  [OK]   rich installed", style="green")
    except ImportError:
        _print("  [WARN] rich not installed (plain output)", style="yellow")

    # Check .paw directory
    paw_dir = Path.cwd() / ".paw"
    if paw_dir.exists():
        _print("  [OK]   .paw directory exists", style="green")
    else:
        _print("  [WARN] .paw directory not found — run 'paw init'", style="yellow")

    # Check paw.yaml
    yaml_path = Path.cwd() / "paw.yaml"
    if yaml_path.exists():
        _print("  [OK]   paw.yaml found", style="green")
    else:
        _print("  [WARN] paw.yaml not found — run 'paw init'", style="yellow")

    # Check soul file
    from pocketpaw.paw.config import PawConfig

    config = PawConfig.load()
    soul_path = config.soul_path or config.default_soul_path
    if soul_path.exists():
        _print(f"  [OK]   Soul file: {soul_path}", style="green")
    else:
        _print(f"  [WARN] No soul file at {soul_path}", style="yellow")

    # Delegate to PocketPaw's health engine
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        results = engine.run_startup_checks()
        for r in results:
            if r.status == "ok":
                _print(f"  [OK]   {r.name}: {r.message}", style="green")
            elif r.status == "warning":
                _print(f"  [WARN] {r.name}: {r.message}", style="yellow")
            else:
                _print(f"  [FAIL] {r.name}: {r.message}", style="red")
    except Exception:
        _print("  [WARN] Could not run PocketPaw health checks", style="yellow")

    _print("")


# ---------------------------------------------------------------------------
# paw os
# ---------------------------------------------------------------------------


@main.command(name="os")
@click.option("--port", "-p", default=8888, type=int, help="Dashboard port (default: 8888)")
@click.option("--dev", is_flag=True, help="Development mode with auto-reload")
def launch_os(port: int, dev: bool) -> None:
    """Launch the full PocketPaw dashboard."""
    _print("Launching PocketPaw dashboard...", style="bold cyan")
    try:
        from pocketpaw.dashboard import run_dashboard

        run_dashboard(host="127.0.0.1", port=port, open_browser=True, dev=dev)
    except ImportError as e:
        _print(f"Dashboard unavailable: {e}", style="red")
    except KeyboardInterrupt:
        _print("Dashboard stopped.", style="dim")


# ---------------------------------------------------------------------------
# paw channels
# ---------------------------------------------------------------------------


@main.command()
@click.option("--telegram", is_flag=True, help="Start Telegram adapter")
@click.option("--slack", is_flag=True, help="Start Slack adapter")
@click.option("--discord", is_flag=True, help="Start Discord adapter")
def channels(telegram: bool, slack: bool, discord: bool) -> None:
    """Run headless channel adapters."""
    if not any([telegram, slack, discord]):
        _print("Specify at least one channel: --telegram, --slack, --discord", style="yellow")
        raise SystemExit(1)

    _print("Starting headless channels...", style="bold cyan")

    try:
        from pocketpaw.config import get_settings

        settings = get_settings()

        # Build a mock args namespace for the headless runner
        class _Args:
            pass

        args = _Args()
        args.telegram = telegram
        args.slack = slack
        args.discord = discord
        args.whatsapp = False
        args.signal = False
        args.matrix = False
        args.teams = False
        args.gchat = False

        if telegram and not any([slack, discord]):
            from pocketpaw.headless import run_telegram_mode

            _run_async(run_telegram_mode(settings))
        else:
            from pocketpaw.headless import run_multi_channel_mode

            _run_async(run_multi_channel_mode(settings, args))
    except ImportError as e:
        _print(f"Missing dependency: {e}", style="red")
        _print("Install channel extras: pip install pocketpaw[telegram,discord,slack]", style="dim")
    except KeyboardInterrupt:
        _print("Channels stopped.", style="dim")


if __name__ == "__main__":
    main()
