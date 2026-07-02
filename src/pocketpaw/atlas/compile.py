# atlas/compile.py — deterministic compiler for the atlas OS self-model
# (AT-4). Created: 2026-07-02 (feat/atlas-compiler).
#
# Turns the hand-authored seed into a COMPILED artifact:
#   authored entries (``atlas/authored/primitives.json`` + ``surfaces.json``)
#   + extracted ``connector`` entries (one per connector YAML in the repo's
#     connectors/ dir — summary, ACTIONS with one-liners + param names,
#     declared senses, search keywords)
#   + extracted ``sense`` entries (one per CORE_SENSES vocabulary entry,
#     cross-linking the connectors that declare it)
# sorted by id and serialized with sorted keys + fixed indent + trailing
# newline so the output is BYTE-DETERMINISTIC (same inputs → identical
# file). ``atlas/data/atlas.json`` is the checked-in compiled artifact
# (lockfile-style, ``"generated": true``); CI runs
# ``pocketpaw atlas build --check`` to keep it fresh.

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pocketpaw.atlas.model import AtlasEntry, AtlasModel
from pocketpaw.atlas.store import _DATA_PATH

if TYPE_CHECKING:
    from pocketpaw.connectors.yaml_engine import ConnectorDef

logger = logging.getLogger(__name__)

# Hand-authored source files, compiled in this order (before id-sorting).
_AUTHORED_DIR = Path(__file__).parent / "authored"
AUTHORED_FILES = (
    _AUTHORED_DIR / "primitives.json",
    _AUTHORED_DIR / "surfaces.json",
)

# Where connector YAML definitions live, relative to the repo root (the
# same default dir ConnectorRegistry scans). The compiler reads ONLY the
# repo dir — never ~/.pocketpaw/connectors — so the checked-in artifact
# reflects the repo, not one developer's machine.
DEFAULT_CONNECTORS_DIR = Path("connectors")

# Every connector entry points users at the integrations surface.
_INTEGRATIONS_ROUTE = "/settings/workspace/integrations"


def load_authored_entries() -> list[AtlasEntry]:
    """Load and validate the hand-authored entry files."""
    entries: list[AtlasEntry] = []
    for path in AUTHORED_FILES:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries.extend(AtlasModel.model_validate(raw).entries)
    return entries


def _load_connector_defs(connectors_dir: Path) -> list[ConnectorDef]:
    """Parse every connector YAML in ``connectors_dir``, sorted by name.

    Malformed YAMLs are skipped with a warning (mirrors the registry's
    scan behavior) so one bad file can't block the build.
    """
    from pocketpaw.connectors.yaml_engine import parse_connector_yaml

    defs = []
    for path in sorted(connectors_dir.glob("*.yaml")):
        try:
            defs.append(parse_connector_yaml(path))
        except Exception as exc:  # noqa: BLE001 — skip malformed, keep compiling
            logger.warning("atlas compile: skipping malformed connector YAML %s: %s", path, exc)
    return sorted(defs, key=lambda d: d.name)


def _connector_entry(defn: ConnectorDef) -> AtlasEntry:
    """Extract one ``kind:"connector"`` atlas entry from a parsed YAML def.

    The narrative is the load-bearing slice: it lists every ACTION (name +
    one-line description + param names) and the declared senses, so an
    agent can discover e.g. that Stripe supports ``list_invoices`` without
    guessing from priors.
    """
    action_names: list[str] = []
    action_bits: list[str] = []
    for action in defn.actions:
        name = str(action.get("name", "")).strip()
        if not name:
            continue
        action_names.append(name)
        desc = str(action.get("description", "")).strip().rstrip(".")
        params = list((action.get("params") or {}).keys())
        bit = f"{name} — {desc}" if desc else name
        if params:
            bit += f" (params: {', '.join(params)})"
        action_bits.append(bit)

    preview = ", ".join(action_names[:4])
    more = f", +{len(action_names) - 4} more" if len(action_names) > 4 else ""
    summary = (
        f"{defn.display_name} ({defn.type}) connector — "
        f"{len(action_names)} actions: {preview}{more}."
    )

    narrative = (
        f"The {defn.display_name} connector ({defn.type}) wires "
        f"{defn.display_name} data into the workspace. "
        f"ACTIONS: {'; '.join(action_bits)}."
    )
    if defn.senses:
        narrative += f" Declared senses: {', '.join(defn.senses)}."

    keywords: list[str] = []
    for word in [defn.name, defn.display_name.lower(), defn.type, *action_names] + [
        _sense_domain(s) for s in defn.senses
    ]:
        if word and word not in keywords:
            keywords.append(word)

    return AtlasEntry(
        id=f"connector:{defn.name}",
        kind="connector",
        name=defn.display_name,
        summary=summary,
        narrative=narrative,
        how=(
            f"list_connector_actions / connector_execute over the "
            f"'{defn.name}' connector once it is bound"
        ),
        surface=_INTEGRATIONS_ROUTE,
        requires=["primitive:connector"],
        keywords=keywords,
    )


