"""Diagnostic commands for PocketPaw CLI.

Extracted from __main__.py — contains --doctor, --check-ollama,
and --check-openai-compatible implementations.
"""

from importlib.metadata import version as get_version

from pocketpaw.config import Settings


async def run_doctor() -> int:
    """Run all health checks and print a polished diagnostic report.

    Returns 0 if healthy, 1 if degraded, 2 if unhealthy.
    """
    import sys

    from pocketpaw.health import get_health_engine

    w = sys.stderr.write

    # ANSI colors
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    current = get_version("pocketpaw")
    w(f"\n  {BOLD}PocketPaw Doctor{RESET} v{current}\n")
    w(f"  {'─' * 40}\n\n")

    engine = get_health_engine()

    # Run all checks (startup sync + connectivity async)
    engine.run_startup_checks()
    await engine.run_connectivity_checks()

    results = engine.results

    # Group by category
    categories: dict[str, list] = {}
    for r in results:
        cat = r.category.title()
        categories.setdefault(cat, []).append(r)

    # Print results grouped by category
    for cat_name, checks in categories.items():
        w(f"  {DIM}{cat_name}{RESET}\n")
        for r in checks:
            if r.status == "ok":
                icon = f"{GREEN}✓{RESET}"
            elif r.status == "warning":
                icon = f"{YELLOW}⚠{RESET}"
            else:
                icon = f"{RED}✗{RESET}"

            # Pad name to align messages
            padded_name = r.name.ljust(22)
            w(f"    {icon} {padded_name} {DIM}{r.message}{RESET}\n")

            if r.fix_hint and r.status != "ok":
                # Split on | for multi-part hints (e.g. "Run: ... | Changelog: ...")
                for hint in r.fix_hint.split("  |  "):
                    w(f"      {DIM}→ {hint.strip()}{RESET}\n")
        w("\n")

    # Summary line
    total = len(results)
    ok_count = sum(1 for r in results if r.status == "ok")
    warn_count = sum(1 for r in results if r.status == "warning")
    crit_count = sum(1 for r in results if r.status == "critical")

    w(f"  {'─' * 40}\n")
    w(f"  {total} checks: {GREEN}{ok_count} passed{RESET}")
    if warn_count:
        w(f", {YELLOW}{warn_count} warning{'s' if warn_count > 1 else ''}{RESET}")
    if crit_count:
        w(f", {RED}{crit_count} critical{RESET}")
    w("\n")

    status = engine.overall_status
    color = {"healthy": GREEN, "degraded": YELLOW, "unhealthy": RED}.get(status, RESET)
    w(f"  Overall: {color}{BOLD}{status.upper()}{RESET}\n\n")

    return {"healthy": 0, "degraded": 1, "unhealthy": 2}.get(status, 1)


async def check_ollama(settings: Settings) -> int:
    """Check Ollama connectivity, model availability, and tool calling support.

    Returns 0 on success, 1 on failure.
    """
    import httpx
    from rich.console import Console

    from pocketpaw.llm.client import resolve_llm_client

    console = Console()
    llm = resolve_llm_client(settings, force_provider="ollama")
    ollama_host = llm.ollama_host
    ollama_model = llm.model
    failures = 0

    # 1. Check server connectivity
    console.print(f"\n  Checking Ollama at [bold]{ollama_host}[/] ...")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{ollama_host}/api/tags")
            resp.raise_for_status()
            tags_data = resp.json()
        models = [m.get("name", "") for m in tags_data.get("models", [])]
        console.print(
            f"  [green]\\[OK][/]  Server reachable — {len(models)} model(s) available"
        )
    except Exception as e:
        console.print(f"  [red]\\[FAIL][/] Cannot reach Ollama server: {e}")
        console.print("         Make sure Ollama is running: [bold]ollama serve[/]")
        return 1

    # 2. Check configured model is available
    model_found = any(
        m == ollama_model or m.startswith(f"{ollama_model}:") for m in models
    )
    if model_found:
        console.print(f"  [green]\\[OK][/]  Model '{ollama_model}' is available")
    else:
        console.print(
            f"  [yellow]\\[WARN][/] Model '{ollama_model}' not found locally"
        )
        if models:
            console.print(f"         Available: {', '.join(models[:10])}")
        console.print(f"         Pull it with: [bold]ollama pull {ollama_model}[/]")
        failures += 1

    # 3. Test Anthropic-compatible endpoint (basic completion)
    console.print("  Testing Anthropic Messages API compatibility ...")
    try:
        ac = llm.create_anthropic_client(timeout=60.0, max_retries=1)
        response = await ac.messages.create(
            model=ollama_model,
            max_tokens=32,
            messages=[{"role": "user", "content": "Say hi"}],
        )
        text = response.content[0].text if response.content else ""
        console.print(
            f"  [green]\\[OK][/]  Messages API works — response: {text[:60]}"
        )
    except Exception as e:
        console.print(f"  [red]\\[FAIL][/] Messages API failed: {e}")
        console.print(
            "         Ollama v0.14.0+ is required for Anthropic API compatibility"
        )
        failures += 1
        # Skip tool test if basic API fails
        console.print(
            f"\n  Result: {2 - (1 if model_found else 0)}/3 checks passed"
        )
        return 1

    # 4. Test tool calling
    console.print("  Testing tool calling support ...")
    try:
        tool_response = await ac.messages.create(
            model=ollama_model,
            max_tokens=256,
            messages=[{"role": "user", "content": "What is 2 + 2?"}],
            tools=[
                {
                    "name": "calculator",
                    "description": "Performs arithmetic calculations",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "expression": {
                                "type": "string",
                                "description": "Math expression to evaluate",
                            }
                        },
                        "required": ["expression"],
                    },
                }
            ],
        )
        has_tool_use = any(b.type == "tool_use" for b in tool_response.content)
        if has_tool_use:
            console.print("  [green]\\[OK][/]  Tool calling works")
        else:
            console.print(
                "  [yellow]\\[WARN][/] Model responded but did not use the tool"
            )
            console.print(
                "         Tool calling quality varies by model."
                " Try a larger model."
            )
            failures += 1
    except Exception as e:
        console.print(f"  [yellow]\\[WARN][/] Tool calling test failed: {e}")
        console.print(
            "         Some models may not support tool calling reliably."
        )
        failures += 1

    passed = 4 - failures
    console.print(f"\n  Result: [bold]{passed}/4[/] checks passed")
    if failures == 0:
        console.print("  [green]Ollama is ready to use with PocketPaw![/]")
        console.print(
            "  Set [bold]llm_provider=ollama[/] in settings"
            " or [bold]POCKETPAW_LLM_PROVIDER=ollama[/]\n"
        )
    return 1 if failures > 1 else 0


