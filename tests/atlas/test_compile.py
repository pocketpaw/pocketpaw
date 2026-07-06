# tests/atlas/test_compile.py — atlas compiler + drift check (AT-4).
# Created: 2026-07-02 (feat/atlas-compiler). Proves:
#   * byte-determinism: compile twice → identical bytes;
#   * the authored files validate and their entries survive the compile
#     unchanged (authored ⊂ compiled, field-for-field);
#   * connector extraction: a real repo connector (stripe) gets an entry
#     whose narrative carries its actions, and intent search reaches
#     connectors ("read my invoices" → a connector that lists invoices,
#     "stripe invoices" → connector:stripe);
#   * sense extraction: sense entries cross-link declaring connectors;
#   * `atlas build --check` passes on a fresh artifact, fails with a diff
#     summary on a tampered one, and the checked-in artifact is fresh;
#   * the startup drift check warns on mismatch (caplog), stays silent on
#     match, and never raises.
# Updated: 2026-07-02 (feat/atlas-widgets, AT-6) — the compiled artifact now
# also carries ``widget`` and ``skill`` entries; the "read my invoices"
# connector-knowledge pin checks limit=10 because the ripple invoice widgets
# legitimately occupy top name-weight slots (see the test docstring).
# Widget/skill extraction itself is pinned in test_widgets_skills.py.
# Updated: 2026-07-05 (fix/atlas-data-accuracy-and-relevance) — new
# ``TestFactualClaimGuard``: the fidelity check only proves the artifact was
# recompiled, not that a narrative is TRUE. Two false narratives had shipped
# and misdirected users (a "no dedicated billing route" claim and a "~190
# widgets" count); this guard flags negative route-existence claims and
# hedged numeric counts in authored prose so they can't silently pass a
# fidelity-only drift check. Surface count assertion bumped 21 → 23 for the
# new /settings/billing and /security authored surfaces.

import json
import logging
import os
import re
from pathlib import Path

from pocketpaw.atlas.compile import (
    AUTHORED_FILES,
    DEFAULT_CONNECTORS_DIR,
    check_artifact,
    compile_atlas,
    compile_atlas_bytes,
    load_authored_entries,
    serialize_atlas,
    write_artifact,
)
from pocketpaw.atlas.model import ATLAS_SCHEMA_V1, AtlasModel
from pocketpaw.atlas.store import _DATA_PATH, AtlasStore, check_connector_drift


class TestDeterminism:
    def test_compile_twice_yields_identical_bytes(self):
        first = compile_atlas_bytes()
        second = compile_atlas_bytes()
        assert first == second, "compiler must be byte-deterministic"

    def test_serialization_shape(self):
        """Sorted entry ids, sorted keys, trailing newline, generated flag."""
        raw = compile_atlas_bytes().decode("utf-8")
        assert raw.endswith("\n") and not raw.endswith("\n\n")
        doc = json.loads(raw)
        assert doc["schema"] == ATLAS_SCHEMA_V1
        assert doc["generated"] is True
        ids = [e["id"] for e in doc["entries"]]
        assert ids == sorted(ids), "entries must be sorted by id"

    def test_checked_in_artifact_is_fresh(self):
        """The committed data/atlas.json matches a fresh compile — the same
        gate CI runs via `pocketpaw atlas build --check`."""
        fresh, summary = check_artifact()
        assert fresh, summary


class TestAuthoredFiles:
    def test_authored_files_validate_against_schema(self):
        for path in AUTHORED_FILES:
            raw = json.loads(path.read_text(encoding="utf-8"))
            model = AtlasModel.model_validate(raw)
            assert model.schema_ == ATLAS_SCHEMA_V1
            assert model.generated is False, "authored files are sources, not compiled"
            assert model.entries

    def test_authored_kinds_are_split_by_file(self):
        prims = AtlasModel.model_validate(json.loads(AUTHORED_FILES[0].read_text(encoding="utf-8")))
        surfs = AtlasModel.model_validate(json.loads(AUTHORED_FILES[1].read_text(encoding="utf-8")))
        assert {e.kind for e in prims.entries} == {"primitive"}
        assert {e.kind for e in surfs.entries} == {"surface"}
        assert len(prims.entries) == 10
        assert len(surfs.entries) == 23

    def test_authored_entries_survive_compile_unchanged(self):
        """Every authored entry appears in the compiled model identical
        field-for-field — the compiler never rewrites authored content."""
        compiled = {e.id: e for e in compile_atlas().entries}
        for entry in load_authored_entries():
            assert compiled[entry.id] == entry


