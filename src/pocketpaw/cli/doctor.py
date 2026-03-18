# CLI doctor command - full diagnostic report with connectivity checks.

from __future__ import annotations

from pocketpaw.cli.utils import output_json


async def run_doctor_cmd(as_json: bool = False) -> int:
    """Run full diagnostics (startup + connectivity). Returns 0/1/2."""
    if as_json:
        return await _doctor_json()

    # Delegate to the existing rich-formatted doctor
    from pocketpaw.diagnostics import run_doctor

    return await run_doctor()


async def _doctor_json() -> int:
    from pocketpaw.health import get_health_engine

    engine = get_health_engine()
    engine.run_startup_checks()
    await engine.run_connectivity_checks()

    data = {
        "status": engine.overall_status,
        "checks": [
            {
                "id": r.check_id,
                "name": r.name,
                "category": r.category,
                "status": r.status,
                "message": r.message,
                "fix_hint": r.fix_hint,
            }
            for r in engine.results
        ],
    }
    output_json(data)
    return {"healthy": 0, "degraded": 1, "unhealthy": 2}.get(engine.overall_status, 1)
