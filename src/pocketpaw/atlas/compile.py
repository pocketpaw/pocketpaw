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
#
# Updated: 2026-07-02 (feat/atlas-widgets, AT-6) — two new extracted kinds,
# both from OFFLINE bundled sources (the compiler never touches the network):
#   + ``widget`` entries — one per ripple canvas widget type, extracted from
#     the bundled design-language module (``pocketpaw.ripple._design``):
#     WIDGET_CATALOG (type + category), USE_THE_WIDGET_RULE (intent phrases
#     → keywords), WIDGET_SHAPES (key prop names for the high-traffic
#     widgets). NOT the CDN manifest — ``ripple/manifest.py`` is
#     network-only, so the compiled card is a discovery pointer and its
#     ``how`` sends the agent to ``get_widget_spec`` for the prop contract.
#   + ``skill`` entries — one per BUNDLED skill
#     (``pocketpaw/bundled_skills/_bundled/skills/<slug>/SKILL.md``),
#     summary/narrative from the frontmatter description. Workspace-
#     installed skills change per install and are deliberately NOT baked
#     into the artifact (their discovery stays with the system-prompt
#     skills block).

from __future__ import annotations

import json
import logging
import re
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


# ---------------------------------------------------------------------------
# Widget extraction (AT-6) — from the BUNDLED ripple design-language module.
#
# The real widget manifest (`ripple.manifest/v1`) is CDN-published and fetched
# at runtime by ``pocketpaw/ripple/manifest.py``; there is NO bundled copy in
# this repo, and the compiler must stay offline-deterministic. The bundled
# source of truth that IS in the repo is ``pocketpaw.ripple._design``:
#   * WIDGET_CATALOG      — every renderable widget type, grouped by category
#   * USE_THE_WIDGET_RULE — intent-phrase → widget mappings ("kanban / board /
#                           sprint board → kanban") = search keywords
#   * WIDGET_SHAPES       — canonical-shape docs for the high-traffic widgets,
#                           mined for key prop NAMES only (never the schema)
# The compiled card is therefore a discovery pointer: fuzzy intent → widget
# type. The prop CONTRACT stays with the live manifest via the existing
# ``get_widget_spec`` MCP tool, which every card's ``how`` points at.
# ---------------------------------------------------------------------------

# ``category  type, type, ...`` header line in the WIDGET_CATALOG block.
_CATALOG_CATEGORY_RE = re.compile(r"^([a-z][a-z-]*)\s{2,}(\S.*)$")

# Intent phrases must be plain widget vocabulary — drop wrapped-line debris
# (quotes, stray parens, arrows) rather than bake it into keywords.
_INTENT_PHRASE_RE = re.compile(r"^[a-z][a-z0-9 .'@›-]*$")

# ``if`` / ``each`` are spec grammar (control flow), not renderable widgets —
# the same distinction ``manifest._CONTROL_FLOW_TYPES`` draws at validation.
_NON_WIDGET_CATEGORIES = frozenset({"control"})


def _parse_widget_catalog() -> dict[str, list[str]]:
    """WIDGET_CATALOG text → ordered ``{widget_type: [category, ...]}``.

    The block is a fixed-format table: a category word, 2+ spaces, then a
    comma-separated type list that may wrap onto indented continuation
    lines. A type listed under several categories (``kbd`` under both
    display and inline) keeps every category, first one primary.
    """
    from pocketpaw.ripple._design import WIDGET_CATALOG

    by_type: dict[str, list[str]] = {}
    category = ""
    for line in WIDGET_CATALOG.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _CATALOG_CATEGORY_RE.match(line)
        if match:
            category, rest = match.group(1), match.group(2)
        elif line.startswith(" ") and category:
            rest = line.strip()
        else:
            continue
        if category in _NON_WIDGET_CATEGORIES:
            continue
        for token in rest.split(","):
            wtype = token.strip()
            if not wtype:
                continue
            cats = by_type.setdefault(wtype, [])
            if category not in cats:
                cats.append(category)
    return by_type


def _parse_intent_phrases(known_types: set[str]) -> dict[str, list[str]]:
    """USE_THE_WIDGET_RULE text → ``{widget_type: [intent phrase, ...]}``.

    Each mapping line is ``phrase / phrase / ...  → widget-type``. The
    right-hand side's first token must be a known catalog type (lines like
    ``→ `sortable:true` on the table`` are prop advice, not a widget) and
    phrases that fail the plain-vocabulary regex (wrapped-line fragments,
    quoted examples) are dropped.
    """
    from pocketpaw.ripple._design import USE_THE_WIDGET_RULE

    out: dict[str, list[str]] = {}
    for line in USE_THE_WIDGET_RULE.splitlines():
        if "→" not in line:
            continue
        lhs, rhs = line.split("→", 1)
        rhs_tokens = rhs.strip().split()
        target = rhs_tokens[0].strip("`") if rhs_tokens else ""
        if target not in known_types:
            continue
        lhs = re.sub(r"\([^)]*\)", " ", lhs)  # drop parenthetical asides
        for raw_phrase in lhs.split("/"):
            phrase = " ".join(raw_phrase.split()).lower()
            if not phrase or not _INTENT_PHRASE_RE.fullmatch(phrase):
                continue
            phrases = out.setdefault(target, [])
            if phrase not in phrases:
                phrases.append(phrase)
    return out