class TestFactualClaimGuard:
    """Flag brittle factual / numeric claims in authored prose.

    The fidelity check (`atlas build --check`) only proves the artifact was
    recompiled from the sources — it cannot tell a TRUE narrative from a
    FALSE one. Two false-narrative classes shipped and misdirected users
    (fixed 2026-07-05): a negative existence claim ("there is no dedicated
    billing route today") that went stale the moment /settings/billing
    shipped, and a hedged widget count ("~190 widgets") that drifted from
    the real 151. This guard catches both patterns so a stale factual claim
    can't silently pass a fidelity-only drift check. It is deliberately
    narrow — it flags the specific brittle shapes, not all prose — so it
    stays low-noise.
    """

    # Negative existence claims about routes / surfaces / pages: these go
    # stale exactly when the OS gains the thing they deny.
    _NEGATIVE_EXISTENCE = re.compile(
        r"\bno\s+(?:dedicated\s+)?[\w\s]{0,20}?(?:route|surface|page)\b", re.IGNORECASE
    )
    # Hedged / approximate counts ("~190 widgets", "about 150 widgets",
    # "roughly 200 widgets"): an approximation invites silent drift. A
    # verified exact count ("150+ widgets") is allowed.
    _APPROX_COUNT = re.compile(
        r"(?:[~≈]\s*|\b(?:about|around|roughly|approx(?:imately)?)\s+)\d{2,}\b",
        re.IGNORECASE,
    )

    def test_no_negative_route_existence_claims(self):
        offenders = []
        for entry in load_authored_entries():
            for field in ("summary", "narrative"):
                text = getattr(entry, field)
                if self._NEGATIVE_EXISTENCE.search(text):
                    offenders.append(f"{entry.id}.{field}: {text}")
        assert not offenders, (
            "authored prose asserts a route/surface does NOT exist — these go "
            "stale when the OS gains it (a false narrative misdirects users). "
            "State what IS, not what is absent:\n" + "\n".join(offenders)
        )

    def test_no_hedged_numeric_claims(self):
        offenders = []
        for entry in load_authored_entries():
            for field in ("summary", "narrative"):
                text = getattr(entry, field)
                if self._APPROX_COUNT.search(text):
                    offenders.append(f"{entry.id}.{field}: {text}")
        assert not offenders, (
            "authored prose carries a hedged/approximate count (e.g. '~190 "
            "widgets') — approximations drift silently past the fidelity-only "
            "check. Use a verified exact count or an open-ended '150+':\n" + "\n".join(offenders)
        )


