# ee/pocketpaw_ee/sites/project_tools.py — the service layer behind a project site's
# files: the paw-sites catalogue reads (``starters --json``, ``recipes --json``),
# ``template-copy`` and ``apply-recipe`` against a temp dir, the recipe plan gate, and
# the file operations (list / read / write / patch / delete) that BOTH the agent's
# tools (``agent/mcp_servers/sites_project.py``) and the HTTP routes
# (``sites/files_router.py``) call, so the two share one path policy, one set of caps
# and one rebuild rule.
#
# The four CLI commands here install nothing and run no author code: they copy and
# edit files. Only ``project-build`` runs author code, and that stays in the Daytona
# sandbox (``project_build``). Every temp dir is removed before the call returns.
#
# Invariants:
#   * a path a tool may write is relative, POSIX, inside the project, and not reserved
#     (``node_modules`` / ``.git`` / the ``.paw`` stage dir / ``paw-build.json`` / a
#     real ``.dev.vars`` or ``.env`` file: secret VALUES never live in the source map);
#   * the source map stays text: a template that ships a binary file is refused;
#   * a change to package.json's dependency lists drops the lockfile, because the
#     sandbox installs with ``--frozen-lockfile`` when one exists and nothing on this
#     side can regenerate it;
#   * a recipe whose ``plan`` the site does not have is refused before anything runs;
#   * every file operation resolves the pocket through ``sites.service.project_pocket``
#     (read rule + workspace match + engine ``project``), writes through
#     ``pockets.service.set_project_source`` (edit access), and queues the draft build
#     of the new source, so a write always answers ``verification``.
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.sites import project_build
from pocketpaw_ee.sites.engines import safe_rel

logger = logging.getLogger(__name__)

#: One catalogue read, copy or recipe apply. They only touch files, so a minute is
#: generous; a run past it is killed (by its own pid) and reported.
CLI_TIMEOUT_SEC = 60

#: Size caps for the generic file tools.
MAX_WRITE_FILE_BYTES = 1024 * 1024
MAX_WRITE_CALL_BYTES = 4 * 1024 * 1024
MAX_PATHS_PER_CALL = 200
MAX_READ_FILE_BYTES = 200_000
MAX_READ_CALL_BYTES = 400_000
MAX_PATH_CHARS = 512

#: Lockfiles a dependency change makes stale.
LOCKFILES = ("bun.lock", "bun.lockb", "package-lock.json", "pnpm-lock.yaml", "yarn.lock")
_DEP_KEYS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
    "overrides",
    "resolutions",
    "workspaces",
)

_RESERVED_SEGMENTS = frozenset({"node_modules", ".git"})
_SECRET_FILES = frozenset({".dev.vars", ".env"})

#: Recipe plans (paw-sites ``RECIPE_PLANS``), lowest first.
RECIPE_PLANS = ("free", "site", "staff")

# The subprocess seam. Tests replace it with a fake that plays the CLI.
_create_subprocess_exec = asyncio.create_subprocess_exec


