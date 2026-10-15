# ee/pocketpaw_ee/cloud/belt/app_blueprint_read.py — the readers and cache behind
# ``GET /belt/blueprints``: one repo's graph, derived by ``app_blueprint``.
#
# Plan files come from git on the bound clone (``_READ_GIT`` through executor
# ``_run``, inside ``_re_resolve_repo``'s allowlist): a fetch of dev, ``ls-tree -r
# -l`` for paths and sizes, ``cat-file`` per blueprint file (cached by blob sha) and
# a ``git grep -A`` of UITests for existing journeys and the test func after each
# ``///`` id. An app's title and its named journeys come from ``<app>.md`` (first
# ``# `` heading, the Journeys section) and every id anywhere in its wave files.
# GitHub comes through the patrols gh runner: ``feature-lead`` issues, merged
# feature-done PRs, dev CI, every lead head in one call, one issue list per repo a
# ``waits_on`` ref names, and a RECORD.md (``_PARALLEL`` at a time) only for a lead
# whose head moved since its last read and whose feature is in a wave file and not
# closed and done (issue closed, feature-done PR merged, no pending run). A RECORD
# never changes a status, so a closed, done lead keeps its last read or none. Lead
# runs come from ``service.list_runs``; an app row counts its features' slices by
# status (done-with-gaps apart from done). Cached per (workspace, repo) for ``TTL``
# behind a single-flight lock; any failed refresh (git, gh, a rate limit, a bug)
# serves the last graph with ``stale: True``, 503 with none yet. App and
# feature ids match ``_ID`` before any path or ref is built; a file over
# ``MAX_MODEL_BYTES`` is refused unread. ``read_c4`` serves one app's planned
# model off the same cache entry, with a status per component and per path.

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from . import app_blueprint as ab
from .executor import _re_resolve_repo, _run
from .orient import _dicts, _owned_system, load_model, path_index
from .service import _READ_GIT, MAX_MODEL_BYTES, BeltConsoleError

Run = Callable[[list[str]], Awaitable[tuple[int, str, str]]]
TTL = 60.0
_PARALLEL = 8  # RECORD reads in flight at once
_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_FILE = re.compile(r"docs/blueprints/([^/]+)/(?:features/([^/]+)\.wave\.json|c4-planned\.json)")
_JID = re.compile(r"[A-Z]+-[JP][0-9]+")
_DONE_BY = re.compile(r"^Feature:\s*(?:[\w-]+/)?([\w-]+)", re.M)
# ponytail: the test func must sit within 4 lines of its comment; read the blob if not
_GREP = ["grep", "-A", "4", "-E", "/// [A-Z]+-[JP][0-9]+", "origin/dev", "--", "UITests/"]
_MATCH = re.compile(r"[^:]*:([^:]+):\s*///(.*)")  # a grep hit, not a context line
_TEST = re.compile(r"func (test\w+)")
_JOURNEYS = re.compile(r"^(#+) [^\n]*journeys[^\n]*\n(.*?)(?=^\1 |\Z)", re.M | re.S | re.I)
_LIST = "--state all --limit 500 --json".split()
_STATUSES = ("done", "built", "building", "stuck", "filed", "planned", "gap")
_SLICE_STATUSES = ("done", "done-with-gaps", "building", "planned")
_UNREADY = "belt.blueprint_unavailable"
# A component's C4 status by its owners' feature status, first match wins.
_PAINT = {"stuck": "failed", "building": "changing"}
_GAP_TEXT = {
    "claimed_ahead": "wave.json says built or done, but no feature-done PR has merged",
    "issue_closed_not_done": "The feature issue is closed, but the feature is not done",
    "feature_no_owned": "wave.json lists no owned files, so nothing places the feature",
    "component_missing": "A built feature owns it, but not all its paths are on dev",
    "component_unowned": "It is not built yet, and no feature owns it",
    "journey_no_test": "A done feature names it, but no UITest carries its id",
}
logger = logging.getLogger(__name__)
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
# ponytail: one entry per blueprint file version ever read; LRU if it matters
_BLOBS: dict[str, str] = {}


class ReadFailed(Exception):
    """A git or gh read failed (ref lock, rate limit): serve the last graph."""