class TestConnectorExtraction:
    def test_stripe_entry_carries_actions_in_narrative(self):
        entry = AtlasStore.load().describe("connector:stripe")
        assert entry is not None, "stripe.yaml exists in connectors/ — must be extracted"
        assert entry.kind == "connector"
        assert "list_invoices" in entry.narrative
        assert "list_subscriptions" in entry.narrative
        assert entry.requires == ["primitive:connector"]
        assert "stripe" in entry.keywords

    def test_read_my_invoices_reaches_a_connector_that_lists_invoices(self):
        """The slice this fixes: intent search must surface connector
        knowledge, not just primitives.

        Since AT-6 the top-5 for invoice vocabulary legitimately includes
        the ripple invoice widgets (`widget:invoice-layout` /
        `widget:invoice-lines` hit at name weight — rendering an invoice IS
        a capability answer), and several connectors tie with stripe on the
        generic "read" keyword. The connector-knowledge guarantee is kept
        at limit=10: an invoice-capable connector must still surface.
        """
        results = AtlasStore.load().search("read my invoices", limit=10)
        connector_hits = [
            e for e in results if e.kind == "connector" and "invoice" in e.narrative.lower()
        ]
        assert connector_hits, (
            f"expected an invoice-capable connector, got {[e.id for e in results]}"
        )

    def test_stripe_invoices_ranks_stripe_first(self):
        results = AtlasStore.load().search("stripe invoices")
        assert results and results[0].id == "connector:stripe", (
            f"expected connector:stripe first, got {[e.id for e in results]}"
        )

    def test_every_repo_connector_yaml_has_an_entry(self):

        import yaml

        store = AtlasStore.load()
        for path in sorted(Path("connectors").glob("*.yaml")):
            name = yaml.safe_load(path.read_text(encoding="utf-8"))["name"]
            assert store.describe(f"connector:{name}") is not None, (
                f"{path} missing from compiled atlas — run `pocketpaw atlas build`"
            )


class TestSenseExtraction:
    def test_payments_sense_links_stripe(self):
        entry = AtlasStore.load().describe("sense:paw.payments.v1")
        assert entry is not None
        assert entry.kind == "sense"
        assert "stripe" in entry.narrative
        assert "connector:stripe" in entry.requires

    def test_all_core_senses_have_entries_with_declaring_connectors(self):
        from pocketpaw.senses.vocabulary import CORE_SENSES

        store = AtlasStore.load()
        for sense in CORE_SENSES:
            entry = store.describe(f"sense:{sense.id}")
            assert entry is not None, f"sense {sense.id} missing from compiled atlas"
            assert entry.summary == sense.description
            # Every core sense is backed by >=1 connector (vocabulary guard
            # rule), so the entry must cross-link at least one.
            assert entry.requires, f"{entry.id} must link its declaring connectors"


class TestCheckArtifact:
    def test_check_passes_on_fresh_artifact(self, tmp_path):
        path, _model = write_artifact(output_path=tmp_path / "atlas.json")
        fresh, summary = check_artifact(artifact_path=path)
        assert fresh and summary == ""

    def test_check_fails_on_tampered_artifact(self, tmp_path):
        path, _model = write_artifact(output_path=tmp_path / "atlas.json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["entries"] = [e for e in doc["entries"] if e["id"] != "connector:stripe"]
        path.write_text(json.dumps(doc), encoding="utf-8")
        fresh, summary = check_artifact(artifact_path=path)
        assert not fresh
        assert "connector:stripe" in summary
        assert "pocketpaw atlas build" in summary

    def test_check_fails_on_missing_artifact(self, tmp_path):
        fresh, summary = check_artifact(artifact_path=tmp_path / "nope.json")
        assert not fresh and "missing" in summary


class TestDriftCheck:
    def test_warns_on_connector_mismatch(self, caplog):
        store = AtlasStore.load(_DATA_PATH)
        atlas_names = {e.id.split(":", 1)[1] for e in store.entries if e.kind == "connector"}
        with caplog.at_level(logging.WARNING, logger="pocketpaw.atlas.store"):
            drifted = check_connector_drift(store, live_names=atlas_names | {"brand-new-connector"})
        assert drifted is True
        assert any(
            "atlas is stale" in r.message and "brand-new-connector" in r.getMessage()
            for r in caplog.records
        )

    def test_silent_on_match(self, caplog):
        store = AtlasStore.load(_DATA_PATH)
        atlas_names = {e.id.split(":", 1)[1] for e in store.entries if e.kind == "connector"}
        with caplog.at_level(logging.WARNING, logger="pocketpaw.atlas.store"):
            drifted = check_connector_drift(store, live_names=atlas_names)
        assert drifted is False
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_never_raises_even_on_broken_input(self, caplog):
        """The drift check must never take the store down with it."""

        class _Boom:
            @property
            def entries(self):
                raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="pocketpaw.atlas.store"):
            assert check_connector_drift(_Boom()) is False  # type: ignore[arg-type]

    def test_serialize_roundtrip_loads_in_store(self, tmp_path):
        """A compiled artifact loads through the unchanged store API."""
        path = tmp_path / "atlas.json"
        path.write_bytes(serialize_atlas(compile_atlas()))
        store = AtlasStore.load(path)
        assert store.describe("primitive:pocket") is not None
        assert store.describe("connector:stripe") is not None


