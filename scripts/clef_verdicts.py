#!/usr/bin/env python3
# scripts/clef_verdicts.py — Clef shadow verdicts beside what actually happened.
#
# Reads the workspace's Instinct store in-process (run it on the host that holds
# ``~/.pocketpaw/workspaces/<id>/instinct.db``) through
# ``pocketpaw_ee.cloud.belt.clef.list_verdicts`` and prints one row per scored
# plan or diff: the verdict probabilities and the outcome (the Action's status;
# a diff that executed with a PR or commit reads ``landed``). This is the eval
# input for deciding whether Clef ever earns a gate.
#
#   uv run python scripts/clef_verdicts.py --workspace <workspace_id> [--json]

from __future__ import annotations

import argparse
import asyncio
import json

from pocketpaw_ee.cloud.belt.clef import list_verdicts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = asyncio.run(list_verdicts(args.workspace, args.limit))
    if args.json:
        print(json.dumps(rows, indent=1))
        return
    for r in rows:
        v = ", ".join(
            f"{k}={val:.2f}" if isinstance(val, float) else f"{k}={val}"
            for k, val in r["verdict"].items()
        )
        print(f"{r['scored_at']}  {r['kind']:4}  {r['outcome']:9}  {r['action_id']}  {v}")
    print(f"{len(rows)} verdict(s)")


if __name__ == "__main__":
    main()