class ProjectCliError(Exception):
    """A paw-sites CLI call that did not produce a usable answer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def cli_argv() -> list[str]:
    """The generator invocation: ``node <PAW_SITES_GEN_DIR>/dist/cli.js`` (bun when
    there is no node) for the vendored copy, else ``PAW_SITES_GEN_CMD``."""
    root = project_build.generator_dir()
    if root is not None:
        runner = shutil.which("node") or shutil.which("bun")
        if runner:
            return [runner, str(root / "dist" / "cli.js")]
    from pocketpaw_ee.sites.generator_client import _gen_cmd_argv

    return _gen_cmd_argv()


def _catalogue_root(name: str) -> list[str]:
    """``--root <gen>/<name>`` when the vendored copy has that dir; the CLI's own
    default (next to its dist) otherwise."""
    root = project_build.generator_dir()
    if root is not None and (root / name).is_dir():
        return ["--root", str(root / name)]
    return []


def _last_json(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


async def run_cli(
    args: list[str], *, timeout: float = CLI_TIMEOUT_SEC
) -> tuple[int, dict[str, Any] | None, str]:
    """Run one CLI command; ``(exit code, last JSON line of stdout, stderr tail)``."""
    try:
        proc = await _create_subprocess_exec(
            *cli_argv(),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ProjectCliError(
            "generator_missing", f"The site generator could not be started ({exc})."
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError as exc:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), 5)
        except (ProcessLookupError, TimeoutError):
            pass
        raise ProjectCliError(
            "timeout", f"The site generator did not answer within {int(timeout)}s."
        ) from exc
    err = (stderr or b"").decode("utf-8", "replace")[-2000:]
    return proc.returncode or 0, _last_json((stdout or b"").decode("utf-8", "replace")), err


# ---------------------------------------------------------------------------
# Catalogues (cached per process: they only change with a new image)
# ---------------------------------------------------------------------------

_templates_cache: list[dict[str, Any]] | None = None
_recipes_cache: list[dict[str, Any]] | None = None


def reset_caches() -> None:
    global _templates_cache, _recipes_cache
    _templates_cache = None
    _recipes_cache = None


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


async def list_templates() -> list[dict[str, Any]]:
    """The base templates: ``slug, name, summary, when_to_use, stack, target, recipes``
    from ``starters --json``. Invalid starters are left out (the CLI lists them apart)."""
    global _templates_cache
    if _templates_cache is not None:
        return _templates_cache
    _code, payload, err = await run_cli(["starters", "--json", *_catalogue_root("starters")])
    starters = payload.get("starters") if isinstance(payload, dict) else None
    if not isinstance(starters, list):
        raise ProjectCliError("catalogue_unavailable", f"The template list is unavailable. {err}")
    out = []
    for s in starters:
        if not isinstance(s, dict) or not isinstance(s.get("slug"), str):
            continue
        out.append(
            {
                "slug": s["slug"],
                "name": s.get("name") or s["slug"],
                "summary": s.get("summary") or "",
                "when_to_use": _strings(s.get("when_to_use")),
                "stack": _strings(s.get("stack")),
                "target": s.get("target"),
                "recipes": _strings(s.get("recipes")),
            }
        )
    _templates_cache = out
    return out


async def list_recipes() -> list[dict[str, Any]]:
    """The backend recipes from ``recipes --json``: id, name, summary, applies_to,
    unsupported (template -> why not), requires, conflicts, plan, binding requests (a
    ``do`` binding carries its ``class_name``), secret and env names."""
    global _recipes_cache
    if _recipes_cache is not None:
        return _recipes_cache
    _code, payload, err = await run_cli(["recipes", "--json", *_catalogue_root("recipes")])
    recipes = payload.get("recipes") if isinstance(payload, dict) else None
    if not isinstance(recipes, list):
        raise ProjectCliError("catalogue_unavailable", f"The recipe list is unavailable. {err}")
    out = []
    for r in recipes:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str):
            continue
        out.append(
            {
                "id": r["id"],
                "name": r.get("name") or r["id"],
                "summary": r.get("summary") or "",
                "applies_to": _strings(r.get("applies_to")),
                "unsupported": {
                    k: v
                    for k, v in (r.get("unsupported") or {}).items()
                    if isinstance(k, str) and isinstance(v, str)
                }
                if isinstance(r.get("unsupported"), dict)
                else {},
                "requires": _strings(r.get("requires")),
                "conflicts": _strings(r.get("conflicts")),
                "plan": r.get("plan") if r.get("plan") in RECIPE_PLANS else "staff",
                "bindings": [
                    {
                        "type": b.get("type"),
                        "name": b.get("name"),
                        **({"class_name": b["class_name"]} if b.get("class_name") else {}),
                    }
                    for b in r.get("bindings") or []
                    if isinstance(b, dict)
                ],
                "secrets": [
                    {
                        "name": s.get("name"),
                        "required": bool(s.get("required")),
                        "description": s.get("description") or "",
                    }
                    for s in r.get("secrets") or []
                    if isinstance(s, dict)
                ],
                "env": [e.get("name") for e in r.get("env") or [] if isinstance(e, dict)],
            }
        )
    _recipes_cache = out
    return out


async def find_template(slug: str) -> dict[str, Any]:
    templates = await list_templates()
    for t in templates:
        if t["slug"] == slug:
            return t
    raise ValidationError(
        "sites.unknown_template",
        f"There is no template {slug!r}. Pick one of: {', '.join(t['slug'] for t in templates)}.",
    )


async def find_recipe(recipe_id: str) -> dict[str, Any]:
    recipes = await list_recipes()
    for r in recipes:
        if r["id"] == recipe_id:
            return r
    raise ValidationError(
        "sites.unknown_recipe",
        f"There is no recipe {recipe_id!r}. Pick one of: {', '.join(r['id'] for r in recipes)}.",
    )


# ---------------------------------------------------------------------------
# Plan gate
# ---------------------------------------------------------------------------


def recipe_plan_allowed(
    min_plan: str | None, *, plan_tier: str | None, subscription_status: str | None
) -> bool:
    """Does a site on ``plan_tier`` cover a recipe that needs ``min_plan``?

    ``free`` always. ``site`` asks ``entitlements.site_paid_backends_entitled``, the
    predicate the binding provisioner gates R2 on (a paid tier AND an active
    subscription). ``staff`` additionally needs the resolved tier to be a staff rung.
    Anything unknown fails closed."""
    if min_plan in (None, "free"):
        return True
    from pocketpaw_ee.cloud.billing import site_plans
    from pocketpaw_ee.cloud.entitlements import service as entitlements

    if not entitlements.site_paid_backends_entitled(
        plan_tier=plan_tier, subscription_status=subscription_status
    ):
        return False
    if min_plan == "site":
        return True
    if min_plan == "staff":
        tier = site_plans.site_scoped_tier(plan_tier)
        return tier is not None and tier.key.startswith("staff")
    return False


# ---------------------------------------------------------------------------
# Paths and caps
# ---------------------------------------------------------------------------


def _bad_path(raw: Any, why: str) -> ValidationError:
    return ValidationError("sites.project_bad_path", f"The path {raw!r} is not allowed: {why}.")


def normalize_path(raw: Any) -> str:
    """A tool path as the source-map key it names. Raises ``ValidationError`` for an
    absolute path, a drive letter, ``..``, a backslash, NUL, or a reserved path."""
    if not isinstance(raw, str) or not raw.strip():
        raise _bad_path(raw, "it must be a non-empty string")
    if len(raw) > MAX_PATH_CHARS:
        raise _bad_path(raw[:40] + "...", f"it is longer than {MAX_PATH_CHARS} characters")
    if "\x00" in raw:
        raise _bad_path(raw, "it contains a NUL byte")
    if "\\" in raw:
        raise _bad_path(raw, "use forward slashes")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise _bad_path(raw, "it must be relative to the project root")
    rel = safe_rel(raw)
    if rel is None or rel == ".":
        raise _bad_path(raw, "it must stay inside the project")
    segments = rel.split("/")
    if any(seg in _RESERVED_SEGMENTS for seg in segments):
        raise _bad_path(raw, "node_modules and .git are never part of the source")
    if segments[0] == ".paw":
        raise _bad_path(raw, ".paw/ is where the build stages its output")
    if rel == project_build.PAW_BUILD_FILENAME:
        raise _bad_path(raw, "paw-build.json is written by the build")
    name = segments[-1]
    if name in _SECRET_FILES or (
        (name.startswith(".env.") or name.startswith(".dev.vars."))
        and not name.endswith(".example")
    ):
        raise _bad_path(
            raw,
            "secret values never go in the source; request them with request_site_secret "
            "and write names only to .dev.vars.example",
        )
    return rel


def _text_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def check_write_sizes(writes: Mapping[str, str]) -> None:
    if len(writes) > MAX_PATHS_PER_CALL:
        raise ValidationError(
            "sites.project_too_many_files",
            f"One call may write at most {MAX_PATHS_PER_CALL} files; split it.",
        )
    total = 0
    for path, contents in writes.items():
        size = _text_bytes(contents)
        if size > MAX_WRITE_FILE_BYTES:
            raise ValidationError(
                "sites.project_file_too_large",
                f"{path} is {size} bytes; one file may be at most {MAX_WRITE_FILE_BYTES}.",
            )
        total += size
    if total > MAX_WRITE_CALL_BYTES:
        raise ValidationError(
            "sites.project_call_too_large",
            f"This call writes {total} bytes; one call may write at most "
            f"{MAX_WRITE_CALL_BYTES}. Split it.",
        )


def check_project_totals(source: Mapping[str, Any]) -> None:
    """The whole map stays under the build's own source ceilings."""
    files = [v for v in source.values() if isinstance(v, str)]
    total = sum(_text_bytes(v) for v in files)
    if len(files) > project_build.MAX_SOURCE_FILES or total > project_build.MAX_SOURCE_BYTES:
        raise ValidationError(
            "sites.project_too_large",
            f"The project would be over the build limits ({project_build.MAX_SOURCE_FILES} "
            f"files, {project_build.MAX_SOURCE_BYTES // (1024 * 1024)} MiB of source).",
        )


