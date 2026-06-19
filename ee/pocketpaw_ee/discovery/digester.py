# pocketpaw_ee/discovery/digester.py — the Digester interface + StructuredShapeDigester.
#
# Created: 2026-06-19 (SZD-3 / feat/szd-3-digester) — sovereign zero-setup
# discovery reverse-engineers a candidate ontology from sampled connector
# records. This module is the digester: structured records → an OntologyDraft.
#
# ``Digester`` is a Protocol: ``digest(records, connector_meta) -> OntologyDraft``.
# ``StructuredShapeDigester`` is the structured-record implementation:
#
#   * records arrive as ``{type_name: [record_dict, ...]}`` (one list per
#     source/type), the natural shape a connector sampler produces;
#   * object types come from the field shapes; property types from value types
#     (str / int / float / bool / datetime-ish), aggregated across the sample;
#   * the primary key is inferred per type — prefer a field named
#     ``id`` / ``_id`` / ``<type>_id`` / ``<type>id``, else the highest-cardinality
#     unique field — with confidence reflecting the signal strength;
#   * links are inferred from fields whose values match another type's keys
#     (a foreign-key shape).
#
# Pure logic: no DB, no network, no async. Depends only on stdlib + pydantic +
# the OSS PropertyDef / discovery models. Unit-testable in isolation.

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from pocketpaw.fabric.models import PropertyDef
from pocketpaw_ee.discovery.models import (
    DraftLink,
    DraftObject,
    DraftObjectType,
    OntologyDraft,
    _clamp,
)

# Field names that strongly signal a primary key, in priority order. ``{type}``
# is substituted with the (lowercased) type name at inference time so that, e.g.
# ``customer_id`` on the ``Customer`` type beats a generic high-cardinality field.
_KEY_NAME_PRIORITY = ("id", "_id", "{type}_id", "{type}id", "uid", "uuid", "key", "pk")

# ISO-8601-ish date / datetime detection (cheap, no parsing libs). Matches
# "2026-06-19", "2026-06-19T22:00:00", "2026-06-19 22:00:00Z", etc.
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"  # date
    r"([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$"  # optional time
)

# Common foreign-key suffixes used when matching a field on type A to the key of
# type B (e.g. ``customer_id`` -> ``Customer``).
_FK_SUFFIXES = ("_id", "id", "_ref", "_key", "_uid")


@runtime_checkable
class Digester(Protocol):
    """Reverse-engineers a candidate ontology from sampled connector records.

    Implementations take a record sample (shape is implementation-defined; the
    structured digester expects ``{type_name: [record, ...]}``) plus connector
    metadata, and return an :class:`OntologyDraft` that downstream code can turn
    into ``FabricMapping`` objects and a fabric-objects proposal.
    """

    def digest(
        self,
        records: Any,
        connector_meta: Mapping[str, Any] | None = None,
    ) -> OntologyDraft: ...


def _is_datetimeish(value: Any) -> bool:
    """True if a value looks like a date/datetime (native or ISO-ish string)."""
    if isinstance(value, (datetime, date)):
        return True
    if isinstance(value, str):
        return bool(_DATETIME_RE.match(value.strip()))
    return False


def _scalar_type(value: Any) -> str | None:
    """Map a single value to a PropertyDef type, or None if not a clean scalar.

    Returns one of: ``"date"``, ``"boolean"``, ``"number"``, ``"string"``.
    ``None`` for nulls (carry no type signal) and for nested/polymorphic shapes
    (dict / list) — those are recorded as ``"string"`` at the type level but do
    not vote on a clean scalar type.
    """
    if value is None:
        return None
    # bool is a subclass of int — check it first.
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (datetime, date)):
        return "date"
    if isinstance(value, str):
        return "date" if _is_datetimeish(value) else "string"
    # dict / list / other — nested or polymorphic; no clean scalar vote.
    return None


def _hashable(value: Any) -> Any:
    """Return a hashable surrogate for a value (for cardinality counting).

    Nested / unhashable values (dict, list) collapse to their repr so they can
    still be counted without crashing — they just won't be good key candidates.
    """
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)


