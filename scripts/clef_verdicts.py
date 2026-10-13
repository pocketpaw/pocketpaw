#!/usr/bin/env python3
# scripts/clef_verdicts.py — Clef verdicts beside what actually happened.
#
# Reads the workspace's Instinct store in-process (run it on the host that holds
# ``~/.pocketpaw/workspaces/<id>/instinct.db``) through
# ``pocketpaw_ee.cloud.belt.clef.list_verdicts`` and prints one row per scored
# plan, diff or finding with its outcome: the Action's status for a plan or diff
# (a diff that executed with a PR or commit reads ``landed``); for a finding tied
# to an issue, what was done with that issue (open, fixed, wont_fix, duplicate),
# read from GitHub with gh (``clef.resolve_outcomes``; ``--offline`` skips it).
# Then agreement per decision kind (``clef.agreement``). This is the eval input
# for deciding whether Clef ever earns a gate.
#
#   uv run python scripts/clef_verdicts.py --workspace <workspace_id> [--json] [--offline]

from __future__ import annotations

import argparse
import asyncio
import json

from pocketpaw_ee.cloud.belt.clef import agreement, list_verdicts, resolve_outcomes
from pocketpaw_ee.cloud.mandates.patrols import _default_gh


async def _rows(workspace: str, limit: int, offline: bool) -> list[dict]:
    rows = await list_verdicts(workspace, limit)
    return rows if offline else await resolve_outcomes(rows, _default_gh)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true", help="skip reading issue outcomes")
    args = ap.parse_args()
    rows = asyncio.run(_rows(args.workspace, args.limit, args.offline))
    if args.json:
        print(json.dumps({"rows": rows, "agreement": agreement(rows)}, indent=1))
        return
    for r in rows:
        v = ", ".join(
            f"{k}={val:.2f}" if isinstance(val, float) else f"{k}={val}"
            for k, val in r["verdict"].items()
        )
        kind = f"{r['kind']}/{r['source']}" if r["kind"] == "finding" else r["kind"]
        where = f"  {r['issue']}" if r.get("issue") else ""
        print(f"{r['scored_at']}  {kind:14}  {r['outcome'] or '?':9}  {r['action_id']}{where}  {v}")
    print(f"{len(rows)} verdict(s)")
    for decision, (agreed, judged) in agreement(rows).items():
        print(f"agreement {decision}: {agreed}/{judged}")


if __name__ == "__main__":
    main()
