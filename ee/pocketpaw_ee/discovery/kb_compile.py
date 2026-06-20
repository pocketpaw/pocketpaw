# pocketpaw_ee/discovery/kb_compile.py — the KbCompileDigester (S2-K1).
#
# Created: 2026-06-20 (S2-K1 / feat/szd-slice2-discovery) — the second Digester
# implementation for sovereign zero-setup discovery. Where StructuredShapeDigester
# reverse-engineers an ontology from typed record dicts, KbCompileDigester handles
# UNSTRUCTURED exhaust (ticket bodies, email/chat text) that has no record shape:
# it compiles the text into a kb-go wiki ON-BOX, reads the compiled articles back,
# and infers the OntologyDraft from them.
#
# Contract: ``digest(records, connector_meta=None) -> OntologyDraft`` — the same
# sync Digester Protocol StructuredShapeDigester satisfies. Here ``records`` are
# TEXT blobs grouped by type label (``{type_label: [text, ...]}`` — the shape the
# DiscoveryRun sampler lands in ``grouped`` for an unstructured connector), or a
# flat ``list[text]``. Never raises on empty/degenerate input (degrades via
# ``draft.meta``). Stamps ``meta["digester"] = "kb-compile"``.
#
# Inference: article ``categories`` → object types; article ``id`` →
# ``source_id_field``; properties over ``{title, summary, concepts, categories}``;
# concept co-occurrence (two articles of different types sharing a concept) →
# a ``DraftLink`` with ``via_field`` = the shared concept. Every confidence is
# ``_clamp``-bounded into [0, 1]. ``to_fabric_mapping_kwargs()`` yields a valid
# ``FabricMapping`` for each keyed type.
#
# SOVEREIGNTY (load-bearing, encoded as a test): compilation uses ONLY the
# keyless on-box path (``kb convo ingest`` — deterministic, no LLM). It must
# NEVER call ``kb ingest`` / ``kb build``, which POST raw tenant text to the
# Anthropic API (kb.go:1349). The ``_kb`` seam below clones
# ``cloud/agents/knowledge.py`` (binary resolution + subprocess + timeout) and is
# the seam the unit tests mock; the digest path simply never asks it for ingest.
#
# Pure orchestration over a subprocess: no DB, no async, honours the subprocess
# timeout. Depends on stdlib + the OSS PropertyDef + the discovery models.

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pocketpaw.fabric.models import PropertyDef
from pocketpaw_ee.discovery.models import (
    DraftLink,
    DraftObject,
    DraftObjectType,
    OntologyDraft,
    _clamp,
)

logger = logging.getLogger(__name__)

# The four article fields the digester projects as properties.
_ARTICLE_PROPERTY_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "string"),
    ("summary", "string"),
    ("concepts", "string"),
    ("categories", "string"),
)

# Confidence when the article id cleanly keys a categorized type (the article id
# is the natural, always-unique idempotency key for a compiled article).
_KEYED_CONFIDENCE = 0.85
# Confidence floor for an uncategorized / keyless degraded type.
_KEYLESS_CONFIDENCE = 0.1


# --------------------------------------------------------------------------- #
# kb-go subprocess seam — cloned from cloud/agents/knowledge.py:36-97.
# This is the seam the unit tests MOCK. The digest path only ever asks it for
# the keyless on-box commands (convo ingest / list / show / graph), NEVER for
# `ingest` / `build` (those POST to Anthropic — a sovereignty violation).
# --------------------------------------------------------------------------- #
def _resolve_kb_bin() -> str:
    """Find the kb binary, in order of preference (mirrors knowledge.py).

    1. ``POCKETPAW_KB_BIN`` env var (explicit override).
    2. ``kb-go`` on PATH, then ``kb`` on PATH.
    3. Workspace-local checkout at ``<paw-workspace>/kb-go/kb``.

    Returns the literal ``"kb-go"`` when nothing resolves so the error message
    stays informative. Resolved at import time.
    """
    explicit = os.environ.get("POCKETPAW_KB_BIN")
    if explicit:
        return explicit
    for name in ("kb-go", "kb"):
        path = shutil.which(name)
        if path:
            return path
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "kb-go" / "kb"
        if candidate.exists():
            return str(candidate)
    return "kb-go"


KB_BIN = _resolve_kb_bin()


