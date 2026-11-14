# ee/pocketpaw_ee/cloud/belt/app_blueprint.py — the Belt blueprint's pure derivation.
#
# Turns one app's plan files (.wave.json, c4-planned.json via ``orient.load_model``,
# a lead's RECORD.md read by header name) plus the GitHub and Belt evidence the
# caller already read into the feature rows of ``GET /belt/blueprints``. No I/O, no
# clock, no network; size caps and id validation belong to the readers. Unreadable
# input gives ``None`` fields, never a guess. Layers follow the design doc's
# section 3 (container id, first match wins; ``Tests/``/``UITests/`` files are tests
# first; a feature sits on its ``owned`` paths, else its tasks, else "unknown").
# Status follows section 1: the first rule that holds wins, and the wave.json
# ``status`` is only a claim (a built or done claim with no merged feature-done PR
# is the ``claimed_ahead`` gap). The issue's newest contact-sheet or
# Mac-Finish-Wanted line splits the red: a sheet is done, a parked issue whose
# last word is Mac work is ``waiting``, and only the rest is ``stuck``. A v3
# feature's wave.json ``slices`` each get a status (done, done-with-gaps,
# building, planned) off its lead run's ``lead``. ``suite_map`` joins the suite
# feature map (creative-apps ``docs/blueprints/suite/feature-map.json``) to those
# feature statuses: a row's status comes only from its ``packet`` id, ``writing``
# when that packet is not in the graph, ``new`` with none; no second status rule.

from __future__ import annotations

import json
import posixpath
import re
from collections import Counter
from typing import Any

from .orient import PathIndex, _dicts, _owned_system, component_for, path_index

LAYERS = ("tests", "app", "shell", "model", "engine", "other")
_LAYER_RULES = (
    ("tests", re.compile(r".*-(tests|journeys|snapshots)|app-audit")),
    ("shell", re.compile(r"suiteui.*|suite-shell|afterglow-.*")),
    ("model", re.compile(r".*-model|suite-logic")),
    ("app", re.compile(r".*-app|apps")),
)
_FIELD = re.compile(r"^(Stage|Next):[ \t]*(\S.*?)[ \t]*$", re.M)
# A wave.json slice id (``CT-a``, ``S1``): what Belt builds lines and argv from.
SLICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_SETTLED = {"landed", "rejected", "failed"}


def parse_wave(text: str) -> dict[str, Any] | None:
    """A feature's ``.wave.json`` as plan fields, or ``None`` when it is not a
    JSON object. ``issue`` is the ``feature_issue`` number only when it reads
    ``#N`` exactly ("file when ready" and "later" are not filed). ``slices`` (a
    v3 packet's) keeps each entry with a string ``id``, unvalidated, and its
    ``waits`` keys (``<feature>``, ``<feature>:<slice>`` or a sibling slice)."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    issue = re.fullmatch(r"#(\d+)", str(raw.get("feature_issue") or "").strip())
    waits = [c for c in str(raw.get("waits_on") or "").split(";") if "not blocking" not in c]
    owned, slices = raw.get("owned"), raw.get("slices")
    slices = slices if isinstance(slices, list) else []
    return {
        "title": raw.get("title"),
        "wave": str(raw.get("wave") or "").removeprefix("W"),
        "order": raw.get("order"),
        "claimed": raw.get("status"),
        "issue": int(issue.group(1)) if issue else None,
        "depends": [d for d in raw.get("depends") or [] if isinstance(d, str)],
        # ponytail: refs are plain numbers; B3 resolves which repo each lives in
        "waits_on": [int(n) for c in waits for n in re.findall(r"#(\d+)", c)],
        "owned": [p for p in owned if isinstance(p, str)] if isinstance(owned, list) else [],
        "slices": [
            {
                "id": s["id"],
                "behavior": s.get("behavior"),
                **{
                    k: [x for x in s[k] if isinstance(x, str)]
                    if isinstance(s.get(k), list | dict)  # waits is {ref: [task ids]}
                    else []
                    for k in ("tasks", "journeys", "waits")
                },
            }
            for s in slices
            if isinstance(s, dict) and isinstance(s.get("id"), str)
        ],
    }


def parse_record(text: str | None) -> dict[str, Any]:
    """A lead's RECORD.md: ``stage`` and ``next`` from their header lines (``None``
    when absent), and the rows of the table whose header has ``id`` and
    ``files``. A ``same as <id>`` files cell takes that task's files."""
    if not isinstance(text, str):
        return {"stage": None, "next": None, "tasks": []}
    fields = dict(_FIELD.findall(text))
    tasks: list[dict[str, Any]] = []
    files_of: dict[str, list[str]] = {}
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            names = [c.lower() for c in cells]
            header = names if {"id", "files"} <= set(names) else []
            continue
        if not header or set(cells[0]) <= set("-: "):
            continue
        row = dict(zip(header, cells, strict=False))
        cell = row.get("files", "")
        files = [f for f in map(str.strip, cell.split(",")) if f]
        if same := re.match(r"same as (\S+)", cell):
            files = files_of.get(same.group(1), [])
        files_of[row.get("id", "")] = files
        pr = re.fullmatch(r"#(\d+)", row.get("pr", ""))
        task = {k: row.get(k) or None for k in ("id", "wave", "state")}
        tasks.append(task | {"files": files, "pr": int(pr.group(1)) if pr else None})
    return {"stage": fields.get("Stage"), "next": fields.get("Next"), "tasks": tasks}