def _shape_prop_keys(shape_text: str, limit: int = 8) -> list[str]:
    """Key prop NAMES from a WIDGET_SHAPES doc's ``"props": {`` examples.

    Scans every example's props object and collects depth-1 keys with a
    tiny brace/string state machine (string contents like ``"{state.x}"``
    never confuse the depth count). Names only, first-seen order, capped —
    the full schema stays with ``get_widget_spec``.
    """
    keys: list[str] = []
    marker = '"props": {'
    cursor = 0
    while len(keys) < limit:
        start = shape_text.find(marker, cursor)
        if start < 0:
            break
        i = start + len(marker)
        depth = 1
        while i < len(shape_text) and depth > 0:
            ch = shape_text[i]
            if ch == '"':
                end = shape_text.find('"', i + 1)
                if end < 0:
                    break
                after = shape_text[end + 1 :].lstrip(" \t")
                if depth == 1 and after.startswith(":"):
                    key = shape_text[i + 1 : end]
                    if key and key not in keys:
                        keys.append(key)
                i = end + 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        cursor = i
    return keys[:limit]


def _widget_entries() -> list[AtlasEntry]:
    """Extract one ``kind:"widget"`` atlas entry per ripple catalog type.

    The card answers "which widget matches this intent" (fuzzy discovery);
    ``how`` and the narrative both route the agent to ``get_widget_spec``
    for the authoritative prop schema, mirroring the WIDGET SPEC TOOL RULE.
    """
    from pocketpaw.ripple._design import WIDGET_SHAPES

    catalog = _parse_widget_catalog()
    intent_phrases = _parse_intent_phrases(set(catalog))

    entries: list[AtlasEntry] = []
    for wtype, categories in catalog.items():
        phrases = intent_phrases.get(wtype, [])
        if phrases:
            summary = (
                f"Ripple '{wtype}' canvas widget ({categories[0]}) — "
                f"the catalog widget for {phrases[0]}."
            )
        else:
            summary = (
                f"Ripple '{wtype}' canvas widget in the {categories[0]} "
                "category of the closed catalog."
            )

        narrative_bits = [
            f"The '{wtype}' widget is part of ripple's closed canvas catalog "
            f"(category: {', '.join(categories)}); only catalog types render — "
            "an invented type shows the user a red 'Unknown widget type' box."
        ]
        if phrases:
            narrative_bits.append("Reach for it when the brief says: " + "; ".join(phrases) + ".")
        key_props = _shape_prop_keys(WIDGET_SHAPES.get(wtype, ""))
        if key_props:
            narrative_bits.append("Key props: " + ", ".join(key_props) + ".")
        narrative_bits.append(
            "This card is a discovery pointer, not the prop contract — call "
            f'get_widget_spec(["{wtype}"]) for the full prop schema before emitting the node.'
        )

        keywords: list[str] = []
        for word in [wtype, *categories, *phrases]:
            if word and word not in keywords:
                keywords.append(word)

        entries.append(
            AtlasEntry(
                id=f"widget:{wtype}",
                kind="widget",
                name=wtype,
                summary=summary,
                narrative=" ".join(narrative_bits),
                how=(
                    f'call get_widget_spec(["{wtype}"]) for the full prop schema, '
                    f'then emit a {{"type": "{wtype}"}} node in a rippleSpec'
                ),
                requires=["primitive:ripple"],
                keywords=keywords,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Skill extraction (AT-6) — BUNDLED skills only.
#
# At compile time only the skills shipped inside the package
# (``pocketpaw/bundled_skills/_bundled/skills/``) are stable across installs.
# Workspace-installed skills (~/.agents/skills, ~/.claude/skills,
# ~/.pocketpaw/skills — the SkillLoader SKILL_PATHS) vary per machine and are
# deliberately NOT baked into the artifact; their discovery stays with the
# system-prompt skills block.
# ---------------------------------------------------------------------------


def _skill_entries() -> list[AtlasEntry]:
    """Extract one ``kind:"skill"`` atlas entry per bundled skill.

    Frontmatter is parsed with the runtime's own ``parse_skill_md`` so the
    compiled card can never disagree with what the loader would serve.
    Summary = first sentence of the description; narrative = the full
    (whitespace-normalized) description plus argument hints, which is where
    the capability vocabulary ("rehearse the renewal", "publish a site")
    lives for intent search.
    """
    from pocketpaw.bundled_skills.installer import _SKILLS_DIR
    from pocketpaw.skills.loader import parse_skill_md

    entries: list[AtlasEntry] = []
    for skill_dir in sorted(p for p in _SKILLS_DIR.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        skill = parse_skill_md(skill_md)
        if skill is None:
            logger.warning("atlas compile: skipping unparseable bundled skill %s", skill_md)
            continue

        slug = skill_dir.name
        description = " ".join(skill.description.split())
        summary = (
            description.split(". ", 1)[0].rstrip(".") + "."
            if description
            else f"Bundled '{skill.name}' skill."
        )
        if len(summary) > 200:
            summary = summary[:199].rsplit(" ", 1)[0] + "…"

        narrative = description or f"The bundled '{skill.name}' skill."
        if skill.argument_hint:
            narrative += f" Arguments: {skill.argument_hint}."

        keywords: list[str] = []
        for word in [slug, skill.name.lower()]:
            if word and word not in keywords:
                keywords.append(word)

        entries.append(
            AtlasEntry(
                id=f"skill:{slug}",
                kind="skill",
                name=skill.name,
                summary=summary,
                narrative=narrative,
                how=(
                    f"a user invokes it as /{skill.name} in chat; the agent loads it "
                    "via the Skill tool (bundled Claude Code plugin). Workspace-installed "
                    "skills are discovered via the system-prompt skills block, not atlas."
                ),
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
    entries.extend(_widget_entries())
    entries.extend(_skill_entries())
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
