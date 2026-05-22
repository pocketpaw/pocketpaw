# classifier.py — Pure, rule-based tier classifier for the pocket
#   execution router.
# Created: 2026-05-22 (Increment 3) — front-runs ``pocket_specialist__edit``.
#   Given a natural-language edit intent and the pocket's rippleSpec, it
#   decides the CHEAPEST capable execution tier:
#     * Tier 0 (declarative)  — fire a declared ``sources.X`` / ``actions.X``
#     * Tier 1 (deterministic) — apply exactly one granular op
#     * Tier 2 (specialist)    — escalate to the existing LLM flow
#
# This module is PURE: no I/O, no Beanie, no LLM, no logging side effects.
# It only inspects the two arguments and returns a ``Classification``.
# Purity is an import-linter contract — the router (router.py) is the
# layer allowed to touch executors and the specialist.
#
# FAIL-SAFE is the governing rule: the classifier returns a cheap-tier
# verdict ONLY when it is HIGH-confidence the cheap tier produces the
# EXACT change the user asked for. Any doubt — partial keyword match, a
# structural verb anywhere in the intent, a target that resolves to !=1
# entity, an unresolved ``{state.x}`` / ``{item.id}`` action param, a
# ``requires_instinct`` action, or a confidence below threshold —
# escalates to Tier 2. A wrong skip produces a broken pocket; a wrong
# escalate only costs an LLM call. The asymmetry drives every default.
"""Pure rule-based tier classifier for the pocket execution router."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

Tier = Literal[0, 1, 2]


@dataclass(frozen=True)
class Classification:
    """The classifier's verdict for one edit intent.

    ``tier`` — 0 declarative, 1 deterministic op, 2 specialist.
    ``target`` — what the cheap tier acts on: a source/action key (Tier
    0), a state path or node id (Tier 1), or ``None`` (Tier 2, nothing
    resolved).
    ``op`` — for Tier 0, ``run_source`` / ``run_action``; for Tier 1, the
    granular op name (``set_state`` / ``set_node_prop``); ``None`` for
    Tier 2.
    ``confidence`` — the classifier's confidence in a cheap-tier verdict,
    in ``[0.0, 1.0]``. Tier 2 verdicts carry the confidence of *not*
    being a cheap tier, which is always 1.0 (escalation is never wrong).
    ``reasoning`` — a short human-readable explanation, surfaced in the
    audit entry and the ``pocket_execution`` frame.
    ``op_args`` — for a Tier 0/1 verdict, the resolved arguments the
    router hands to the executor / op-apply path. Empty for Tier 2.
    """

    tier: Tier
    target: str | None
    confidence: float
    reasoning: str
    op: str | None = None
    op_args: dict[str, Any] = field(default_factory=dict)

    @property
    def is_escalation(self) -> bool:
        """True when the verdict routes to the specialist (Tier 2)."""
        return self.tier == 2


def _escalate(reasoning: str) -> Classification:
    """Build a Tier-2 (specialist) verdict. The fail-safe default."""
    return Classification(tier=2, target=None, confidence=1.0, reasoning=reasoning, op=None)


# ---------------------------------------------------------------------------
# Keyword tables
# ---------------------------------------------------------------------------
#
# These are deliberately conservative. A verb only counts when it appears
# as a whole word (``\b`` anchored) — "remarketing" must not match
# "remove". A *partial* signal (a verb that hints at a tier but whose
# object the classifier cannot pin to exactly one spec entity) escalates.

# Structural verbs — anything that changes the COMPONENT TREE shape.
# Their mere presence forces Tier 2: the specialist owns structure.
_STRUCTURAL_VERBS: frozenset[str] = frozenset(
    {
        "add",
        "create",
        "insert",
        "build",
        "redesign",
        "rebuild",
        "restructure",
        "reorganize",
        "reorganise",
        "layout",
        "move",
        "rearrange",
        "delete",
        "drop",
        "split",
        "merge",
        "convert",
        "turn",
        "wrap",
        "group",
        "duplicate",
        "clone",
        "swap",
        "redo",
    }
)

# Creative / ambiguous verbs — open-ended intent the classifier cannot
# reduce to one op. Also Tier 2.
_CREATIVE_VERBS: frozenset[str] = frozenset(
    {
        "improve",
        "polish",
        "clean",
        "cleanup",
        "declutter",
        "simplify",
        "beautify",
        "modernize",
        "modernise",
        "fix",
        "tweak",
        "adjust",
        "enhance",
        "optimize",
        "optimise",
        "refresh",  # NB: "refresh the dashboard" is creative; "refresh <source>"
        # is Tier 0 — handled explicitly in ``_classify_tier0`` before this
        # table is consulted.
    }
)

# Tier-0 declarative verbs: a data REFRESH against a declared source.
# Deliberately NOT "update" — "update" is too generic ("update the chart
# title" is a prop edit, not a source refresh); it would shadow Tier 1.
_REFRESH_VERBS: frozenset[str] = frozenset({"refresh", "reload", "sync", "fetch", "pull"})

# Tier-0 declarative verbs: fire a declared write action.
_ACTION_VERBS: frozenset[str] = frozenset({"submit", "save", "send", "post"})

# Tier-1 deterministic-op verbs: a status flip on one state field.
# ``mark`` / ``toggle`` are direction-NEUTRAL — they need an explicit
# done/todo word to pin a direction. The others carry their own
# direction: ``check`` / ``complete`` / ``tick`` mean DONE,
# ``uncheck`` / ``untick`` mean NOT-DONE.
_MARK_VERBS: frozenset[str] = frozenset(
    {"mark", "check", "uncheck", "complete", "toggle", "tick", "untick"}
)
_VERB_IMPLIES_DONE: frozenset[str] = frozenset({"check", "complete", "tick"})
_VERB_IMPLIES_NOT_DONE: frozenset[str] = frozenset({"uncheck", "untick"})

# Tier-1 deterministic-op verbs: a single-prop / single-field rename.
_RENAME_VERBS: frozenset[str] = frozenset({"rename", "relabel", "retitle"})

# Tier-1 deterministic-op verbs: set a filter / single state value.
_SET_VERBS: frozenset[str] = frozenset({"set", "filter", "change"})

# An unresolved Ripple expression in an action's params — a ``{...}``
# template the classifier cannot evaluate without runtime state.
_RIPPLE_EXPR = re.compile(r"\{[^}]+\}")


def _words(intent: str) -> list[str]:
    """Lowercase whole-word tokens of the intent."""
    return re.findall(r"[a-z0-9_]+", intent.lower())


def _has_any(words: list[str], table: frozenset[str]) -> bool:
    return any(w in table for w in words)


# ---------------------------------------------------------------------------
# Spec inspection helpers (pure)
# ---------------------------------------------------------------------------


def _sources(ripple_spec: dict[str, Any]) -> dict[str, Any]:
    raw = (ripple_spec or {}).get("sources")
    return raw if isinstance(raw, dict) else {}


def _actions(ripple_spec: dict[str, Any]) -> dict[str, Any]:
    raw = (ripple_spec or {}).get("actions")
    return raw if isinstance(raw, dict) else {}


def _state(ripple_spec: dict[str, Any]) -> dict[str, Any]:
    raw = (ripple_spec or {}).get("state")
    return raw if isinstance(raw, dict) else {}


def _match_keys(words: list[str], keys: list[str]) -> list[str]:
    """Return the spec keys whose name appears in the intent.

    A key matches when the key itself (lowercased) is one of the intent
    words, OR when every underscore-split segment of the key is an intent
    word (so ``mark_done`` matches "mark done"). Conservative: a key like
    ``orders`` matches "refresh orders" but not "refresh order" — exact
    whole-word only.
    """
    wset = set(words)
    hits: list[str] = []
    for key in keys:
        low = key.lower()
        if low in wset:
            hits.append(key)
            continue
        segments = [s for s in re.split(r"[_\-]", low) if s]
        if segments and all(seg in wset for seg in segments):
            hits.append(key)
    return hits


def _action_params_resolve(action_entry: Any) -> bool:
    """True when an action's declared ``params`` carry NO unresolved
    Ripple expression.

    A ``{state.x}`` / ``{item.id}`` in a param value means the action
    cannot fire without runtime context the classifier does not have —
    that escalates. An action with static params (or none) resolves.
    """
    if not isinstance(action_entry, dict):
        return False
    params = action_entry.get("params")
    if params is None:
        return True
    if not isinstance(params, dict):
        return False

    def _scan(value: Any) -> bool:
        if isinstance(value, str):
            return _RIPPLE_EXPR.search(value) is None
        if isinstance(value, dict):
            return all(_scan(v) for v in value.values())
        if isinstance(value, list):
            return all(_scan(v) for v in value)
        return True

    return _scan(params)


def _action_requires_instinct(action_entry: Any) -> bool:
    """True when the raw action carries a truthy ``requires_instinct``.

    Reads the same ``requires_instinct`` flag the write executor parks on
    (see ``ActionBinding.requires_instinct``) — such an action must NOT be
    auto-fired by the router; it escalates so the specialist flow (and its
    human-in-the-loop affordances) handles it.
    """
    return isinstance(action_entry, dict) and bool(action_entry.get("requires_instinct"))


# ---------------------------------------------------------------------------
# Tier 0 — declarative
# ---------------------------------------------------------------------------


def _classify_tier0(words: list[str], ripple_spec: dict[str, Any]) -> Classification | None:
    """Try to classify as Tier 0 (declarative). Returns ``None`` when the
    intent is not a clean declarative match — the caller falls through."""
    sources = _sources(ripple_spec)
    actions = _actions(ripple_spec)

    # ---- refresh / reload a declared source ----
    if _has_any(words, _REFRESH_VERBS) and sources:
        hits = _match_keys(words, list(sources.keys()))
        if len(hits) == 1:
            key = hits[0]
            return Classification(
                tier=0,
                target=key,
                confidence=0.97,
                reasoning=f"intent refreshes the declared source '{key}' 1:1",
                op="run_source",
                op_args={"source": key},
            )
        if len(hits) > 1:
            return _escalate(
                f"refresh intent matched {len(hits)} sources {sorted(hits)} — "
                "ambiguous, the specialist disambiguates"
            )
        # A refresh verb with no named source. "refresh the dashboard"
        # is creative, not declarative — fall through to escalation.
        return None

    # ---- submit / save -> a declared write action ----
    if _has_any(words, _ACTION_VERBS) and actions:
        hits = _match_keys(words, list(actions.keys()))
        if len(hits) == 1:
            key = hits[0]
            entry = actions[key]
            if _action_requires_instinct(entry):
                return _escalate(
                    f"action '{key}' is marked requires_instinct — escalating "
                    "so the gated flow handles approval"
                )
            if not _action_params_resolve(entry):
                return _escalate(
                    f"action '{key}' has unresolved {{state.x}}/{{item.id}} "
                    "params — cannot fire declaratively, escalating"
                )
            return Classification(
                tier=0,
                target=key,
                confidence=0.95,
                reasoning=f"intent fires the declared action '{key}' 1:1 with resolved params",
                op="run_action",
                op_args={"action": key},
            )
        if len(hits) > 1:
            return _escalate(
                f"action intent matched {len(hits)} actions {sorted(hits)} — ambiguous"
            )
        return None

    return None


# ---------------------------------------------------------------------------
# Tier 1 — deterministic single granular op
# ---------------------------------------------------------------------------

# Status keywords the classifier knows how to write into a task/item
# ``status`` field for a "mark X done" intent.
_DONE_WORDS: frozenset[str] = frozenset({"done", "complete", "completed", "finished", "closed"})
_NOT_DONE_WORDS: frozenset[str] = frozenset({"undone", "incomplete", "open", "todo", "pending"})

# A trailing integer in the intent — "mark task 3 done" -> 3.
_INDEX_RE = re.compile(r"\b(\d{1,4})\b")


def _classify_tier1(
    intent: str, words: list[str], ripple_spec: dict[str, Any]
) -> Classification | None:
    """Try to classify as Tier 1 (one deterministic granular op).

    Returns ``None`` when the intent is not an unambiguous single-op
    match. The router only acts on a returned verdict; ``None`` means
    fall through to escalation.
    """
    state = _state(ripple_spec)

    # ---- "mark task N done" -> set_state on a status field ----
    if _has_any(words, _MARK_VERBS):
        idx_match = _INDEX_RE.search(intent)
        wset = set(words)
        # Direction comes from an explicit done/todo word OR from a
        # self-directed verb (``check`` -> done, ``uncheck`` -> not-done).
        wants_done = bool(wset & _DONE_WORDS) or bool(wset & _VERB_IMPLIES_DONE)
        wants_not_done = bool(wset & _NOT_DONE_WORDS) or bool(wset & _VERB_IMPLIES_NOT_DONE)
        # Exactly one direction, exactly one numeric target, and a state
        # array the index can address — anything else escalates.
        if idx_match and (wants_done ^ wants_not_done):
            idx = int(idx_match.group(1))
            collection = _single_indexable_collection(state)
            if collection is None:
                return _escalate(
                    "mark-done intent but no single indexable task/item "
                    "collection in state — the specialist disambiguates"
                )
            name, length = collection
            # 1-based human counting -> 0-based index. Reject out of range.
            zero_idx = idx - 1
            if not (0 <= zero_idx < length):
                return _escalate(
                    f"mark-done index {idx} is out of range for state.{name} "
                    f"(len {length}) — escalating"
                )
            status_value = "done" if wants_done else "todo"
            path = f"{name}[{zero_idx}].status"
            return Classification(
                tier=1,
                target=path,
                confidence=0.95,
                reasoning=(
                    f"mark item {idx} -> set_state state.{path} = '{status_value}' "
                    "(single deterministic op)"
                ),
                op="set_state",
                op_args={"path": path, "value": status_value},
            )
        return _escalate(
            "mark/toggle intent is not an unambiguous single-item status "
            "flip — escalating to the specialist"
        )

    # ---- "set filter to X" -> set_state on a top-level scalar ----
    if "filter" in words and _has_any(words, _SET_VERBS | {"filter"}):
        if "filter" in state and not isinstance(state.get("filter"), (dict, list)):
            value = _trailing_value(intent)
            if value is not None:
                return Classification(
                    tier=1,
                    target="filter",
                    confidence=0.92,
                    reasoning=f"set the declared scalar state.filter = '{value}' (single op)",
                    op="set_state",
                    op_args={"path": "filter", "value": value},
                )
            return _escalate("set-filter intent has no extractable target value — escalating")
        return _escalate("set-filter intent but state has no scalar 'filter' field — escalating")

    # ---- "rename X to Y" -> set_node_prop, only when X resolves to one node ----
    if _has_any(words, _RENAME_VERBS):
        new_label = _rename_target(intent)
        node_hits = _nodes_matching_rename(intent, ripple_spec)
        if new_label is None:
            return _escalate("rename intent has no extractable new label — escalating")
        if len(node_hits) != 1:
            return _escalate(
                f"rename intent resolved to {len(node_hits)} candidate nodes — "
                "the specialist disambiguates which node to relabel"
            )
        node_id, prop = node_hits[0]
        return Classification(
            tier=1,
            target=node_id,
            confidence=0.91,
            reasoning=(
                f"rename -> set_node_prop {node_id}.{prop} = '{new_label}' "
                "(single deterministic op)"
            ),
            op="set_node_prop",
            op_args={"node_id": node_id, "prop": prop, "value": new_label},
        )

    return None


def _single_indexable_collection(state: dict[str, Any]) -> tuple[str, int] | None:
    """Return ``(name, length)`` when state has EXACTLY ONE list whose
    items are objects (a task/item collection an index can address);
    ``None`` otherwise.

    Multiple candidate lists is ambiguous — escalate. Zero is escalate.
    A list of scalars is not an addressable item collection.
    """
    candidates: list[tuple[str, int]] = []
    for name, value in state.items():
        if isinstance(value, list) and value and all(isinstance(it, dict) for it in value):
            candidates.append((name, len(value)))
    return candidates[0] if len(candidates) == 1 else None


def _trailing_value(intent: str) -> str | None:
    """Extract the value after a ``to`` / ``=`` / ``:`` in a set-style
    intent — "set the filter to overdue" -> "overdue"."""
    m = re.search(r"(?:\bto\b|=|:)\s*['\"]?([A-Za-z0-9_\- ]+?)['\"]?\s*$", intent.strip())
    if m:
        value = m.group(1).strip()
        return value or None
    return None


def _rename_target(intent: str) -> str | None:
    """Extract the new label from a rename intent.

    Handles ``rename X to "Y"`` and ``rename X to Y``. The quoted form
    matches the SAME quote char that opened (so an apostrophe inside a
    double-quoted label — ``"Today's Work"`` — is kept verbatim).
    Returns ``None`` when no ``to`` clause is present — without a target
    label the op cannot run.
    """
    m = re.search(r"\bto\b\s+([\"'])(.+?)\1", intent)
    if m:
        return m.group(2).strip() or None
    m = re.search(r"\bto\b\s+(.+?)\s*$", intent.strip())
    if m:
        return m.group(1).strip().strip("'\"") or None
    return None


def _iter_nodes(node: Any) -> list[dict[str, Any]]:
    """Flatten a rippleSpec UI tree to a list of node dicts."""
    out: list[dict[str, Any]] = []
    if not isinstance(node, dict):
        return out
    out.append(node)
    for child in node.get("children") or []:
        out.extend(_iter_nodes(child))
    return out


def _nodes_matching_rename(intent: str, ripple_spec: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(node_id, prop)`` pairs for nodes a rename intent targets.

    A node matches when one of its text-ish props (``text`` / ``title`` /
    ``label`` / ``heading``) holds a value whose words all appear in the
    intent BEFORE the ``to`` clause. The prop returned is the one that
    matched. This is intentionally strict — a rename only goes Tier 1
    when EXACTLY ONE node matches; the caller escalates otherwise.
    """
    before_to = re.split(r"\bto\b", intent, maxsplit=1)[0].lower()
    before_words = set(re.findall(r"[a-z0-9_]+", before_to))
    if not before_words:
        return []
    label_props = ("text", "title", "label", "heading")
    hits: list[tuple[str, str]] = []
    ui = (ripple_spec or {}).get("ui")
    for node in _iter_nodes(ui):
        node_id = node.get("id")
        props = node.get("props")
        if not isinstance(node_id, str) or not isinstance(props, dict):
            continue
        for prop in label_props:
            raw = props.get(prop)
            if not isinstance(raw, str) or not raw.strip():
                continue
            value_words = set(re.findall(r"[a-z0-9_]+", raw.lower()))
            # Every word of the current prop value is named in the intent.
            if value_words and value_words <= before_words:
                hits.append((node_id, prop))
                break
    return hits


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def classify(intent: str, ripple_spec: dict[str, Any]) -> Classification:
    """Classify an edit ``intent`` against a pocket's ``ripple_spec``.

    Pure — no I/O, no LLM. Returns a :class:`Classification` naming the
    cheapest capable tier. The decision order is fail-safe:

    1. An empty / too-short intent escalates (nothing to classify).
    2. A structural or creative verb ANYWHERE in the intent escalates —
       the specialist owns structure and open-ended change.
    3. Tier 0 — a clean 1:1 declarative match against a declared
       ``sources.X`` (refresh) or ``actions.X`` (submit/save).
    4. Tier 1 — a clean single-op match (``set_state`` / ``set_node_prop``).
    5. Anything left escalates to Tier 2.

    The classifier never *guesses*: a partial match, a target resolving
    to !=1 entity, an unresolved action param, or a ``requires_instinct``
    action all escalate. A wrong skip breaks the pocket.
    """
    if not intent or len(intent.strip()) < 3:
        return _escalate("intent is empty or too short to classify — escalating")

    words = _words(intent)

    # --- Tier 0 is checked FIRST so a declarative "refresh <source>"
    # wins over the "refresh" creative-verb escalation below. The Tier-0
    # path only matches a refresh verb when it pins to exactly one named
    # source; otherwise it returns ``None`` and the creative-verb guard
    # catches the bare "refresh the dashboard" case. ---
    tier0 = _classify_tier0(words, ripple_spec)
    if tier0 is not None:
        return tier0

    # --- Structural verbs: the specialist owns the component tree. ---
    if _has_any(words, _STRUCTURAL_VERBS):
        struck = sorted(set(words) & _STRUCTURAL_VERBS)
        return _escalate(
            f"structural verb(s) {struck} present — component-tree change, "
            "the specialist owns structure"
        )

    # --- Creative / ambiguous verbs: open-ended, no single op. ---
    if _has_any(words, _CREATIVE_VERBS):
        struck = sorted(set(words) & _CREATIVE_VERBS)
        return _escalate(
            f"creative / ambiguous verb(s) {struck} present — no single "
            "deterministic op expresses this, escalating"
        )

    # --- Tier 1: one deterministic granular op. ---
    tier1 = _classify_tier1(intent, words, ripple_spec)
    if tier1 is not None:
        return tier1

    # --- Fail-safe default. ---
    return _escalate(
        "intent did not match a high-confidence Tier-0 or Tier-1 rule — "
        "escalating to the specialist (fail-safe default)"
    )


__all__ = ["Classification", "Tier", "classify"]
