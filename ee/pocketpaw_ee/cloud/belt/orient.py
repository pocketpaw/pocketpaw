# ee/pocketpaw_ee/cloud/belt/orient.py — the factory's architecture context.
#
# The craft factory treats the repo's own architecture model as the source of
# truth, so its agents extend what exists instead of rebuilding it. Two readers:
#
#   ``orient_block``  the develop station's ORIENT step. Resolves a loom world
#       model for the bound repo (``<loom dir>/worldmodel-<repo dir name>.json``;
#       the loom dir is ``POCKETPAW_FACTORY_LOOM_DIR``, else the nearest ancestor
#       of the repo holding a ``.loom/`` dir), runs ``loom orient -json`` through
#       the station's ONE runner (argv list, flags before ``--`` so a task that
#       starts with ``-`` is never read as a flag, 60s timeout) and renders the
#       brief as a capped text block. No world model, or loom failing, falls back
#       to the repo's C4 list; with neither the block is empty. It never raises:
#       the returned note names what was used, for the run summary.
#   ``c4_lines``  the bound repo's ``docs/c4/model.json`` as one line per
#       container/component (name + first sentence), capped. The foreman gets
#       these in its prompt; ORIENT uses them as its fallback.
#
# One pure mapper: ``block_component`` turns a Pulley block manifest into c4-gen's
# ``Component`` (id, name, description, technology) plus its ``deps`` as sync
# relationships. It writes nothing and has no caller yet: it is for blueprint
# drafting (BF-14) and the line app's docs/c4/model.json writer (BF-15). C4 has no
# slot for routes, tables or events, so they ride the description after the
# block's own first sentence (the part ``c4_lines`` shows).
#
# Both blocks are owner-authored repo data (C4, symbols, shared-soul rules), so
# they ride the prompt unfenced, after the task and charter.

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

ORIENT_TIMEOUT = 60.0
_BLOCK_CHARS = 4000
_C4_CHARS = 3000
_TASK_CHARS = 2000
_REUSE_RULE = (
    "Reuse before you add: extend what is listed here; do not create a second copy "
    "of anything listed."
)


def _first_sentence(text: str, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    cut = text.find(". ")
    text = text[: cut + 1] if 0 < cut else text
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _capped(lines: list[str], limit: int) -> list[str]:
    kept: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > limit:
            kept.append(f"(… {len(lines) - i} more not shown)")
            break
        kept.append(line)
        used += len(line) + 1
    return kept


def c4_lines(repo: str | Path) -> list[str]:
    """The repo's own C4 system as ``- <container> / <component>: <what it is>``
    lines (``[]`` when there is no readable model). Only the system named by the
    model's ``scope`` (else the first with containers): the others are external
    systems the repo talks to, not things it owns."""
    try:
        data = json.loads((Path(repo) / "docs" / "c4" / "model.json").read_text())
        systems = data["model"]["systems"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    owned = [s for s in systems if s.get("id") == data.get("scope") and s.get("containers")]
    owned = owned or [s for s in systems if s.get("containers")]
    if not owned:
        return []
    lines: list[str] = []
    for container in owned[0]["containers"]:
        name = container.get("name") or "?"
        components = container.get("components") or []
        if not components:
            lines.append(f"- {name}: {_first_sentence(container.get('description'))}")
        for comp in components:
            lines.append(
                f"- {name} / {comp.get('name') or '?'}: {_first_sentence(comp.get('description'))}"
            )
    return _capped(lines, _C4_CHARS)


def block_component(manifest: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, str]]]:
    """A Pulley block manifest as ``(component, relationships)``: the c4-gen
    ``Component`` fields, and one sync ``block -> dependency`` relationship per
    entry in ``deps``. Pure: the caller decides where they go (blueprint
    drafting, a line app's model.json). Pulley declares a table PREFIX, not
    tables, so tables render as ``<prefix>*``."""
    name = str(manifest["name"])
    events = manifest.get("events") or {}
    prefix = manifest.get("tablePrefix")
    facts = [
        ("Provides", manifest.get("provides") or []),
        ("Routes", [r["path"] for r in manifest.get("routes") or []]),
        ("Endpoints", [e["path"] for e in manifest.get("endpoints") or []]),
        ("Tables", [f"{prefix}*"] if prefix else []),
        ("Emits", events.get("emits") or []),
        ("Consumes", events.get("consumes") or []),
    ]
    first = str(manifest.get("description") or name).strip()
    if not first.endswith((".", "!", "?")):
        first += "."
    rest = [f"{label}: {', '.join(values)}." for label, values in facts if values]
    component = {
        "id": name,
        "name": name,
        "description": " ".join([first, *rest]),
        "technology": f"Pulley {manifest.get('kind') or 'block'} block {manifest.get('version')}",
    }
    relationships = [
        {"source": name, "target": dep, "description": f"depends on {dep} {rng}", "style": "sync"}
        for dep, rng in sorted((manifest.get("deps") or {}).items())
    ]
    return component, relationships


