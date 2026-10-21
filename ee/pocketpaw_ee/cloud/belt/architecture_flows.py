# ee/pocketpaw_ee/cloud/belt/architecture_flows.py — ``flows`` and ``warnings``
# for ``GET /belt/architecture``: ``docs/c4/flows.json`` at the read's sha,
# checked against the repo's C4 model. ``flows.schema.json`` beside this file is
# the published contract; ``_flow_problem`` checks it by hand (no jsonschema at
# runtime), so change both together. A flow off the schema or naming an id the
# model lacks is dropped with ``{flow, step, problem}`` warnings; a message hop
# with no model relationship is kept as ``implied`` (``reverse`` checks the
# relationship the other way). Content never raises. ``load_flows`` sizes the
# blob with ``git ls-tree -l`` before reading it; only an empty listing means no
# file, and any other git failure is a warning.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .executor import _run
from .service import _READ_GIT, MAX_MODEL_BYTES

FLOWS_PATH = "docs/c4/flows.json"
# The step kinds of flows.schema.json: (required keys, optional keys).
_KINDS = {
    "message": ({"from", "to", "label"}, {"technology", "reverse"}),
    "process": ({"at", "label"}, set()),
    "note": ({"note"}, set()),
}
# Each string key's (min, max) length, as the schema's defs and caps set them.
_STR = {k: (1, 200) for k in ("id", "from", "to", "at")}
_STR |= {k: (1, 2000) for k in ("name", "label", "note")}
_STR |= {"description": (0, 2000), "technology": (0, 200)}


def _warn(flow: str | None, step: int | None, problem: str) -> dict[str, Any]:
    return {"flow": flow, "step": step, "problem": problem}


def _dicts(value: Any) -> list[dict]:
    """The dict items of ``value`` when it is a list, else none: model.json is
    repo content, so any level may be the wrong shape."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _index(model: dict[str, Any]) -> tuple[set[str], set[tuple]]:
    """The model's element ids and its ``(source, target)`` relationships."""
    body = model.get("model")
    body = body if isinstance(body, dict) else {}
    rels = {(r.get("source"), r.get("target")) for r in _dicts(body.get("relationships"))}
    ids = {p.get("id") for p in _dicts(body.get("people"))}
    for s in _dicts(body.get("systems")):
        ids.add(s.get("id"))
        for c in _dicts(s.get("containers")):
            ids.add(c.get("id"))
            ids.update(x.get("id") for x in _dicts(c.get("components")))
    return {i for i in ids if isinstance(i, str)}, rels


def _keys(obj: Any, required: set[str], optional: set[str], where: str) -> str | None:
    if not isinstance(obj, dict):
        return f"{where}: not an object"
    if missing := sorted(required - obj.keys()):
        return f"{where}: missing {', '.join(missing)}"
    if extra := sorted(obj.keys() - required - optional):
        return f"{where}: unknown key {', '.join(map(str, extra))}"
    for key in obj.keys() & _STR.keys():
        lo, hi = _STR[key]
        if not isinstance(obj[key], str) or not lo <= len(obj[key]) <= hi:
            return f"{where}/{key}: must be a string of {lo} to {hi} characters"
    return None


def _flow_problem(flow: Any, where: str) -> str | None:
    """Why ``flow`` breaks flows.schema.json's ``flow`` def, or ``None``."""
    if problem := _keys(flow, {"id", "name", "steps"}, {"description"}, where):
        return problem
    steps = flow["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 200:
        return f"{where}/steps: must be a list of 1 to 200 steps"
    for n, step in enumerate(steps):
        at = f"{where}/steps/{n}"
        kinds = [
            k for k, (req, _) in _KINDS.items() if isinstance(step, dict) and req <= step.keys()
        ]
        if len(kinds) != 1:
            return f"{at}: must be exactly one of a message, process or note step"
        if problem := _keys(step, *_KINDS[kinds[0]], at):
            return problem
        if "reverse" in step and not isinstance(step["reverse"], bool):
            return f"{at}/reverse: must be true or false"
    return None


def check(data: Any, model: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """``(flows, warnings)`` for a parsed flows file against a C4 model."""
    if not isinstance(data, dict) or not isinstance(data.get("flows"), list):
        return [], [_warn(None, None, 'flows.json: must be {"flows": [...]}')]
    ids, rels = _index(model)
    flows: list[dict] = []
    warnings: list[dict] = []
    for i, flow in enumerate(data["flows"]):
        name = flow.get("id") if isinstance(flow, dict) else None
        name = name if isinstance(name, str) else None
        if problem := _flow_problem(flow, f"flows/{i}"):
            warnings.append(_warn(name, None, problem))
            continue
        problems = [
            _warn(name, n, f"{key} {step[key]!r} is not in the model")
            for n, step in enumerate(flow["steps"])
            for key in ("from", "to", "at")
            if key in step and step[key] not in ids
        ]
        if problems:
            warnings += problems
            continue
        steps = []
        for step in flow["steps"]:
            if "from" in step:
                hop = (step["from"], step["to"])[:: -1 if step.get("reverse") else 1]
                step = {**step, "implied": hop not in rels}
            steps.append(step)
        flows.append({**flow, "steps": steps})
    return flows, warnings


async def load_flows(repo_path: Path, sha: str, model: dict[str, Any]) -> tuple[list, list]:
    """``check`` of ``docs/c4/flows.json`` at ``sha``; ``([], [])`` without one.
    Raises only what ``_run`` raises (git missing, a timeout)."""
    # ls-tree, not cat-file -s: an empty listing is the one "no file" answer,
    # where cat-file says "does not exist" for an unknown sha too.
    argv = [*_READ_GIT, "ls-tree", "-l", sha, "--", FLOWS_PATH]
    code, entry, err = await _run(argv, cwd=repo_path)
    if code != 0:
        return [], [_warn(None, None, f"flows.json is unreadable: {err.strip()[:200]}")]
    if not entry.strip():
        return [], []
    _mode, kind, obj, size = entry.split("\t", 1)[0].split()
    if kind != "blob":
        return [], [_warn(None, None, f"flows.json is a {kind}, not a file")]
    if int(size) > MAX_MODEL_BYTES:
        return [], [_warn(None, None, f"flows.json is over {MAX_MODEL_BYTES} bytes")]
    code, text, err = await _run([*_READ_GIT, "cat-file", "blob", obj], cwd=repo_path)
    if code != 0:
        return [], [_warn(None, None, f"flows.json is unreadable: {err.strip()[:200]}")]
    try:
        data = json.loads(text)
    except ValueError as e:
        return [], [_warn(None, None, f"flows.json is not JSON: {e}")]
    return check(data, model)