class _FieldStats:
    """Accumulates per-field signal across a type's record sample."""

    __slots__ = ("type_votes", "present", "non_null", "values", "distinct")

    def __init__(self) -> None:
        self.type_votes: Counter[str] = Counter()
        self.present = 0  # records where the key is present (even if null)
        self.non_null = 0  # records with a non-null value
        # distinct non-null hashable values (capped implicitly by sample size)
        self.distinct: set[Any] = set()
        # the raw non-null values seen, for FK matching (capped surrogate)
        self.values: list[Any] = []

    def observe(self, value: Any) -> None:
        self.present += 1
        if value is None:
            return
        self.non_null += 1
        vt = _scalar_type(value)
        if vt is not None:
            self.type_votes[vt] += 1
        else:
            # nested / polymorphic — record as string so the property still types.
            self.type_votes["string"] += 1
        h = _hashable(value)
        self.distinct.add(h)
        self.values.append(h)

    @property
    def cardinality_ratio(self) -> float:
        """distinct / non_null — 1.0 means every value is unique."""
        if self.non_null == 0:
            return 0.0
        return len(self.distinct) / self.non_null

    def dominant_type(self) -> str:
        """The most-voted PropertyDef type; ``string`` when no votes."""
        if not self.type_votes:
            return "string"
        return self.type_votes.most_common(1)[0][0]

    @property
    def type_purity(self) -> float:
        """Fraction of non-null values agreeing with the dominant type."""
        total = sum(self.type_votes.values())
        if total == 0:
            return 0.0
        return self.type_votes.most_common(1)[0][1] / total