def dependencies_changed(old: str | None, new: str | None) -> bool:
    """True when two package.json texts differ in what an install resolves."""

    def deps(text: str | None) -> Any:
        try:
            data = json.loads(text or "")
        except ValueError:
            return text
        if not isinstance(data, dict):
            return text
        return {k: data.get(k) for k in _DEP_KEYS}

    return deps(old) != deps(new)


def stale_lockfiles(
    source: Mapping[str, Any], writes: Mapping[str, str], deletes: list[str] | None = None
) -> list[str]:
    """Lockfiles to drop because this change edits package.json's dependency lists
    and does not rewrite the lockfile itself."""
    if "package.json" not in writes:
        return []
    if not dependencies_changed(source.get("package.json"), writes["package.json"]):
        return []
    gone = set(deletes or [])
    return [lf for lf in LOCKFILES if lf in source and lf not in writes and lf not in gone]


# ---------------------------------------------------------------------------
# Template copy + recipe apply (temp dirs on this host; no installs, no author code)
# ---------------------------------------------------------------------------


def _read_tree(root: Path) -> dict[str, str]:
    """Every regular file under ``root`` as ``{relpath: text}``. Binary files are
    refused: the source map is text, and the site-asset rail is for the owner's
    uploads, not template internals."""
    files: dict[str, str] = {}
    binary: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(seg in _RESERVED_SEGMENTS for seg in rel.split("/")):
            continue
        try:
            files[rel] = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            binary.append(rel)
    if binary:
        raise ValidationError(
            "sites.template_binary_files",
            "This template ships binary files a project source map cannot hold: "
            f"{', '.join(binary[:10])}. Pick another template, or ask for it to be "
            "fixed in paw-sites.",
        )
    return files