async def check_openai_compatible(settings: Settings) -> int:
    """Check OpenAI-compatible endpoint connectivity and tool calling support.

    Returns 0 on success, 1 on failure.
    """
    from rich.console import Console

    from pocketpaw.llm.client import resolve_llm_client

    console = Console()
    llm = resolve_llm_client(settings, force_provider="openai_compatible")
    base_url = llm.openai_compatible_base_url
    model = llm.model

    if not base_url:
        console.print("\n  [red]\\[FAIL][/] No base URL configured.")
        console.print(
            "         Set [bold]POCKETPAW_OPENAI_COMPATIBLE_BASE_URL[/]"
            " or configure in Settings.\n"
        )
        return 1

    if not model:
        console.print("\n  [red]\\[FAIL][/] No model configured.")
        console.print(
            "         Set [bold]POCKETPAW_OPENAI_COMPATIBLE_MODEL[/]"
            " or configure in Settings.\n"
        )
        return 1

    failures = 0

    # 1. Test OpenAI Chat Completions API
    console.print(f"\n  Checking endpoint at [bold]{base_url}[/] ...")
    console.print(f"  Model: [bold]{model}[/]")
    console.print("  Testing Chat Completions API ...")
    try:
        oc = llm.create_openai_client(timeout=60.0, max_retries=1)
        response = await oc.chat.completions.create(
            model=model,
            max_tokens=32,
            messages=[{"role": "user", "content": "Say hi"}],
        )
        text = response.choices[0].message.content or ""
        console.print(
            f"  [green]\\[OK][/]  Chat Completions API works — response: {text[:60]}"
        )
    except Exception as e:
        console.print(f"  [red]\\[FAIL][/] Chat Completions API failed: {e}")
        console.print("\n  Result: 0/2 checks passed")
        return 1

    # 2. Test tool calling
    console.print("  Testing tool calling support ...")
    try:
        tool_response = await oc.chat.completions.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": "What is 2 + 2?"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "description": "Performs arithmetic calculations",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "expression": {
                                    "type": "string",
                                    "description": "Math expression to evaluate",
                                }
                            },
                            "required": ["expression"],
                        },
                    },
                }
            ],
        )
        has_tool_use = bool(response.choices[0].message.tool_calls)
        if has_tool_use:
            console.print("  [green]\\[OK][/]  Tool calling works")
        else:
            console.print(
                "  [yellow]\\[WARN][/] Model responded but did not use the tool"
            )
            console.print("         Tool calling quality varies by model.")
            failures += 1
    except Exception as e:
        console.print(f"  [yellow]\\[WARN][/] Tool calling test failed: {e}")
        failures += 1

    passed = 2 - failures
    console.print(f"\n  Result: [bold]{passed}/2[/] checks passed")
    if failures == 0:
        console.print("  [green]Endpoint is ready to use with PocketPaw![/]")
        console.print(
            "  Set [bold]llm_provider=openai_compatible[/] in settings"
            " or [bold]POCKETPAW_LLM_PROVIDER=openai_compatible[/]\n"
        )
    return 1 if failures > 1 else 0