def resolve_files(tokens: list[str], tree: list[str]) -> list[str]:
    """RECORD file tokens (``Live/HaloKeyRules``, ``HaloKeying.swift``) as repo
    paths: every tree path whose extensionless form is the token's, or ends in
    ``/`` plus it. A token that names nothing in the tree is dropped."""
    out: list[str] = []
    for tok in tokens:
        stem = posixpath.splitext(tok)[0]
        out += [
            p for p in tree if (s := posixpath.splitext(p)[0]) == stem or s.endswith("/" + stem)
        ]
    return list(dict.fromkeys(out))


def container_layer(container: str, external: bool = False) -> str:
    """The line-view layer of a C4 container id; first matching rule wins."""
    if external:
        return "engine"
    return next((name for name, rx in _LAYER_RULES if rx.fullmatch(container)), "other")


def component_layers(model: dict[str, Any] | None) -> dict[str, str]:
    """Every component id in a C4 model mapped to its container's layer."""
    systems = _dicts(((model or {}).get("model") or {}).get("systems"))
    owned = _owned_system(model) if model else None
    return {
        str(k["id"]): container_layer(str(c.get("id") or ""), external=s is not owned)
        for s in systems
        for c in _dicts(s.get("containers"))
        for k in _dicts(c.get("components"))
        if k.get("id")
    }


def file_layer(path: str, index: PathIndex, layers: dict[str, str]) -> str:
    if path.startswith(("Tests/", "UITests/")):
        return "tests"
    return layers.get(component_for(path, index) or "", "other")


def task_layer(files: list[str], index: PathIndex, layers: dict[str, str]) -> str:
    """The layer most of a task's non-test files sit on (ties: the first file's),
    ``tests`` when all are test files, ``unknown`` with none."""
    placed = [file_layer(p, index, layers) for p in files]
    rest = Counter(p for p in placed if p != "tests")
    return rest.most_common(1)[0][0] if rest else ("tests" if placed else "unknown")