async def read_blueprint(
    workspace_id: str,
    repo: str,
    *,
    git: Run | None = None,
    gh: Run | None = None,
    runs: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    touched: Callable[[str, str], Awaitable[list[str]]] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """The repo's blueprint graph (design doc section 5). 404 for a repo outside
    the allowlist; 503 when the first read fails."""
    from pocketpaw_ee.cloud.mandates.patrols import _default_gh

    path, _err = _re_resolve_repo(repo)
    if path is None:
        raise BeltConsoleError(404, "Repo not found.", "belt.repo_not_found")
    key = (workspace_id, str(path))
    async with _LOCKS.setdefault(key, asyncio.Lock()):
        hit = _CACHE.get(key)
        if hit and now() - hit["at"] < TTL:
            return hit["graph"]
        io = (git or _git(path), gh or _default_gh, runs or _lead_runs, touched or _touched)
        try:
            entry = await _read(workspace_id, path, *io, hit or {})
        except Exception as exc:  # a ReadFailed, or a bug: stale or 503, never a bare 500
            if not isinstance(exc, ReadFailed):
                logger.exception("Blueprint read of %s failed", path)
            if hit is None:
                raise BeltConsoleError(503, "Blueprint not readable yet.", _UNREADY) from None
            entry = {**hit, "graph": {**hit["graph"], "stale": True}}
        _CACHE[key] = {**entry, "at": now()}
        return entry["graph"]


async def read_c4(workspace_id: str, repo: str, app: str, **io: Any) -> dict[str, Any]:
    """One app's ``c4-planned.json`` as ``{scope, model}`` and a status per
    component id: ``failed`` when a stuck feature owns it, ``changing`` when a
    building one does, ``landed`` when every path is on dev, else ``planned``.
    Each component path is a key too: ``landed`` when it is on dev, else
    ``planned``, so a partial component can mark its files one by one."""
    graph = await read_blueprint(workspace_id, repo, **io)
    entry = _CACHE[(workspace_id, graph["repo"])]
    model = entry["models"].get(app)
    if model is None:
        raise BeltConsoleError(404, "No C4 model for this app.", "belt.app_not_found")
    feats = {f["id"]: f["status"] for f in graph["features"]}
    status = {p: "landed" if hit else "planned" for p, hit in entry["paths"].get(app, {}).items()}
    for c in graph["components"]:
        if c["id"].startswith(f"{app}/"):
            owners = {feats.get(f) for f in c["features"]}
            built = "landed" if c["status"] == "built" else "planned"
            status[c["id"][len(app) + 1 :]] = next(
                (v for k, v in _PAINT.items() if k in owners), built
            )
    scoped = {"scope": model.get("scope"), "model": model["model"]}
    return {"app": app, "model": scoped, "status": status}


def _git(path: Path) -> Run:
    async def run(argv: list[str]) -> tuple[int, str, str]:
        try:
            return await _run([*_READ_GIT, *argv], cwd=path)
        except (OSError, RuntimeError) as exc:  # git missing, or the timeout
            raise ReadFailed(str(exc)) from exc

    return run


async def _lead_runs(workspace_id: str) -> list[dict[str, Any]]:
    from .service import list_runs

    return (await list_runs(workspace_id))["runs"]


async def _touched(workspace_id: str, action_id: str) -> list[str]:
    from .service import _owned_run, _touched_files

    _action, blob = await _owned_run(workspace_id, action_id)
    return await _touched_files(workspace_id, action_id, blob)


async def _ok(run: Run, argv: list[str]) -> str:
    code, out, err = await run(argv)
    if code != 0:
        raise ReadFailed(err.strip()[:200] or argv[1])
    return out


async def _rows(gh: Run, argv: list[str]) -> list[dict[str, Any]]:
    try:
        rows = json.loads(await _ok(gh, argv))
    except ValueError as exc:
        raise ReadFailed("gh printed no JSON") from exc
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


async def _record(gh: Run, slug: str, feature: str) -> str | None:
    """A lead's RECORD.md off its branch; ``None`` when absent or oversized."""
    argv = ["gh", "api", f"repos/{slug}/contents/docs/leads/{feature}/RECORD.md?ref=lead/{feature}"]
    code, out, err = await gh(argv)
    if code != 0 and "rate limit" in err.lower():
        raise ReadFailed(err.strip()[:200])
    try:
        body = json.loads(out) if code == 0 else {}
        if int(body.get("size") or 0) > MAX_MODEL_BYTES:
            return None
        return base64.b64decode(body.get("content") or "").decode("utf-8", "replace") or None
    except (ValueError, AttributeError, TypeError):
        return None


def _waits(raw: Any, slug: str, external: set[str]) -> list[str]:
    """``waits_on`` refs as ``owner/repo#n``: a chunk led by an external system's
    id in the app's model (``creative-core #171``) is that sibling repo's."""
    out: list[str] = []
    for chunk in str(raw or "").split(";"):
        head = re.match(r"\s*([a-z0-9][a-z0-9-]*) #", chunk)
        repo = f"{slug.split('/')[0]}/{head[1]}" if slug and head and head[1] in external else slug
        if "not blocking" not in chunk:
            out += [f"{repo}#{n}" for n in re.findall(r"#(\d+)", chunk)]
    return out


def _present(path: str, tree: list[str]) -> bool:
    # ponytail: a tree scan per path; index by prefix if repos get big
    return any(t == path or t.startswith(path.rstrip("/") + "/") or fnmatch(t, path) for t in tree)


async def _read(
    ws: str, path: Path, git: Run, gh: Run, runs: Any, touched: Any, prev: dict[str, Any]
) -> dict[str, Any]:
    from pocketpaw_ee.cloud.mandates.patrols import _github_slug

    await _ok(git, ["fetch", "--no-tags", "origin", "dev"])
    sha = (await _ok(git, ["rev-parse", "origin/dev"])).strip()
    tree, shas, sizes = [], {}, {}
    for line in (await _ok(git, ["ls-tree", "-r", "-l", "origin/dev"])).splitlines():
        meta, _, p = line.partition("\t")
        if len(parts := meta.split()) == 4 and parts[1] == "blob":
            tree.append(p)
            shas[p], sizes[p] = parts[2], int(parts[3]) if parts[3].isdigit() else 0
    code, grep, err = await git(_GREP)
    if code > 1:  # 1 is "no match"
        raise ReadFailed(err.strip()[:200])
    existing, tests, pending = {}, {}, []
    for ln in grep.splitlines():
        m = _MATCH.match(ln)
        if m and (ids := _JID.findall(m[2])):
            existing |= dict.fromkeys(ids, m[1])
            pending += ids
        elif t := _TEST.search(ln):
            tests |= dict.fromkeys(pending, t[1])
            pending = []
        elif ln == "--":
            pending = []

    async def blob(p: str | None) -> str | None:
        if p not in shas or sizes[p] > MAX_MODEL_BYTES:
            return None
        if shas[p] not in _BLOBS:
            _BLOBS[shas[p]] = await _ok(git, ["cat-file", "blob", shas[p]])
        return _BLOBS[shas[p]]

    apps: dict[str, dict[str, str]] = {}
    for p in tree:
        m = _FILE.fullmatch(p)
        if m and _ID.fullmatch(m[1]) and (m[2] is None or _ID.fullmatch(m[2])):
            apps.setdefault(m[1], {})[m[2] or ""] = p

    slug = prev["slug"] if "slug" in prev else await _github_slug(str(path), gh) or ""
    issues: dict[Any, dict[str, Any]] = {}
    done_prs: dict[str, list[dict[str, Any]]] = {}
    ci, heads = {}, {}
    if slug:
        leads_argv = ["gh", "issue", "list", "-R", slug, "--label", "feature-lead", *_LIST]
        for row in await _rows(gh, [*leads_argv, "number,state,labels"]):
            issues[row.get("number")] = row
        merged = ["gh", "pr", "list", "-R", slug, "--label", "feature-done", "--state", "merged"]
        for row in await _rows(gh, [*merged, "--limit", "200", "--json", "number,body"]):
            pr = {"number": row.get("number"), "state": "MERGED", "done": True}
            for f in _DONE_BY.findall(str(row.get("body") or "")):
                done_prs.setdefault(f, []).append(pr)
        dev = ["gh", "run", "list", "-R", slug, "--branch", "dev", "--limit", "5", "--json"]
        ci = next(iter(await _rows(gh, [*dev, "headSha,conclusion,url"])), {})
        for row in await _rows(gh, ["gh", "api", f"repos/{slug}/git/matching-refs/heads/lead/"]):
            f = str(row.get("ref") or "").removeprefix("refs/heads/lead/")
            if _ID.fullmatch(f):
                heads[f] = (row.get("object") or {}).get("sha")
    state = {"success": "green", "failure": "red"}.get(ci.get("conclusion"), "pending")
    dev_ci = {"sha": ci.get("headSha"), "state": state, "url": ci.get("url")} if ci else None

    leads: dict[str | None, dict[str, Any]] = {}
    in_flight = []
    for row in await runs(ws):
        if not row.get("repo") or Path(str(row["repo"])).expanduser().resolve() != path:
            continue
        lead = row.get("lead") or {}
        fid = f"{lead.get('app')}/{lead['feature']}" if lead.get("feature") else None
        leads.setdefault(fid, row)
        if row.get("status") not in ab._SETTLED:
            files = await touched(ws, str(row.get("action_id")))
            kind = "lead" if fid else "task"
            in_flight.append(
                {"action_id": row["action_id"], "kind": kind, "feature": fid, "files": files}
            )

    pending, live = {r["feature"] for r in in_flight}, set()
    for app, files in apps.items():
        for f, p in files.items():
            wave = ab.parse_wave(await blob(p) or "") if f else None
            if wave is None:
                continue
            closed = str((issues.get(wave["issue"]) or {}).get("state")).lower() == "closed"
            if not (closed and f in done_prs) or f"{app}/{f}" in pending:
                live.add(f)
    seen, kept = prev.get("heads", {}), prev.get("records", {})
    moved = [f for f in heads if f in live and heads[f] != seen.get(f)]
    read_at = {f: heads[f] if f in live else seen.get(f) for f in heads}  # skipped: as last read
    records = {f: kept.get(f) for f in heads}
    gate = asyncio.Semaphore(_PARALLEL)

    async def fetch(f: str) -> None:
        async with gate:
            records[f] = await _record(gh, slug, f)

    try:
        async with asyncio.TaskGroup() as tg:
            for f in moved:
                tg.create_task(fetch(f))
    except ExceptionGroup as eg:  # the first failure, as a lone read raises it
        raise eg.exceptions[0] from None

    features, components, journeys, gaps, app_rows, models = [], [], [], [], [], []
    c4: dict[str, dict[str, Any] | None] = {}
    on_dev: dict[str, dict[str, bool]] = {}
    for app, files in sorted(apps.items()):
        model = c4[app] = load_model(await blob(files.get("")) or "")
        owned = _owned_system(model) if model else None
        systems = _dicts(((model or {}).get("model") or {}).get("systems"))
        external = {str(s.get("id")) for s in systems if s is not owned}
        models.append((path_index(model), ab.component_layers(model)))
        md = await blob(f"docs/blueprints/{app}.md") or ""
        head = re.search(r"^# +(.+?)\s*$", md, re.M)
        title = re.sub(r"(?i)\s+blueprint$", "", head[1]) if head else app
        section = _JOURNEYS.search(md)
        mine: list[dict[str, Any]] = []
        named = {f"{app}/{j}": [] for j in _JID.findall(section[2] if section else "")}
        for f, p in sorted(files.items()):
            text = await blob(p) if f else None
            if not text or (wave := ab.parse_wave(text)) is None:
                continue
            row = issues.get(wave["issue"]) or {}
            labels = [str(x.get("name")) for x in _dicts(row.get("labels"))]
            issue = {"state": str(row.get("state")).lower(), "labels": labels} if row else None
            run, rec, prs = leads.get(f"{app}/{f}"), records.get(f), done_prs.get(f, [])
            feat = ab.derive_feature(
                app, f, text, rec, model=model, tree=tree, issue=issue, run=run, prs=prs
            )
            raw = json.loads(text)
            feat["waits_on"] = _waits(raw.get("waits_on"), slug or "", external)
            feat["journeys"] = [f"{app}/{j}" for j in dict.fromkeys(_JID.findall(text))]
            for j in feat["journeys"]:
                named.setdefault(j, []).append(feat["id"])
            mine.append(feat)
        built = {x["id"] for x in mine if x["status"] in ("built", "done")}
        finished = {x["id"] for x in mine if x["status"] == "done"}
        for c in _dicts((owned or {}).get("containers")):
            for k in _dicts(c.get("components")):
                paths = [x for x in k.get("paths") or [] if isinstance(x, str)]
                hits = [_present(x, tree) for x in paths]
                on_dev.setdefault(app, {}).update(zip(paths, hits, strict=True))
                present = sum(hits)
                status = "built" if paths and present == len(paths) else "partial"
                status = status if present else "planned"
                cid = f"{app}/{k.get('id')}"
                owners = [x["id"] for x in mine if cid in x["components"]]
                if not owners and status != "built":
                    gaps.append({"kind": "component_unowned", "ref": f"component:{cid}"})
                if status != "built" and built & set(owners):
                    gaps.append({"kind": "component_missing", "ref": f"component:{cid}"})
                layer = ab.container_layer(str(c.get("id")))
                row = {"id": cid, "container": c.get("id"), "layer": layer, "status": status}
                components.append(
                    row | {"present": present, "total": len(paths), "features": owners}
                )
        for j, owners in named.items():
            passing = built & set(owners) and state == "green"
            js = (
                "named" if j.split("/")[1] not in existing else "passing" if passing else "existing"
            )
            if js == "named" and finished & set(owners):
                gaps.append({"kind": "journey_no_test", "ref": f"journey:{j}"})
            jid = j.split("/")[1]
            row = {"id": j, "test": tests.get(jid), "file": existing.get(jid), "state": js}
            journeys.append(row | {"features": owners})
        n = [x["state"] for x in journeys if x["id"].startswith(app + "/")]
        jc = {"named": len(n), "existing": len(n) - n.count("named"), "passing": n.count("passing")}
        counts = dict.fromkeys(_STATUSES, 0) | Counter(x["status"] for x in mine)
        sc = Counter(s["status"] for x in mine for s in x["slices"])
        app_rows.append(
            {"id": app, "title": title, "counts": {"total": len(mine), **counts}, "journeys": jc}
            | {"slices": dict.fromkeys(_SLICE_STATUSES, 0) | sc}
        )
        features += mine

    states: dict[str, dict[Any, str]] = {"": {}}  # waits_on reads every issue, not only leads
    for dep in {ref.split("#")[0] for f in features for ref in f["waits_on"]} - states.keys():
        rows = await _rows(gh, ["gh", "issue", "list", "-R", dep, *_LIST, "number,state"])
        states[dep] = {r.get("number"): str(r.get("state")).lower() for r in rows}
    for f in features:
        gaps += [{"kind": g, "ref": f"feature:{f['id']}"} for g in f["gaps"]]
        refs = [ref.split("#") for ref in f["waits_on"]]
        f["waits_on"] = [
            {"ref": f"{r}#{n}", "state": states.get(r, {}).get(int(n), "unknown")} for r, n in refs
        ]
    gaps = [g | {"text": _GAP_TEXT[g["kind"]]} for g in gaps]
    for run in in_flight:  # ponytail: a file on an "other" container is not shown
        placed = {ab.file_layer(p, i, lay) for p in run.pop("files") for i, lay in models}
        run["layers"] = [x for x in ab.LAYERS if x in placed - {"other"}]
    graph = {"repo": str(path), "slug": slug, "sha": sha, "stale": False, "dev_ci": dev_ci}
    graph |= {"read_at": datetime.now(UTC).isoformat(), "layers": list(ab.LAYERS)}
    graph |= {"apps": app_rows, "features": features, "components": components}
    graph |= {"journeys": journeys, "in_flight": in_flight, "gaps": gaps}
    return {
        "graph": graph,
        "slug": slug,
        "heads": read_at,
        "records": records,
        "models": c4,
        "paths": on_dev,
    }
