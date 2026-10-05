#!/usr/bin/env python3
# scripts/factory_digest.py — print the craft factory's morning report.
#
# Calls GET /api/v1/belt/mandates/digest on a running backend and renders the
# result as markdown: one table of mandates, then what needs a human (plan and
# diff gates), then failures (stuck headless develops of any age, failed runs,
# shifts whose plan never reached the gate), then what landed.
#
# Stdlib only (urllib). The bearer token comes from --token-file or $PAW_TOKEN
# and is never printed.
#
#   uv run python scripts/factory_digest.py --base http://localhost:8893 \
#       --token-file ~/.paw/token [--since 2026-10-05T06:00:00+00:00]

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def fetch(base: str, token: str, since: str | None) -> dict[str, Any]:
    url = base.rstrip("/") + "/api/v1/belt/mandates/digest"
    if since:
        url += "?" + urllib.parse.urlencode({"since": since})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _shifts_cell(shifts: list[dict[str, Any]]) -> str:
    return ", ".join(f"#{s['no']} {s['state']} ({s['task_count']})" for s in shifts) or "-"


def _runs_cell(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "-"
    landed = sum(1 for r in runs if r["status"] == "landed")
    return f"{len(runs)} ({landed} landed)"


def render(digest: dict[str, Any]) -> str:
    mandates = digest.get("mandates") or []
    totals = digest.get("totals") or {}
    lines = [
        f"# Factory digest since {digest.get('since', '?')}",
        "",
        f"{totals.get('mandates', 0)} mandates · {totals.get('new_sightings', 0)} new sightings"
        f" · {totals.get('shifts', 0)} shifts · {totals.get('runs', 0)} runs"
        f" ({totals.get('landed', 0)} landed, {totals.get('failed', 0)} failed)"
        f" · {totals.get('gates_waiting', 0)} gates waiting",
        "",
        "| Mandate | Cadence | Sightings | Shifts | Runs | Gates |",
        "|---|---|---|---|---|---|",
    ]
    needs: list[str] = []
    failures: list[str] = []
    landed: list[str] = []
    for m in mandates:
        name = m["name"]
        sight = m["sightings"]
        top = sight["top"][0] if sight["top"] else None
        sight_cell = f"{sight['count']}"
        if top:
            sight_cell += f" · sev {top['severity']} {_cell(top['title'])[:60]}"
        gates = m["gates"]
        n_gates = len(gates["plans"]) + len(gates["diffs"])
        lines.append(
            f"| {_cell(name)} | {m['cadence']} | {sight_cell} | {_shifts_cell(m['shifts'])}"
            f" | {_runs_cell(m['runs'])} | {n_gates or '-'} |"
        )
        for g in gates["plans"]:
            needs.append(f"- {name}: plan gate, shift {g['shift_no']} ({g['task_count']} tasks)")
        for g in gates["diffs"]:
            needs.append(f"- {name}: diff gate, {g['title']} (`{g['action_id']}`)")
        for s in m["shifts"]:
            if s["state"] == "planning" and s.get("outcome"):
                failures.append(f"- {name}: shift {s['no']} never reached the gate: {s['outcome']}")
        for r in m.get("stuck") or []:
            why = r.get("headless_error") or "develop never finished (restart or crash)"
            failures.append(f"- {name}: {r['title']}: {why}")
        for r in m["runs"]:
            if r["status"] == "failed":
                failures.append(f"- {name}: {r['title']}: run failed")
            elif r["status"] == "landed":
                where = r.get("pr_url") or r.get("branch") or r.get("commit_sha") or ""
                landed.append(f"- {name}: {r['title']}" + (f" ({where})" if where else ""))
    if not mandates:
        lines.append("| (no mandates) | | | | | |")
    for title, items in (("Needs you", needs), ("Failures", failures), ("Landed", landed)):
        lines += ["", f"## {title}", ""]
        lines += items or ["- nothing"]
    return "\n".join(lines) + "\n"


def _token(path: str | None) -> str:
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    return os.environ.get("PAW_TOKEN", "").strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print the craft factory digest as markdown.")
    ap.add_argument("--base", default="http://localhost:8893", help="backend base URL")
    ap.add_argument("--token-file", help="file holding a bearer token (else $PAW_TOKEN)")
    ap.add_argument("--since", help="ISO-8601 start of the window (default: 24h ago)")
    args = ap.parse_args(argv)

    try:
        token = _token(args.token_file)
    except OSError as exc:
        print(f"cannot read the token file: {exc.strerror}", file=sys.stderr)
        return 2
    if not token:
        print("no token: pass --token-file or set PAW_TOKEN", file=sys.stderr)
        return 2
    try:
        digest = fetch(args.base, token, args.since)
    except urllib.error.HTTPError as exc:
        print(f"digest request failed: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"digest request failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(render(digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