async def copy_template(slug: str) -> dict[str, str]:
    """``template-copy <slug>`` into a temp dir, read back as a source map."""
    await find_template(slug)
    with tempfile.TemporaryDirectory(prefix="paw-template-") as tmp:
        out = Path(tmp) / "project"
        code, payload, err = await run_cli(
            ["template-copy", slug, "--out", str(out), "--json", *_catalogue_root("starters")]
        )
        if code != 0 or not isinstance(payload, dict) or not payload.get("ok"):
            detail = (payload or {}).get("error") if isinstance(payload, dict) else None
            raise ProjectCliError(
                str((payload or {}).get("code") or "template_copy_failed"),
                f"Copying the {slug} template failed: {detail or err or 'no output'}",
            )
        source = _read_tree(out)
    project_build.project_files(source)  # package.json present, under the ceilings
    return source


def _materialize(source: Mapping[str, Any], root: Path) -> None:
    for rel, data in project_build.project_files(source).items():
        target = root.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


async def apply_recipe(
    source: Mapping[str, Any],
    recipe_id: str,
    *,
    template: str | None = None,
    dry_run: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Run ``apply-recipe`` on the source map in a temp dir.

    Returns ``(result, writes)``: the CLI's ``ApplyRecipeResult`` and, when it
    applied cleanly for real, the text of every file it wrote. A conflict, an error
    or a dry run returns no writes; the CLI itself writes nothing on a conflict."""
    with tempfile.TemporaryDirectory(prefix="paw-recipe-") as tmp:
        root = Path(tmp) / "project"
        _materialize(source, root)
        args = ["apply-recipe", recipe_id, "--template-dir", str(root), "--json"]
        if template:
            args += ["--template", template]
        if dry_run:
            args.append("--dry-run")
        _code, payload, err = await run_cli([*args, *_catalogue_root("recipes")])
        if not isinstance(payload, dict) or "ok" not in payload:
            raise ProjectCliError(
                "apply_recipe_failed", f"apply-recipe gave no result: {err or 'no output'}"
            )
        writes: dict[str, str] = {}
        if payload.get("ok") and not dry_run and not payload.get("alreadyApplied"):
            for raw in payload.get("written") or []:
                rel = safe_rel(raw)
                if rel is None or rel == ".":
                    raise ProjectCliError("apply_recipe_failed", f"apply-recipe wrote {raw!r}")
                path = root.joinpath(*rel.split("/"))
                try:
                    writes[rel] = path.read_bytes().decode("utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise ProjectCliError(
                        "apply_recipe_failed", f"apply-recipe wrote {rel} but it is unreadable"
                    ) from exc
    return payload, writes


# ---------------------------------------------------------------------------
# File operations (the agent's file tools and the HTTP files routes)
# ---------------------------------------------------------------------------


async def load_project(*, workspace_id: str, user_id: str, pocket_id: str) -> dict[str, Any]:
    """The project pocket (404 cross-workspace / no access, 422 another engine)."""
    from pocketpaw_ee.sites import service as sites_service

    return await sites_service.project_pocket(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
    )


def source_of(pocket: Mapping[str, Any]) -> dict[str, Any]:
    source = pocket.get("source")
    return dict(source) if isinstance(source, dict) else {}


async def build_verification(*, workspace_id: str, user_id: str, pocket_id: str) -> dict[str, Any]:
    """Queue the draft build of the pocket's current source and say so: ``pending``
    with the job id, ``passed`` when that exact source already built, ``failed`` when
    the tree cannot build at all (no package.json), ``unverified`` when the queue is
    down. Never raises."""
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites import service as sites_service

    try:
        art = await sites_service.queue_project_build(
            workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
        )
    except CloudError as exc:
        return {"status": "failed", "build": "failed", "reason": exc.code, "message": exc.message}
    except Exception:  # noqa: BLE001 — a build that could not queue is reported, not raised
        logger.warning("sites.project: could not queue a build for %s", pocket_id, exc_info=True)
        return {"status": "unverified", "build": "unverified", "reason": "queue_unavailable"}
    job_id = art.get("build_job_id")
    if art.get("build_status") == "none" and art.get("preview_mode"):
        return {
            "status": "passed",
            "build": "passed",
            "job_id": job_id,
            "preview_mode": art.get("preview_mode"),
        }
    return {
        "status": "pending",
        "build": "pending",
        "job_id": job_id,
        "layers": [{"name": "build", "status": "pending"}],
    }


async def save_changes(
    *,
    user_id: str,
    pocket_id: str,
    source: Mapping[str, Any],
    writes: Mapping[str, str] | None = None,
    deletes: list[str] | None = None,
    add_recipe: str | None = None,
    label: str | None = None,
) -> list[str]:
    """Size-check and persist one change in one save. Returns the lockfiles dropped
    because the change edits package.json's dependency lists."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    writes = dict(writes or {})
    deletes = list(deletes or [])
    check_write_sizes(writes)
    dropped = stale_lockfiles(source, writes, deletes)
    gone = set(deletes) | set(dropped)
    merged = {k: v for k, v in source.items() if k not in gone}
    merged.update(writes)
    check_project_totals(merged)
    await pockets_service.set_project_source(
        pocket_id,
        user_id,
        writes=writes,
        deletes=[*deletes, *dropped],
        add_recipe=add_recipe,
        label=label,
    )
    return dropped


async def list_files(
    *, workspace_id: str, user_id: str, pocket_id: str, prefix: str | None = None
) -> dict[str, Any]:
    """``{pocket_id, files: [{path, size}], file_count}``, sorted by path; ``size`` is
    UTF-8 bytes. ``prefix`` keeps only paths that start with it."""
    pocket = await load_project(workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id)
    want = (prefix or "").strip()
    while want.startswith("./"):
        want = want[2:]
    files = sorted(
        (
            {"path": path, "size": _text_bytes(text)}
            for path, text in source_of(pocket).items()
            if isinstance(text, str) and path.startswith(want)
        ),
        key=lambda f: f["path"],
    )
    return {"pocket_id": pocket_id, "files": files, "file_count": len(files)}


async def read_files(
    *, workspace_id: str, user_id: str, pocket_id: str, paths: Any
) -> list[dict[str, Any]]:
    """``[{path, size, content}]`` in request order, deduplicated, FULL contents (a
    caller that saves a file back must never get a truncated one). Any unknown path
    is a 404 naming it."""
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise ValidationError("sites.project_bad_path", "Name at least one file path to read.")
    if len(paths) > MAX_PATHS_PER_CALL:
        raise ValidationError(
            "sites.project_too_many_files", f"Read at most {MAX_PATHS_PER_CALL} files at once."
        )
    wanted: list[str] = []
    for raw in paths:
        rel = safe_rel(raw)
        if rel is None or rel == ".":
            raise _bad_path(raw, "it must be a relative path inside the project")
        if rel not in wanted:
            wanted.append(rel)
    pocket = await load_project(workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id)
    source = source_of(pocket)
    missing = [p for p in wanted if not isinstance(source.get(p), str)]
    if missing:
        raise NotFound("site_file", ", ".join(missing[:10]))
    return [{"path": p, "size": _text_bytes(source[p]), "content": source[p]} for p in wanted]


async def write_files(
    *, workspace_id: str, user_id: str, pocket_id: str, files: Any
) -> dict[str, Any]:
    """Create or overwrite files: ``{pocket_id, written, created, lockfile_removed,
    verification}``. Every path and size is checked before anything is saved; one bad
    path saves nothing."""
    if not isinstance(files, Mapping) or not files:
        raise ValidationError(
            "sites.project_no_files", "Send `files`: an object of {path: full contents}."
        )
    writes: dict[str, str] = {}
    for raw, contents in files.items():
        if not isinstance(contents, str):
            raise ValidationError(
                "sites.project_bad_file", f"The contents of {raw!r} must be a string."
            )
        writes[normalize_path(raw)] = contents
    pocket = await load_project(workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id)
    source = source_of(pocket)
    dropped = await save_changes(
        user_id=user_id,
        pocket_id=pocket_id,
        source=source,
        writes=writes,
        label=f"Edited {len(writes)} files" if len(writes) > 1 else None,
    )
    return {
        "pocket_id": pocket_id,
        "written": sorted(writes),
        "created": sorted(p for p in writes if p not in source),
        "lockfile_removed": dropped,
        "verification": await build_verification(
            workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
        ),
    }


async def patch_file(
    *, workspace_id: str, user_id: str, pocket_id: str, path: Any, edits: Any
) -> dict[str, Any]:
    """Apply ``[{old, new}]`` blocks to one existing file; each ``old`` must match the
    running text exactly once (``sites.service.apply_edits``), else nothing is saved.
    ``{pocket_id, path, lockfile_removed, verification}``."""
    from pocketpaw_ee.sites.service import apply_edits

    if not isinstance(edits, list) or not edits:
        raise ValidationError("site_edit.empty_edits", "Send `edits`: a list of {old, new}.")
    blocks = []
    for i, block in enumerate(edits):
        if (
            not isinstance(block, Mapping)
            or not isinstance(block.get("old"), str)
            or not isinstance(block.get("new"), str)
        ):
            raise ValidationError(
                "site_edit.malformed_block", f"Edit block {i} needs string `old` and `new`."
            )
        blocks.append({"old_string": block["old"], "new_string": block["new"]})
    rel = normalize_path(path)
    pocket = await load_project(workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id)
    source = source_of(pocket)
    if not isinstance(source.get(rel), str):
        raise NotFound("site_file", rel)
    new_text = apply_edits(source[rel], blocks)
    dropped = await save_changes(
        user_id=user_id, pocket_id=pocket_id, source=source, writes={rel: new_text}
    )
    return {
        "pocket_id": pocket_id,
        "path": rel,
        "lockfile_removed": dropped,
        "verification": await build_verification(
            workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
        ),
    }


async def delete_files(
    *, workspace_id: str, user_id: str, pocket_id: str, paths: Any
) -> dict[str, Any]:
    """Delete files: ``{pocket_id, deleted, verification}``. Every path must exist
    (else 404, nothing deleted); package.json cannot be deleted."""
    if not isinstance(paths, list) or not paths:
        raise ValidationError("sites.project_no_files", "Send `paths`: the files to delete.")
    if len(paths) > MAX_PATHS_PER_CALL:
        raise ValidationError(
            "sites.project_too_many_files", f"Delete at most {MAX_PATHS_PER_CALL} files at once."
        )
    rels = list(dict.fromkeys(normalize_path(p) for p in paths))
    if "package.json" in rels:
        raise ValidationError(
            "sites.project_needs_package_json",
            "A project site cannot build without its package.json; rewrite it instead.",
        )
    pocket = await load_project(workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id)
    source = source_of(pocket)
    missing = [p for p in rels if p not in source]
    if missing:
        raise NotFound("site_file", ", ".join(missing[:10]))
    await save_changes(
        user_id=user_id,
        pocket_id=pocket_id,
        source=source,
        deletes=rels,
        label=f"Deleted {len(rels)} files" if len(rels) > 1 else f"Deleted {rels[0]}",
    )
    return {
        "pocket_id": pocket_id,
        "deleted": rels,
        "verification": await build_verification(
            workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
        ),
    }


__all__ = [
    "build_verification",
    "delete_files",
    "list_files",
    "load_project",
    "patch_file",
    "read_files",
    "save_changes",
    "source_of",
    "write_files",
    "CLI_TIMEOUT_SEC",
    "LOCKFILES",
    "MAX_READ_CALL_BYTES",
    "MAX_READ_FILE_BYTES",
    "MAX_WRITE_CALL_BYTES",
    "MAX_WRITE_FILE_BYTES",
    "ProjectCliError",
    "apply_recipe",
    "check_project_totals",
    "check_write_sizes",
    "cli_argv",
    "copy_template",
    "dependencies_changed",
    "find_recipe",
    "find_template",
    "list_recipes",
    "list_templates",
    "normalize_path",
    "recipe_plan_allowed",
    "reset_caches",
    "run_cli",
    "stale_lockfiles",
]
