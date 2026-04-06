# CLI health command - quick startup-only health check (no connectivity, fast).

from __future__ import annotations

from pocketpaw.cli.utils import (
    BOLD,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    output_json,
)


def run_health_cmd(as_json: bool = False) -> int:
    """Run startup health checks only (fast, no network). Returns 0/1/2."""
    from pocketpaw.health import get_health_engine

    engine = get_health_engine()
    results = engine.run_startup_checks()

    if as_json:
        data = {
            "status": engine.overall_status,
            "checks": [
                {
                    "id": r.check_id,
                    "name": r.name,
                    "status": r.status,
                    "message": r.message,
                }
                for r in results
            ],
        }
        output_json(data)
        return {"healthy": 0, "degraded": 1, "unhealthy": 2}.get(engine.overall_status, 1)

    print(f"\n  {BOLD}PocketPaw Health{RESET}\n")

    for r in results:
        if r.status == "ok":
            icon = f"{GREEN}[OK]{RESET}  "
        elif r.status == "warning":
            icon = f"{YELLOW}[WARN]{RESET}"
        else:
            icon = f"{RED}[FAIL]{RESET}"
        print(f"  {icon} {r.name:<22} {DIM}{r.message}{RESET}")
        if r.fix_hint and r.status != "ok":
            print(f"         {DIM}-> {r.fix_hint}{RESET}")

    status = engine.overall_status
    color = {"healthy": GREEN, "degraded": YELLOW, "unhealthy": RED}.get(status, RESET)
    print(f"\n  Overall: {color}{BOLD}{status.upper()}{RESET}\n")

    return {"healthy": 0, "degraded": 1, "unhealthy": 2}.get(status, 1)