class StructuredShapeDigester:
    """Infers an OntologyDraft from structured records grouped by type.

    Input shape: ``{type_name: [record_dict, ...]}``. A flat ``list[dict]`` is
    also accepted and treated as a single anonymous type (named from
    ``connector_meta["default_type"]`` or ``"Record"``).
    """

    def __init__(self, default_type_name: str = "Record") -> None:
        self._default_type_name = default_type_name

    # ------------------------------------------------------------------ public
    def digest(
        self,
        records: Any,
        connector_meta: Mapping[str, Any] | None = None,
    ) -> OntologyDraft:
        meta = dict(connector_meta or {})
        grouped = self._normalize_input(records, meta)

        draft = OntologyDraft(meta={"digester": "structured_shape", **meta})
        if not grouped:
            draft.meta["degraded"] = "empty"
            return draft

        # Pass 1 — per-type field stats + property defs + key inference.
        type_stats: dict[str, dict[str, _FieldStats]] = {}
        for type_name, rows in grouped.items():
            clean_rows = [r for r in rows if isinstance(r, Mapping)]
            stats = self._collect_field_stats(clean_rows)
            type_stats[type_name] = stats
            draft_type = self._build_object_type(type_name, clean_rows, stats)
            draft.object_types.append(draft_type)

        # Pass 2 — objects (rows projected, with the inferred source_id).
        key_by_type = {ot.name: ot.source_id_field for ot in draft.object_types}
        for type_name, rows in grouped.items():
            key_field = key_by_type.get(type_name)
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                source_id = self._extract_source_id(row, key_field)
                draft.objects.append(
                    DraftObject(
                        type_name=type_name,
                        source_id=source_id,
                        properties=dict(row),
                    )
                )

        # Pass 3 — links from foreign-key shapes (field values matching another
        # type's keys). Skipped entirely when fewer than two types have keys.
        draft.links = self._infer_links(grouped, draft, type_stats, key_by_type)

        if not draft.links and len(draft.object_types) <= 1:
            draft.meta.setdefault("degraded", "objects-only")
        return draft

    # -------------------------------------------------------------- normalize
    def _normalize_input(self, records: Any, meta: Mapping[str, Any]) -> dict[str, list[Any]]:
        """Coerce the input into ``{type_name: [record, ...]}``.

        Accepts: a mapping of type->rows, or a flat sequence of records (single
        anonymous type). Empty / None inputs return ``{}``. Empty per-type lists
        are dropped so an all-empty mapping degrades to an empty draft.
        """
        if not records:
            return {}
        if isinstance(records, Mapping):
            out: dict[str, list[Any]] = {}
            for type_name, rows in records.items():
                if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                    row_list = list(rows)
                else:
                    row_list = []
                if row_list:
                    out[str(type_name)] = row_list
            return out
        if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
            row_list = list(records)
            if not row_list:
                return {}
            name = str(meta.get("default_type") or self._default_type_name)
            return {name: row_list}
        return {}

    # --------------------------------------------------------------- pass one
    def _collect_field_stats(self, rows: list[Mapping[str, Any]]) -> dict[str, _FieldStats]:
        stats: dict[str, _FieldStats] = defaultdict(_FieldStats)
        for row in rows:
            for field, value in row.items():
                stats[str(field)].observe(value)
        return dict(stats)

    def _build_object_type(
        self,
        type_name: str,
        rows: list[Mapping[str, Any]],
        stats: dict[str, _FieldStats],
    ) -> DraftObjectType:
        record_count = len(rows)
        properties: list[PropertyDef] = []
        field_map: dict[str, str] = {}
        for field, fstat in stats.items():
            prop_type = fstat.dominant_type()
            # required if present (and non-null) in every sampled record.
            required = record_count > 0 and fstat.non_null == record_count
            properties.append(PropertyDef(name=field, type=prop_type, required=required))
            field_map[field] = field

        key_field, key_conf = self._infer_primary_key(type_name, rows, stats)

        # Type confidence: blend sample size signal with average property-type
        # purity (how consistent the field shapes are across records).
        size_signal = _clamp(record_count / 5.0)  # 5+ records → full size signal
        purities = [s.type_purity for s in stats.values() if s.non_null]
        avg_purity = sum(purities) / len(purities) if purities else 0.0
        type_conf = _clamp(0.4 * size_signal + 0.6 * avg_purity) if stats else 0.0

        return DraftObjectType(
            name=type_name,
            properties=properties,
            source_id_field=key_field,
            field_map=field_map,
            confidence=round(type_conf, 3),
            key_confidence=round(key_conf, 3),
            record_count=record_count,
        )

    def _infer_primary_key(
        self,
        type_name: str,
        rows: list[Mapping[str, Any]],
        stats: dict[str, _FieldStats],
    ) -> tuple[str | None, float]:
        """Infer the primary-key field + confidence.

        Strategy:
          1. A named-key candidate (``id``, ``<type>_id``, …) that is unique +
             fully-populated wins with high confidence; merely present but not
             perfectly unique still wins by name but with reduced confidence.
          2. Else the highest-cardinality field that is fully-unique and fully
             populated, with confidence scaled by how distinctive it is and how
             much the name resembles a key.
          3. Else no key — ``(None, low)``.
        """
        if not rows or not stats:
            return None, 0.0

        n = len(rows)
        type_lc = re.sub(r"[^a-z0-9]", "", type_name.lower())
        # Resolve the priority name list against this type.
        name_priority = [p.replace("{type}", type_lc) for p in _KEY_NAME_PRIORITY]

        # 1) named candidates, in priority order.
        lower_to_field = {f.lower(): f for f in stats}
        for rank, wanted in enumerate(name_priority):
            field = lower_to_field.get(wanted)
            if field is None:
                continue
            fstat = stats[field]
            unique = fstat.non_null > 0 and len(fstat.distinct) == fstat.non_null
            populated = fstat.non_null == n
            # base confidence high for a named key, boosted by uniqueness +
            # population, gently decayed by priority rank.
            base = 0.95 - 0.05 * rank
            conf = base
            if not unique:
                conf -= 0.25
            if not populated:
                conf -= 0.15
            return field, _clamp(conf)

        # 2) high-cardinality unique fallback.
        best_field: str | None = None
        best_card = 0.0
        for field, fstat in stats.items():
            unique = fstat.non_null > 0 and len(fstat.distinct) == fstat.non_null
            populated = fstat.non_null == n
            if unique and populated and fstat.cardinality_ratio >= best_card:
                # prefer string/number keys; skip boolean/date as keys.
                if fstat.dominant_type() in ("boolean", "date"):
                    continue
                best_card = fstat.cardinality_ratio
                best_field = field
        if best_field is not None:
            # name resemblance bonus (ends with id/key/etc.)
            name_bonus = 0.1 if any(best_field.lower().endswith(s) for s in _FK_SUFFIXES) else 0.0
            # a single-record sample can't prove uniqueness — temper it.
            sample_signal = _clamp((n - 1) / 4.0)  # need a few rows to trust it
            conf = _clamp(0.45 + 0.25 * sample_signal + name_bonus)
            return best_field, conf

        # 3) no usable key.
        return None, 0.1

    @staticmethod
    def _extract_source_id(row: Mapping[str, Any], key_field: str | None) -> str | None:
        if key_field is None:
            return None
        raw = row.get(key_field)
        if raw is None:
            return None
        sid = str(raw).strip()
        return sid or None

    # --------------------------------------------------------------- pass three
    def _infer_links(
        self,
        grouped: dict[str, list[Any]],
        draft: OntologyDraft,
        type_stats: dict[str, dict[str, _FieldStats]],
        key_by_type: dict[str, str | None],
    ) -> list[DraftLink]:
        """Infer FK links: a field on type A whose values match type B's keys.

        For every (typeA, field) and every other typeB with a key, we test how
        many of typeA's field values land in typeB's key value set. A high
        overlap signals a foreign key. The field is skipped if it IS typeA's own
        key (self-reference noise) unless it clearly targets another type.
        """
        # Build per-type key value sets (as hashable surrogates).
        key_values: dict[str, set[Any]] = {}
        for type_name, key_field in key_by_type.items():
            if not key_field:
                continue
            stats = type_stats.get(type_name, {})
            fstat = stats.get(key_field)
            if fstat is not None:
                key_values[type_name] = set(fstat.distinct)

        if len(key_values) < 1:
            return []

        # Map a type's key surrogate -> source_id (string) for endpoint lookup.
        # Reuse the already-projected DraftObjects.
        sid_by_type_value: dict[str, dict[Any, str]] = defaultdict(dict)
        for obj in draft.objects:
            key_field = key_by_type.get(obj.type_name)
            if not key_field or obj.source_id is None:
                continue
            raw = obj.properties.get(key_field)
            sid_by_type_value[obj.type_name][_hashable(raw)] = obj.source_id

        links: list[DraftLink] = []
        for from_type, rows in grouped.items():
            stats = type_stats.get(from_type, {})
            own_key = key_by_type.get(from_type)
            for field, fstat in stats.items():
                if fstat.non_null == 0:
                    continue
                field_vals = set(fstat.distinct)
                for to_type, kvals in key_values.items():
                    if to_type == from_type and field == own_key:
                        continue  # self-key, not a link
                    if not kvals:
                        continue
                    overlap = field_vals & kvals
                    if not overlap:
                        continue
                    coverage = len(overlap) / len(field_vals)
                    # Require a meaningful overlap to call it an FK. A lone
                    # accidental match on a low-cardinality field is noise.
                    if coverage < 0.5 and len(overlap) < 2:
                        continue
                    # Name resemblance: ``customer_id`` -> ``Customer`` boosts.
                    name_match = self._name_targets_type(field, to_type)
                    confidence = _clamp(0.4 + 0.4 * coverage + (0.2 if name_match else 0.0))
                    link_type = self._link_type_name(field, to_type)
                    # Emit one link per matching row.
                    for row in rows:
                        if not isinstance(row, Mapping):
                            continue
                        val = row.get(field)
                        if val is None:
                            continue
                        h = _hashable(val)
                        if h not in kvals:
                            continue
                        to_sid = sid_by_type_value.get(to_type, {}).get(h)
                        from_sid = self._extract_source_id(row, own_key)
                        if to_sid is None or from_sid is None:
                            continue
                        links.append(
                            DraftLink(
                                from_type=from_type,
                                from_source_id=from_sid,
                                to_type=to_type,
                                to_source_id=to_sid,
                                link_type=link_type,
                                via_field=field,
                                confidence=round(confidence, 3),
                            )
                        )
        return links

    @staticmethod
    def _name_targets_type(field: str, type_name: str) -> bool:
        """True if a field name resembles a FK to ``type_name`` (customer_id→Customer)."""
        f = field.lower()
        t = re.sub(r"[^a-z0-9]", "", type_name.lower())
        stem = f
        for suf in _FK_SUFFIXES:
            if f.endswith(suf):
                stem = f[: -len(suf)]
                break
        stem = re.sub(r"[^a-z0-9]", "", stem)
        return stem == t or stem == t.rstrip("s") or f"{stem}s" == t

    @staticmethod
    def _link_type_name(field: str, to_type: str) -> str:
        """Derive a readable link_type from the FK field / target type."""
        f = field.lower()
        for suf in _FK_SUFFIXES:
            if f.endswith(suf) and len(f) > len(suf):
                return f"belongs_to_{f[: -len(suf)]}"
        return f"references_{to_type.lower()}"