def _sense_domain(sense_id: str) -> str:
    """'paw.payments.v1' → 'payments' (the searchable domain word)."""
    parts = sense_id.split(".")
    return parts[1] if len(parts) >= 2 else sense_id


def _sense_entries(defs: list[ConnectorDef]) -> list[AtlasEntry]:
    """Extract one ``kind:"sense"`` entry per CORE_SENSES vocabulary item.

    Each entry cross-links the connectors that declare the sense (via the
    static ``connectors_for_sense`` index) so an agent can go from a
    capability ("email") to the concrete connectors that fill it.
    """
    from pocketpaw.senses.vocabulary import CORE_SENSES, connectors_for_sense

    entries: list[AtlasEntry] = []
    for sense in CORE_SENSES:
        declaring = connectors_for_sense(sense.id, defs)  # already sorted
        narrative = (
            f"{sense.description} A Sense is a provider-agnostic capability "
            f"above connectors: templates and agents address the sense id "
            f"({sense.id}) and the resolver binds it to whichever declaring "
            f"connector the workspace enabled."
        )
        if declaring:
            narrative += f" Connectors declaring this sense: {', '.join(declaring)}."

        keywords: list[str] = []
        for word in [_sense_domain(sense.id), sense.display_name.lower(), *declaring]:
            if word and word not in keywords:
                keywords.append(word)

        entries.append(
            AtlasEntry(
                id=f"sense:{sense.id}",
                kind="sense",
                name=sense.display_name,
                summary=sense.description,
                narrative=narrative,
                how=(
                    "bind a declaring connector; connectors declare senses in "
                    "their YAML (senses: [...]), resolved via connectors_for_sense"
                ),
                surface=_INTEGRATIONS_ROUTE,
                requires=[f"connector:{name}" for name in declaring],
                keywords=keywords,
            )
        )
    return entries


def compile_atlas(connectors_dir: Path | None = None) -> AtlasModel:
    """Compile authored + extracted entries into one model, sorted by id."""
    connectors_dir = connectors_dir or DEFAULT_CONNECTORS_DIR
    defs = _load_connector_defs(connectors_dir)
    entries = load_authored_entries()
    entries.extend(_connector_entry(d) for d in defs)
    entries.extend(_sense_entries(defs))
    entries.sort(key=lambda e: e.id)
    return AtlasModel(generated=True, entries=entries)


def serialize_atlas(model: AtlasModel) -> bytes:
    """Byte-deterministic serialization: sorted keys, indent 2, trailing \\n."""
    payload = model.model_dump(by_alias=True)
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def compile_atlas_bytes(connectors_dir: Path | None = None) -> bytes:
    """Compile straight to the exact bytes the artifact file would hold."""
    return serialize_atlas(compile_atlas(connectors_dir))


def write_artifact(
    output_path: Path | None = None, connectors_dir: Path | None = None
) -> tuple[Path, AtlasModel]:
    """Compile and write the artifact (default: the packaged data file)."""
    path = output_path or _DATA_PATH
    model = compile_atlas(connectors_dir)
    path.write_bytes(serialize_atlas(model))
    return path, model


def check_artifact(
    artifact_path: Path | None = None, connectors_dir: Path | None = None
) -> tuple[bool, str]:
    """Compile to memory and compare against the checked-in artifact.

    Returns ``(fresh, diff_summary)`` — ``fresh`` is True when the bytes
    match exactly; ``diff_summary`` explains the drift (added / removed /
    changed entry ids, or a formatting-only note) for CI output.
    """
    path = artifact_path or _DATA_PATH
    expected = compile_atlas_bytes(connectors_dir)
    if not path.exists():
        return False, f"artifact missing: {path}"
    actual = path.read_bytes()
    if actual == expected:
        return True, ""

    lines = [f"artifact stale: {path}"]
    try:
        actual_entries = {e["id"]: e for e in json.loads(actual)["entries"]}
        expected_entries = {e["id"]: e for e in json.loads(expected)["entries"]}
        added = sorted(set(expected_entries) - set(actual_entries))
        removed = sorted(set(actual_entries) - set(expected_entries))
        changed = sorted(
            eid
            for eid in set(actual_entries) & set(expected_entries)
            if actual_entries[eid] != expected_entries[eid]
        )
        if added:
            lines.append(f"  entries to add ({len(added)}): {', '.join(added[:10])}")
        if removed:
            lines.append(f"  entries to remove ({len(removed)}): {', '.join(removed[:10])}")
        if changed:
            lines.append(f"  entries changed ({len(changed)}): {', '.join(changed[:10])}")
        if not (added or removed or changed):
            lines.append("  entry content identical — header/formatting drift only")
    except Exception:  # noqa: BLE001 — a corrupt artifact still reports stale
        lines.append("  (checked-in artifact is not parseable JSON)")
    lines.append("  fix: run `pocketpaw atlas build` from the repo root and commit the result")
    return False, "\n".join(lines)


__all__ = [
    "AUTHORED_FILES",
    "DEFAULT_CONNECTORS_DIR",
    "check_artifact",
    "compile_atlas",
    "compile_atlas_bytes",
    "load_authored_entries",
    "serialize_atlas",
    "write_artifact",
]
