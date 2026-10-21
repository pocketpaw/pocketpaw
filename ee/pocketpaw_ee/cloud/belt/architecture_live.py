# ee/pocketpaw_ee/cloud/belt/architecture_live.py — the ``live`` block of
# ``GET /belt/architecture``: what Belt and GitHub are doing to each C4 component.
#
# ``derive`` joins the reads into the block (pure). Its inputs: the workspace's
# in-flight runs on the repo with the files each touched, the open PRs (files and
# ``statusCheckRollup``, as ``gh pr list`` prints them), the trunk's ``git log
# --name-only`` over ``WINDOW``, the open ``finding`` issues and the trunk's dev
# CI. ``finding`` is the label the cloud station's critics file (its lead brief:
# "Critics file `finding` issues"); a finding maps by its ``Files:`` line.
#
# Each file joins a component by ``orient.component_for`` over the model's
# ``paths`` globs, and a signal joins every component one of its files does. A
# signal that joins none lands on ``repo``, the row for the repo as a whole.
# A row's ``status`` is the first rule that holds: ``failed`` (an open PR's
# checks are red), ``changing`` (an in-flight run), ``changed`` (a trunk commit
# in the last ``CHANGED_H`` hours; ``merges`` still counts ``WINDOW``),
# ``planned`` (the model tags it ``planned``), else ``landed``. Only in-flight
# runs are passed in, so one failed run cannot paint a component red for good;
# ``drift`` waits on loom.
#
# Rows carry ids, titles and states only, never a finding's body, a PR's file
# list or raw check rows, so creative-apps' block stays under 100 KB.

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .orient import _dicts, _owned_system, component_for, path_index, repo_path

FINDING_LABEL = "finding"
WINDOW = "7.days"
CHANGED_H = 24  # a ring reads changed this long after a trunk commit
_TEXT = 160  # a run task or a title, cut
_GATE = "local-ci/quality"
_RED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_FILES = re.compile(r"^Files:(.*)$", re.M)


def checks(rollup: Any) -> tuple[str | None, str | None]:
    """``(checks, gate)``: ``red`` when any check failed, else ``pending`` when
    any is unfinished, else ``green``; ``None`` with no checks. ``gate`` is the
    ``local-ci/quality`` status as ``"local-ci/quality: <state>"``, or ``None``."""
    seen, gate = set(), None
    for c in _dicts(rollup):
        if "context" in c:  # a commit status (StatusContext)
            state = str(c.get("state") or "").upper()
            if c["context"] == _GATE:
                gate = f"{_GATE}: {state.lower()}"
        else:  # a CheckRun: its conclusion is "" until it completes
            done = str(c.get("status") or "").upper() == "COMPLETED"
            state = str(c.get("conclusion") or "").upper() if done else ""
        seen.add("red" if state in _RED else "green" if state in _GREEN else "pending")
    return next((s for s in ("red", "pending", "green") if s in seen), None), gate


def finding_files(body: str) -> list[str]:
    """The repo paths on a finding's ``Files:`` line(s), escaping ones dropped."""
    raw = (t for line in _FILES.findall(body or "") for t in re.split(r"[\s,`]+", line))
    return list(dict.fromkeys(p for t in raw if t and (p := repo_path(t))))


def component_status(row: dict[str, Any], planned: bool, now: datetime) -> str:
    """A row's status, first rule that holds (``feature_status``'s shape)."""
    age = _age_h((row["merges"]["last"] or {}).get("at"), now)
    rules = (
        ("failed", any(p.get("checks") == "red" for p in row["prs"])),
        ("changing", bool(row["runs"])),
        ("changed", age is not None and age < CHANGED_H),
        ("planned", planned),
    )
    return next((name for name, holds in rules if holds), "landed")


def _row() -> dict[str, Any]:
    return {"runs": [], "prs": [], "merges": {"count_7d": 0, "last": None}, "findings": []}


def _age_h(created: Any, now: datetime) -> float | None:
    try:
        return round((now - datetime.fromisoformat(str(created))).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


def derive(
    model: dict[str, Any],
    *,
    runs: list[tuple[dict[str, Any], list[str]]],
    prs: list[dict[str, Any]],
    log: str,
    findings: list[dict[str, Any]],
    dev_ci: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """The live block: ``{stale, dev_ci, components: {<id>: row}, repo: row}``.
    ``runs`` pairs each in-flight run row with its files; ``log`` is the trunk's
    ``git log --name-only --format=%H%x09%s%x09%cI``, newest first."""
    index = path_index(model)
    system = _owned_system(model) or {}
    comps = [c for k in _dicts(system.get("containers")) for c in _dicts(k.get("components"))]
    comps = [c for c in comps if c.get("id")]
    rows = {str(c["id"]): _row() for c in comps}
    repo = _row()

    def homes(files: list[str]) -> list[dict[str, Any]]:
        ids = sorted({c for f in files if (c := component_for(f, index)) in rows})
        return [rows[i] for i in ids] or [repo]

    for run, files in runs:
        lead = run.get("lead") or {}
        item = {
            "action_id": run.get("action_id"),
            "task": str(run.get("title") or run.get("task") or "")[:_TEXT],
            "status": run.get("status"),
            "stage": run.get("stage"),
            "lead": {k: lead.get(k) for k in ("feature", "app", "issue")} if lead else None,
        }
        for row in homes(files):
            row["runs"].append(item)
    for pr in prs:
        state, gate = checks(pr.get("statusCheckRollup"))
        item = {
            "number": pr.get("number"),
            "title": str(pr.get("title") or "")[:_TEXT],
            "branch": pr.get("headRefName"),
            "checks": state,
            "gate": gate,
            "url": pr.get("url"),
        }
        files = [p for f in _dicts(pr.get("files")) if (p := repo_path(str(f.get("path") or "")))]
        for row in homes(files):
            row["prs"].append(item)
    commits: list[tuple[dict[str, Any], list[str]]] = []
    for line in log.splitlines():
        if len(parts := line.split("\t")) == 3:
            commits.append(({"sha": parts[0], "title": parts[1][:_TEXT], "at": parts[2]}, []))
        elif line.strip() and commits and (p := repo_path(line)):
            commits[-1][1].append(p)
    for head, files in commits:
        for row in homes(files):
            row["merges"]["count_7d"] += 1
            row["merges"]["last"] = row["merges"]["last"] or head
    for f in findings:
        item = {
            "number": f.get("number"),
            "title": str(f.get("title") or "")[:_TEXT],
            "age_h": _age_h(f.get("createdAt"), now),
            "url": f.get("url"),
        }
        for row in homes(finding_files(str(f.get("body") or ""))):
            row["findings"].append(item)
    for c in comps:
        planned = "planned" in (c.get("tags") or [])
        rows[str(c["id"])]["status"] = component_status(rows[str(c["id"])], planned, now)
    repo["status"] = component_status(repo, False, now)
    return {"stale": False, "dev_ci": dev_ci, "components": rows, "repo": repo}