class TestNullDisplayName:
    """FINDING A — a connector YAML with an explicit ``display_name: null``
    parses cleanly but must not crash the whole compiler on ``.lower()``.

    The module's own posture ("one bad file can't block the build") is
    broken if a single well-formed-but-null field aborts the entire build
    (and the CI drift gate) with AttributeError.
    """

    def test_connector_with_null_display_name_compiles(self, tmp_path):
        (tmp_path / "quiet.yaml").write_text(
            "name: quiet\n"
            "display_name: null\n"
            "type: generic\n"
            "actions:\n"
            "  - name: ping\n"
            "    description: check liveness\n",
            encoding="utf-8",
        )
        model = compile_atlas(connectors_dir=tmp_path)
        entry = next(e for e in model.entries if e.id == "connector:quiet")
        # Falls back to the connector name — never the string "None".
        assert entry.name == "quiet"
        assert "None" not in entry.summary
        assert "None" not in entry.narrative
        assert "quiet" in entry.keywords


class TestConnectorsDirAnchoring:
    """FINDING C — the default connectors dir must not be CWD-relative.

    ``compile_atlas`` / ``write_artifact`` / ``check_artifact`` called with
    defaults from any dir other than the repo root would otherwise read ZERO
    connector YAMLs and silently drop every connector + sense entry;
    ``write_artifact`` would then persist a truncated artifact.
    """

    def test_default_connectors_dir_is_absolute(self):
        assert DEFAULT_CONNECTORS_DIR.is_absolute(), (
            "DEFAULT_CONNECTORS_DIR must be anchored to a stable location, not CWD-relative"
        )
        assert DEFAULT_CONNECTORS_DIR.is_dir(), "the anchored connectors dir must exist"

    def test_compile_from_other_cwd_still_finds_connectors(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # A CWD-relative default would find nothing here.
        assert not (Path(os.getcwd()) / "connectors").exists()
        model = compile_atlas()
        connectors = [e for e in model.entries if e.kind == "connector"]
        senses = [e for e in model.entries if e.kind == "sense"]
        assert connectors, "connectors must still be extracted from a different CWD"
        assert senses, "senses must still be extracted from a different CWD"
        assert any(e.id == "connector:stripe" for e in connectors)

    def test_check_artifact_fresh_from_other_cwd(self, tmp_path, monkeypatch):
        """The CI drift gate runs from repo root today; anchoring keeps it
        correct even if invoked elsewhere — no truncated-artifact false
        'stale' with everything dropped."""
        monkeypatch.chdir(tmp_path)
        fresh, summary = check_artifact()
        assert fresh, summary


class TestDuplicateIdDiff:
    """FINDING E — the drift diff collapses duplicate-id entries.

    ``check_artifact`` built its per-id map with a dict comprehension, so two
    entries sharing an id silently collapse to one: CI still fails on bytes,
    but the printed added/removed/changed lists misreport what actually
    differs. Duplicate ids should be surfaced explicitly.
    """

    def test_duplicate_ids_reported(self, tmp_path):
        path, _model = write_artifact(output_path=tmp_path / "atlas.json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        # Inject a duplicate of an existing id (authoring collision shape).
        first = doc["entries"][0]
        doc["entries"].append(dict(first))
        path.write_text(json.dumps(doc), encoding="utf-8")
        fresh, summary = check_artifact(artifact_path=path)
        assert not fresh
        assert "duplicate" in summary.lower(), (
            f"duplicate id must be surfaced in the diff summary, got:\n{summary}"
        )
        assert first["id"] in summary