def _loom_dir(repo: Path) -> Path | None:
    configured = (os.environ.get("POCKETPAW_FACTORY_LOOM_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    for parent in repo.parents:
        if (parent / ".loom").is_dir():
            return parent / ".loom"
    return None


def world_model_for(repo: Path) -> Path | None:
    """``<loom dir>/worldmodel-<repo dir name, lowercased>.json`` when it exists."""
    loom_dir = _loom_dir(repo)
    if loom_dir is None:
        return None
    candidate = loom_dir / f"worldmodel-{repo.name.lower()}.json"
    return candidate if candidate.is_file() else None


def loom_binary() -> str:
    """``POCKETPAW_FACTORY_LOOM_BIN``, else ``loom`` on PATH, else ``~/go/bin/loom``."""
    return (
        os.environ.get("POCKETPAW_FACTORY_LOOM_BIN")
        or shutil.which("loom")
        or str(Path.home() / "go" / "bin" / "loom")
    )


def _render_brief(brief: dict[str, Any], model_name: str) -> str:
    components: list[str] = []
    for crumb in brief.get("position") or []:
        for part in str(crumb).split(" > ")[2:]:
            if part not in components:
                components.append(part)

    def by_path(entities: list[dict[str, Any]]) -> list[str]:
        """Symbols grouped per file; a path-less entity (a C4 component in a
        model with no symbol extractor) on its own line with its description."""
        grouped: dict[str, list[str]] = {}
        loose: list[str] = []
        for e in entities or []:
            label = e.get("symbol") or e.get("name") or "?"
            attrs = e.get("attrs") or {}
            if not e.get("path"):
                what = _first_sentence(attrs.get("description"))
                loose.append(f"- {e.get('kind') or 'entity'} {label}: {what}")
                continue
            kind = attrs.get("kind")
            grouped.setdefault(e["path"], []).append(f"{label} ({kind})" if kind else label)
        return [f"- {path}: {', '.join(names)}" for path, names in grouped.items()] + loose

    lines = [f"EXISTING ARCHITECTURE (source of truth: loom world model {model_name})"]
    if components:
        lines.append("Components this task touches: " + "; ".join(components))
    if brief.get("scope"):
        lines.append("Code that already exists for this task:")
        lines += by_path(brief["scope"])
    if brief.get("blast_radius"):
        lines.append("Also affected by a change here (blast radius):")
        lines += by_path(brief["blast_radius"])
    if brief.get("entrypoints"):
        lines.append("Entrypoints:")
        lines += by_path(brief["entrypoints"])
    rules = brief.get("rules") or []
    if rules:
        lines.append("Rules:")
        for r in rules:
            what = _first_sentence(r.get("description"), 240)
            lines.append(f"- [{r.get('kind')}] {r.get('from') or ''}: {what}")
    body = "\n".join(_capped(lines, _BLOCK_CHARS - len(_REUSE_RULE) - 1))
    return f"{body}\n{_REUSE_RULE}"


def _c4_block(repo: Path) -> str:
    lines = c4_lines(repo)
    if not lines:
        return ""
    head = "EXISTING ARCHITECTURE (source of truth: the repo's C4 model docs/c4/model.json)"
    return "\n".join([head, *lines, _REUSE_RULE])


async def orient_block(run: Any, repo: Path, task: str, *, cwd: Path) -> tuple[str, str]:
    """``(block, note)`` for one develop run. ``run`` is the station's runner.
    Never raises: a missing loom, model, or C4 file degrades to a note."""
    note = ""
    model = world_model_for(repo)
    if model is not None:
        argv = [loom_binary(), "orient", "-model", str(model), "-json", "--", task[:_TASK_CHARS]]
        try:
            code, out, err = await run(argv, cwd=cwd, timeout=ORIENT_TIMEOUT)
            brief = json.loads(out) if code == 0 else None
        except (OSError, ValueError):
            code, brief = -1, None
        if isinstance(brief, dict):
            return _render_brief(brief, model.name), f"loom {model.name}"
        note = f"loom orient failed (exit {code}); "
    block = _c4_block(repo)
    if block:
        return block, note + "no world model; C4 docs/c4/model.json"
    return "", note + "no world model"


__all__ = [
    "ORIENT_TIMEOUT",
    "block_component",
    "c4_lines",
    "loom_binary",
    "orient_block",
    "world_model_for",
]