def _kb(*args: str, input_text: str | None = None, timeout: int = 120) -> dict | list | str:
    """Call the kb binary, return parsed JSON or raw text.

    Cloned from ``cloud/agents/knowledge.py:_kb`` — a blocking ``subprocess.run``
    with a timeout. Callers keep it synchronous (the Digester Protocol is sync);
    the orchestrator already drives the whole digest under ``asyncio.to_thread``.
    """
    cmd = [KB_BIN, *args, "--json"]
    try:
        result = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"kb binary not found at {KB_BIN!r}. "
            "Install: go install github.com/qbtrix/kb-go@latest, "
            "or set POCKETPAW_KB_BIN to the binary path (e.g. /path/to/kb-go/kb), "
            "or place the workspace-local checkout at <paw-workspace>/kb-go/kb."
        ) from exc
    if result.returncode != 0:
        logger.warning("kb failed (exit %d): %s", result.returncode, result.stderr[:200])
        raise RuntimeError(f"kb failed: {result.stderr[:200]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()


class KbCompileDigester:
    """Infers an OntologyDraft from unstructured text exhaust, compiled on-box.

    Input shape: ``{type_label: [text, ...]}`` (text blobs grouped the way the
    DiscoveryRun sampler lands them in ``grouped``) or a flat ``list[text]``.
    Each label's blobs are compiled into a discovery-private kb-go scope via the
    KEYLESS ``kb convo ingest`` path; the compiled articles are read back
    (``list`` → ``show``) and their concept graph queried (``graph``); the draft
    is inferred from the articles.

    Conforms to the ``Digester`` Protocol structurally (single sync ``digest``).
    """

    def __init__(self, scope_prefix: str = "workspace", scope_suffix: str = "discovery") -> None:
        # Scope shape: ``{scope_prefix}:{workspace_id}:{scope_suffix}`` — a
        # discovery-private kb-go scope so compiled exhaust never collides with
        # an agent/workspace KB.
        self._scope_prefix = scope_prefix
        self._scope_suffix = scope_suffix

    # ------------------------------------------------------------------ public
    def digest(
        self,
        records: Any,
        connector_meta: Mapping[str, Any] | None = None,
    ) -> OntologyDraft:
        meta = dict(connector_meta or {})
        grouped = self._normalize_input(records)

        draft = OntologyDraft(meta={"digester": "kb-compile", **meta})
        if not grouped:
            draft.meta["degraded"] = "empty"
            return draft

        scope = self._compile_scope(meta)

        # 1) Compile every text blob into the discovery scope, keyless + on-box.
        for label, blobs in grouped.items():
            self._compile_blobs(scope, label, blobs)

        # 2) Read the compiled articles back (list → show for full bodies).
        articles = self._read_articles(scope)
        if not articles:
            # The compile produced nothing readable — degrade cleanly to empty.
            draft.meta["degraded"] = "empty"
            return draft

        # 3) Query the concept co-occurrence graph (on-box, no LLM). Best-effort:
        #    link inference falls back to the article concepts if graph is empty.
        self._read_concept_graph(scope)

        # 4) Group articles into object types by category.
        typed, uncategorized = self._partition_by_category(articles)

        if typed:
            for type_name, type_articles in typed.items():
                draft.object_types.append(self._build_typed_object_type(type_name, type_articles))
                for art in type_articles:
                    draft.objects.append(
                        DraftObject(
                            type_name=type_name,
                            source_id=str(art.get("id")) if art.get("id") else None,
                            properties=self._project_article(art),
                        )
                    )

        if uncategorized:
            # Articles with no category degrade to a single objects-only type
            # named from the connector — no usable key, low confidence, no links.
            objects_type = self._objects_only_type_name(meta)
            draft.object_types.append(self._build_keyless_object_type(objects_type, uncategorized))
            for art in uncategorized:
                draft.objects.append(
                    DraftObject(
                        type_name=objects_type,
                        source_id=None,
                        properties=self._project_article(art),
                    )
                )

        # 5) Links from concept co-occurrence across DIFFERENT typed articles.
        draft.links = self._infer_concept_links(typed)

        if uncategorized and not typed:
            draft.meta.setdefault("degraded", "objects-only")
        elif not draft.links and len(draft.object_types) <= 1 and not typed:
            draft.meta.setdefault("degraded", "objects-only")

        return draft

    # -------------------------------------------------------------- normalize
    def _normalize_input(self, records: Any) -> dict[str, list[str]]:
        """Coerce the input into ``{type_label: [text, ...]}`` of non-empty strings.

        Accepts a mapping of label→text-blobs, or a flat sequence of text blobs
        (single anonymous label). Empty / None inputs → ``{}``. Non-string blobs
        are coerced via ``str``; empty/whitespace-only blobs are dropped. Labels
        whose blob list empties out are dropped so an all-empty input degrades.
        """
        if not records:
            return {}

        def _clean(blobs: Any) -> list[str]:
            if isinstance(blobs, (str, bytes)):
                items: Sequence[Any] = [blobs]
            elif isinstance(blobs, Sequence):
                items = blobs
            else:
                return []
            out: list[str] = []
            for b in items:
                text = b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)
                text = text.strip()
                if text:
                    out.append(text)
            return out

        if isinstance(records, Mapping):
            out: dict[str, list[str]] = {}
            for label, blobs in records.items():
                cleaned = _clean(blobs)
                if cleaned:
                    out[str(label)] = cleaned
            return out

        if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
            cleaned = _clean(records)
            return {"Record": cleaned} if cleaned else {}

        return {}

    # ------------------------------------------------------------------- scope
    def _compile_scope(self, meta: Mapping[str, Any]) -> str:
        wid = str(meta.get("workspace_id") or "default")
        return f"{self._scope_prefix}:{wid}:{self._scope_suffix}"

    @staticmethod
    def _objects_only_type_name(meta: Mapping[str, Any]) -> str:
        raw = str(meta.get("connector") or meta.get("default_type") or "Document")
        # Title-case a connector-ish label into a type name (zendesk → Zendesk).
        cleaned = "".join(ch for ch in raw if ch.isalnum()) or "Document"
        return cleaned[:1].upper() + cleaned[1:]

    # ---------------------------------------------------------------- compile
    def _compile_blobs(self, scope: str, label: str, blobs: Sequence[str]) -> None:
        """Compile a label's text blobs into ``scope`` via the keyless path.

        Writes the blobs to a temp transcript file and runs ``kb convo ingest``
        — fully deterministic, no LLM, no API key (kb-go convo.go). NEVER
        ``kb ingest`` / ``kb build``. Subprocess failures are non-fatal: one
        bad label can't kill the digest (it just contributes no articles).
        """
        if not blobs:
            return
        # A simple transcript: one labelled turn per blob. ``convo ingest``
        # parses turns heuristically; the exact format is forgiving.
        transcript = "\n\n".join(f"{label}: {blob}" for blob in blobs)
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", encoding="utf-8", delete=False
            ) as fh:
                fh.write(transcript)
                tmp_path = fh.name
            _kb("convo", "ingest", tmp_path, "--scope", scope)
        except Exception as exc:  # noqa: BLE001 — one bad label can't kill the run
            logger.warning("kb-compile: convo ingest failed for label %s: %s", label, exc)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ------------------------------------------------------------------- read
    def _read_articles(self, scope: str) -> list[dict[str, Any]]:
        """Read compiled articles back: ``list`` for ids, ``show`` for bodies.

        ``kb list --json`` omits ``concepts`` / ``categories`` (kb.go:cmdList),
        so each article is enriched via ``kb show <id>`` which returns the full
        WikiArticle (id, title, summary, content, concepts, categories).
        """
        try:
            listed = _kb("list", "--scope", scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("kb-compile: list failed for scope %s: %s", scope, exc)
            return []
        if not isinstance(listed, list):
            return []

        articles: list[dict[str, Any]] = []
        for entry in listed:
            if not isinstance(entry, Mapping):
                continue
            art_id = entry.get("id")
            if not art_id:
                continue
            full = self._show_article(scope, str(art_id))
            if full is None:
                # Fall back to the lean list entry (no concepts/categories).
                full = dict(entry)
            articles.append(full)
        return articles

    def _show_article(self, scope: str, article_id: str) -> dict[str, Any] | None:
        try:
            shown = _kb("show", article_id, "--scope", scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("kb-compile: show failed for %s: %s", article_id, exc)
            return None
        if isinstance(shown, Mapping) and shown.get("id"):
            return dict(shown)
        return None

    def _read_concept_graph(self, scope: str) -> dict[str, Any]:
        """Query the concept co-occurrence graph (on-box, no LLM). Best-effort."""
        try:
            graph = _kb("graph", "--scope", scope, "--format", "json")
        except Exception as exc:  # noqa: BLE001 — graph is advisory; never fatal
            logger.info("kb-compile: graph failed for scope %s: %s", scope, exc)
            return {}
        return dict(graph) if isinstance(graph, Mapping) else {}

    # -------------------------------------------------------------- inference
    @staticmethod
    def _partition_by_category(
        articles: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        """Split articles into ``{category: [article, ...]}`` + uncategorized.

        An article's first category names its object type. Articles with no
        category are uncategorized (they degrade to objects-only).
        """
        typed: dict[str, list[dict[str, Any]]] = defaultdict(list)
        uncategorized: list[dict[str, Any]] = []
        for art in articles:
            cats = art.get("categories") or []
            cat = None
            if isinstance(cats, Sequence) and not isinstance(cats, (str, bytes)):
                for c in cats:
                    if isinstance(c, str) and c.strip():
                        cat = c.strip()
                        break
            elif isinstance(cats, str) and cats.strip():
                cat = cats.strip()
            if cat:
                typed[cat].append(dict(art))
            else:
                uncategorized.append(dict(art))
        return dict(typed), uncategorized

    @staticmethod
    def _project_article(art: Mapping[str, Any]) -> dict[str, Any]:
        """Project an article onto the four ontology property fields."""
        return {
            "id": art.get("id"),
            "title": art.get("title", ""),
            "summary": art.get("summary", ""),
            "concepts": list(art.get("concepts") or []),
            "categories": list(art.get("categories") or []),
        }

    def _build_typed_object_type(
        self, type_name: str, articles: Sequence[Mapping[str, Any]]
    ) -> DraftObjectType:
        """Build a keyed object type — article ``id`` is the source_id_field.

        The article id is always unique and fully populated for a compiled
        article, so the key confidence is high. Type confidence scales with the
        sample size (how many articles landed in this category).
        """
        properties = [PropertyDef(name="id", type="string", required=True)]
        field_map: dict[str, str] = {"id": "id"}
        for field, ptype in _ARTICLE_PROPERTY_FIELDS:
            properties.append(PropertyDef(name=field, type=ptype, required=False))
            field_map[field] = field

        size_signal = _clamp(len(articles) / 3.0)  # 3+ articles → full size signal
        type_conf = _clamp(0.5 + 0.5 * size_signal)

        return DraftObjectType(
            name=type_name,
            properties=properties,
            source_id_field="id",
            field_map=field_map,
            confidence=round(type_conf, 3),
            key_confidence=round(_KEYED_CONFIDENCE, 3),
            record_count=len(articles),
        )

    def _build_keyless_object_type(
        self, type_name: str, articles: Sequence[Mapping[str, Any]]
    ) -> DraftObjectType:
        """Build an objects-only type — no usable key, low confidence."""
        properties: list[PropertyDef] = []
        field_map: dict[str, str] = {}
        for field, ptype in _ARTICLE_PROPERTY_FIELDS:
            properties.append(PropertyDef(name=field, type=ptype, required=False))
            field_map[field] = field
        return DraftObjectType(
            name=type_name,
            properties=properties,
            source_id_field=None,
            field_map=field_map,
            confidence=round(_clamp(0.3), 3),
            key_confidence=round(_KEYLESS_CONFIDENCE, 3),
            record_count=len(articles),
        )

    def _infer_concept_links(self, typed: Mapping[str, list[dict[str, Any]]]) -> list[DraftLink]:
        """Link articles of different types that share a concept.

        Concept co-occurrence is the unstructured analogue of a foreign key: two
        articles of different object types tagged with the same concept signal a
        relationship. ``via_field`` is the shared concept; ``link_type`` records
        the concept; confidence scales with how rare (and thus how specific) the
        concept is across the corpus.
        """
        if len(typed) < 2:
            return []

        # concept -> [(type_name, source_id), ...]
        concept_index: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for type_name, articles in typed.items():
            for art in articles:
                sid = art.get("id")
                if not sid:
                    continue
                for concept in art.get("concepts") or []:
                    if isinstance(concept, str) and concept.strip():
                        concept_index[concept.strip()].append((type_name, str(sid)))

        links: list[DraftLink] = []
        for concept, endpoints in concept_index.items():
            if len(endpoints) < 2:
                continue
            # Rarer concepts are more specific signals → higher confidence.
            specificity = _clamp(2.0 / len(endpoints))
            confidence = _clamp(0.4 + 0.4 * specificity)
            # Emit a link for each cross-type pair sharing the concept.
            for i in range(len(endpoints)):
                from_type, from_sid = endpoints[i]
                for j in range(i + 1, len(endpoints)):
                    to_type, to_sid = endpoints[j]
                    if from_type == to_type:
                        continue  # same-type co-occurrence isn't a cross-type link
                    links.append(
                        DraftLink(
                            from_type=from_type,
                            from_source_id=from_sid,
                            to_type=to_type,
                            to_source_id=to_sid,
                            link_type=f"shares_concept_{concept.lower()}",
                            via_field=concept,
                            confidence=round(confidence, 3),
                        )
                    )
        return links