def feature_status(
    claimed: Any,
    issue_number: int | None,
    issue: dict[str, Any] | None,
    run: dict[str, Any] | None,
    prs: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """``(status, gap kinds)``. ``issue`` is ``{state, labels}``, plus ``mark`` when
    the read looked at its comments: ``sheet`` or ``mac``, whichever of a
    ``Belt-Contact-Sheet:`` or ``Mac-Finish-Wanted:`` line came last. ``run`` is the
    newest lead run row (console ``status``, ``lead`` with ``prs`` and the int
    ``stalls``); ``prs`` the merged feature-done PRs that name the feature, as
    ``{number, state, done}`` rows like ``lead.prs``. A sheet is done even when
    its run was released; ``needs-human`` with Mac work last is ``waiting`` (the
    Mac finisher's), any other ``needs-human`` or 2+ stalls or a settled run is
    ``stuck``."""
    issue, run = issue or {}, run or {}
    lead = run.get("lead") or {}
    closed = str(issue.get("state") or "").lower() == "closed"
    prs = [*prs, *(lead.get("prs") or [])]
    done_pr = any(p.get("done") and p.get("state") == "MERGED" for p in prs)
    ahead = claimed in ("built", "done") and not done_pr
    stalls = lead.get("stalls") if isinstance(lead.get("stalls"), int) else 0
    parked = "needs-human" in (issue.get("labels") or [])
    stuck = parked or stalls >= 2
    rules = (
        ("done", run.get("status") == "landed" or (closed and done_pr)),
        ("done", issue.get("mark") == "sheet"),
        ("built", done_pr),
        ("gap", ahead or closed),
        ("waiting", parked and issue.get("mark") == "mac"),
        ("stuck", stuck or run.get("status") in _SETTLED),
        ("building", bool(run)),
        ("filed", issue_number is not None),
    )
    status = next((name for name, holds in rules if holds), "planned")
    gaps = ["claimed_ahead"] if ahead else []
    return status, gaps + (["issue_closed_not_done"] if closed and status != "done" else [])


def slice_status(slices: list[dict[str, Any]], run: dict[str, Any] | None) -> list[dict[str, Any]]:
    """A feature's wave.json ``slices`` (safe ids only), each with its ``status``:
    ``done`` or ``done-with-gaps`` as the lead run's ``lead.slices`` says,
    ``building`` when a live lead has a PR whose ``Slice:`` line names it, else
    ``planned``; plus that slice's ``run`` url and ``gaps`` off the run."""
    run = run or {}
    lead = run.get("lead") or {}
    state = {s.get("id"): s for s in lead.get("slices") or [] if isinstance(s, dict)}
    live = run.get("status") not in _SETTLED
    named = {i for p in lead.get("prs") or [] for i in p.get("slices") or []} if live else set()
    rows = []
    for s in slices:
        if not SLICE_ID.match(s["id"]):
            continue
        got = state.get(s["id"]) or {}
        gaps = got.get("gaps") or []
        status = "planned"
        if got.get("done"):
            status = "done-with-gaps" if gaps else "done"
        elif s["id"] in named:
            status = "building"
        rows.append(s | {"status": status, "run": got.get("run"), "gaps": gaps})
    return rows


def derive_feature(
    app: str,
    feature: str,
    wave_text: str,
    record_text: str | None,
    *,
    model: dict[str, Any] | None,
    tree: list[str],
    issue: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
    prs: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """One feature row of the blueprint graph (``None``: unreadable wave.json).
    ``model`` is the app's loaded c4-planned.json, ``tree`` dev's file paths.
    Each task carries its resolved ``files`` and its ``layer``. ``issue`` always
    has ``state`` (``unknown`` when gh did not list it) and ``labels``; ``run``'s
    ``record_at`` is ``None`` until something read carries the RECORD's time."""
    wave = parse_wave(wave_text)
    if wave is None:
        return None
    rec = parse_record(record_text)
    index, layers = path_index(model), component_layers(model)
    owned = wave["owned"]
    tasks = rec["tasks"]
    for t in tasks:  # owned paths resolve tokens for files not on dev yet
        t["files"] = resolve_files(t["files"], [*tree, *owned])
        t["layer"] = task_layer(t["files"], index, layers)
    placed = {file_layer(p, index, layers) for p in owned}
    placed = placed or {t["layer"] for t in tasks} or {"unknown"}
    comps = (component_for(p, index) for p in owned or [f for t in tasks for f in t["files"]])
    status, gaps = feature_status(wave["claimed"], wave["issue"], issue, run, prs or [])
    return {
        "id": f"{app}/{feature}",
        "app": app,
        **{k: wave[k] for k in ("title", "wave", "order", "claimed", "depends", "waits_on")},
        "status": status,
        "issue": {"state": "unknown", "labels": [], **(issue or {}), "number": wave["issue"]}
        if wave["issue"]
        else None,
        "run": {"action_id": run.get("action_id"), "stage": rec["stage"], "next": rec["next"]}
        | {"record_at": None}
        if run
        else None,
        "tasks": tasks,
        "slices": slice_status(wave["slices"], run),
        "layers": [x for x in (*LAYERS, "unknown") if x in placed],
        "components": list(dict.fromkeys(f"{app}/{c}" for c in comps if c)),
        "gaps": gaps + ([] if owned else ["feature_no_owned"]),
    }


# A suite map row's status: a feature status off its packet, ``writing`` when the
# packet's wave.json is not on dev yet, ``new`` with no packet.
MAP_STATUSES = ("done", "built", "building", "waiting", "stuck", "gap", "filed", "planned")
MAP_STATUSES += ("writing", "new")


def suite_map(raw: Any, live: dict[str, str]) -> dict[str, Any] | None:
    """creative-apps' ``docs/blueprints/suite/feature-map.json`` joined to ``live``
    (graph feature id to status): ``{layers, apps, features, counts}``, or ``None``
    when ``raw`` is not a map. Each feature and adopter takes its status from its
    own ``packet`` (an ``<app>/<feature>`` id) and nothing else; a row without a
    string ``id`` and ``layer`` is dropped. ``counts`` covers features only."""
    if not isinstance(raw, dict) or not all(
        isinstance(raw.get(k), list) for k in ("layers", "apps", "features")
    ):
        return None

    def status(packet: Any) -> str:
        return live.get(packet, "writing") if isinstance(packet, str) and packet else "new"

    def text(v: Any) -> str | None:
        return v if isinstance(v, str) and v else None

    features = []
    for f in _dicts(raw["features"]):
        if not (text(f.get("id")) and text(f.get("layer"))):
            continue
        adopters = [
            {"app": a["app"], "packet": text(a.get("packet")), "status": status(a.get("packet"))}
            for a in _dicts(f.get("adopters"))
            if text(a.get("app"))
        ]
        c4 = [c for c in f["c4"] if isinstance(c, str)] if isinstance(f.get("c4"), list) else []
        row = {"id": f["id"], "name": text(f.get("name")) or f["id"], "layer": f["layer"]}
        row |= {"app": text(f.get("app")), "c4": c4, "packet": text(f.get("packet"))}
        features.append(row | {"status": status(f.get("packet")), "adopters": adopters})
    counts = dict.fromkeys(MAP_STATUSES, 0) | Counter(f["status"] for f in features)

    def named(key: str) -> list[dict[str, str]]:
        rows = (x for x in _dicts(raw[key]) if text(x.get("id")))
        return [{"id": x["id"], "name": text(x.get("name")) or x["id"]} for x in rows]

    return {
        "layers": named("layers"),
        "apps": named("apps"),
        "features": features,
        "counts": counts,
    }
