# tests/cloud/test_belt_component_map.py — the real ComponentResolver.
#
# Created: 2026-09-12 (integration/belt-factory) — pins the resolver that fills
# the seam ``belt/service.py`` shipped open. Until it existed every
# ``belt_entity_changed`` carried ``component: None``, so the Factory Map put
# every change in its unattributed bucket and lit no node.
#
# What this pins:
#   * a known file resolves to its owning component, read out of a real loom
#     world model's ``composes`` edges;
#   * the id is BARE (``fabric``, never ``pocketpaw:component:fabric``) — the
#     exact seam that would silently send everything to the unattributed bucket
#     while every other test in this file still passed;
#   * an unowned file, an unset path, a missing file, and malformed JSON each
#     resolve to ``None`` and NEVER raise — this sits on the hot path of every
#     agent tool call;
#   * the model is READ once across many lookups, and re-read when its mtime
#     changes (the loom-sync Stop hook rebuilds it under a running process);
#   * the production bridge defaults to this resolver — the wiring, not just
#     the resolver, since a correct resolver nobody injected is the bug this
#     whole module exists to fix.

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import component_map  # noqa: E402

# One edge of each kind that matters, in the exact shape the real model uses
# (verified against .loom/worldmodel-pocketpaw.json: 1,046 such edges).
_MODEL = {
    "schema": 1,
    "scope": "pocketpaw",
    "entities": [],
    "edges": [
        {
            "from": "pocketpaw:file:src/pocketpaw/fabric/store.py",
            "to": "pocketpaw:component:fabric",
            "type": "composes",
            "attrs": {"match": "fabric", "source": "c4"},
        },
        {
            "from": "pocketpaw:file:ee/pocketpaw_ee/cloud/pockets/service.py",
            "to": "pocketpaw:component:pockets",
            "type": "composes",
        },
        # Not a composes edge — must not contribute a mapping.
        {
            "from": "pocketpaw:file:src/pocketpaw/fabric/store.py",
            "to": "pocketpaw:file:src/pocketpaw/config.py",
            "type": "imports",
        },
        # A non-composes edge that DOES run file -> component. This one is the
        # only edge in the fixture that isolates the ``type`` check: the
        # ``imports`` edge above is already rejected for having no component
        # half, so without this one, deleting the type filter changes nothing
        # and the test passes on a gate that isn't there. Found by mutation,
        # not by review.
        {
            "from": "pocketpaw:file:src/pocketpaw/other.py",
            "to": "pocketpaw:component:runtime",
            "type": "references",
        },
        # A composes edge between two components — no file half, so it is not a
        # file->component mapping and must be skipped.
        {
            "from": "pocketpaw:component:fabric",
            "to": "pocketpaw:component:runtime",
            "type": "composes",
        },
    ],
}


@pytest.fixture(autouse=True)
def _clear_cache():
    """The parse memo is keyed on (path, mtime) and tmp_path files can collide
    on both across tests. Clearing it keeps each test independent — and the
    read-count test below asserts the caching itself, so nothing is hidden."""
    component_map._mapping.cache_clear()
    yield
    component_map._mapping.cache_clear()


@pytest.fixture
def model(tmp_path: Path) -> Path:
    p = tmp_path / "worldmodel-pocketpaw.json"
    p.write_text(json.dumps(_MODEL), encoding="utf-8")
    return p


@pytest.fixture
def loom(monkeypatch):
    """Point ``settings.loom_model_path`` at a path (or ``None``)."""

    def _apply(path) -> None:
        from pocketpaw.config import get_settings

        real = get_settings()

        class _S:
            loom_model_path = None if path is None else str(path)

            def __getattr__(self, name):
                return getattr(real, name)

        monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())

    return _apply


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_a_known_file_resolves_to_its_component(model, loom) -> None:
    loom(model)
    assert component_map.loom_component("pocketpaw:file:src/pocketpaw/fabric/store.py") == "fabric"
    assert (
        component_map.loom_component("pocketpaw:file:ee/pocketpaw_ee/cloud/pockets/service.py")
        == "pockets"
    )


def test_the_component_id_is_bare(model, loom) -> None:
    """The Factory Map's node ids are bare (``fabric``). Returning the
    namespaced id would match no node, and every change would land in the
    unattributed bucket — the same symptom as returning None, with none of the
    obviousness. This is the assertion that catches it."""
    loom(model)
    got = component_map.loom_component("pocketpaw:file:src/pocketpaw/fabric/store.py")
    assert got == "fabric"
    assert ":" not in (got or ""), f"expected a bare component id, got {got!r}"


def test_a_file_with_no_owner_resolves_to_none(model, loom) -> None:
    """~54% coverage is expected and fine — the UI has an unattributed bucket
    for exactly this."""
    loom(model)
    assert component_map.loom_component("pocketpaw:file:src/pocketpaw/config.py") is None


def test_only_composes_file_to_component_edges_count(model, loom) -> None:
    """An ``imports`` edge and a component->component ``composes`` edge are both
    in the fixture; neither may produce a mapping."""
    loom(model)
    assert component_map.loom_component("pocketpaw:component:fabric") is None
    mapping = component_map._current_mapping()
    assert set(mapping) == {
        "pocketpaw:file:src/pocketpaw/fabric/store.py",
        "pocketpaw:file:ee/pocketpaw_ee/cloud/pockets/service.py",
    }


def test_the_scope_is_not_hardcoded(tmp_path: Path, loom) -> None:
    """A soul-protocol world model resolves through the same code. The markers
    are ``:file:`` / ``:component:``, not a ``pocketpaw:`` prefix."""
    p = tmp_path / "worldmodel-soul.json"
    p.write_text(
        json.dumps(
            {
                "edges": [
                    {
                        "from": "soul-protocol:file:src/soul/cli.py",
                        "to": "soul-protocol:component:cli",
                        "type": "composes",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loom(p)
    assert component_map.loom_component("soul-protocol:file:src/soul/cli.py") == "cli"


# ---------------------------------------------------------------------------
# Fail-soft — this sits on the hot path of every agent tool call
# ---------------------------------------------------------------------------


def test_an_unset_model_path_resolves_to_none(loom) -> None:
    loom(None)
    assert component_map.loom_component("pocketpaw:file:src/pocketpaw/fabric/store.py") is None


def test_a_missing_file_resolves_to_none(tmp_path: Path, loom) -> None:
    loom(tmp_path / "does-not-exist.json")
    assert component_map.loom_component("pocketpaw:file:src/pocketpaw/fabric/store.py") is None


def test_malformed_json_resolves_to_none(tmp_path: Path, loom) -> None:
    p = tmp_path / "broken.json"
    p.write_text("{not json at all", encoding="utf-8")
    loom(p)
    assert component_map.loom_component("pocketpaw:file:src/pocketpaw/fabric/store.py") is None


@pytest.mark.parametrize(
    "body",
    [
        "[]",  # a list, not a dict
        '"just a string"',
        "{}",  # no edges key
        '{"edges": "not a list"}',
        '{"edges": [null, 3, "x"]}',  # edges that aren't dicts
        '{"edges": [{"from": 1, "to": 2, "type": "composes"}]}',  # non-string ids
    ],
)
def test_an_unexpected_shape_resolves_to_none(tmp_path: Path, loom, body: str) -> None:
    """Every one of these is a model we don't recognise. None of them may raise
    into an agent run."""
    p = tmp_path / "odd.json"
    p.write_text(body, encoding="utf-8")
    loom(p)
    assert component_map.loom_component("pocketpaw:file:src/pocketpaw/fabric/store.py") is None


def test_a_partial_model_still_contributes_what_it_has(tmp_path: Path, loom) -> None:
    """One malformed edge must not discard the good ones — a truncated model
    should degrade the highlight, not disable it."""
    p = tmp_path / "partial.json"
    p.write_text(
        json.dumps(
            {
                "edges": [
                    {"from": 1, "to": 2, "type": "composes"},
                    {
                        "from": "pocketpaw:file:a.py",
                        "to": "pocketpaw:component:runtime",
                        "type": "composes",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    loom(p)
    assert component_map.loom_component("pocketpaw:file:a.py") == "runtime"


# ---------------------------------------------------------------------------
# Caching — 47k edges, once per file write
# ---------------------------------------------------------------------------


def test_the_model_is_read_once_across_many_lookups(model, loom, monkeypatch) -> None:
    """Re-parsing ~47k edges per Write would be absurd. Counted at the READ, not
    at the parse, because the read is what the cache has to avoid."""
    loom(model)
    reads: list[str] = []
    real_read = Path.read_text

    def _counting_read(self, *a, **k):
        reads.append(str(self))
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _counting_read)

    for _ in range(25):
        assert (
            component_map.loom_component("pocketpaw:file:src/pocketpaw/fabric/store.py") == "fabric"
        )

    assert len(reads) == 1, f"expected one read of the model, got {len(reads)}"


def test_a_rebuilt_model_is_picked_up_without_a_restart(model, loom) -> None:
    """The loom-sync Stop hook rebuilds the world model under a long-lived
    process. A memo keyed only on the path would serve the old mapping forever;
    the mtime is in the key so a rebuild is a miss."""
    loom(model)
    assert component_map.loom_component("pocketpaw:file:src/pocketpaw/new_thing.py") is None

    rebuilt = dict(_MODEL)
    rebuilt["edges"] = [
        *_MODEL["edges"],
        {
            "from": "pocketpaw:file:src/pocketpaw/new_thing.py",
            "to": "pocketpaw:component:runtime",
            "type": "composes",
        },
    ]
    model.write_text(json.dumps(rebuilt), encoding="utf-8")
    # Force a distinct mtime — a same-second rewrite can otherwise land on the
    # identical stamp on a coarse-grained filesystem and make this test lie.
    stat = model.stat()
    import os

    os.utime(model, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    assert component_map.loom_component("pocketpaw:file:src/pocketpaw/new_thing.py") == "runtime"
    # The old entries survive the rebuild.
    assert component_map.loom_component("pocketpaw:file:src/pocketpaw/fabric/store.py") == "fabric"


# ---------------------------------------------------------------------------
# The wiring — a correct resolver nobody injected is the bug being fixed
# ---------------------------------------------------------------------------


def test_the_production_bridge_defaults_to_the_real_resolver() -> None:
    """``maybe_emit_belt_entity_changed`` is the path ``run_core`` calls. If its
    default is still ``no_component``, every event carries ``component: None``
    however good this module is — which is exactly the state this branch found
    the feature in."""
    import inspect

    from pocketpaw_ee.cloud.belt import service

    default = (
        inspect.signature(service.maybe_emit_belt_entity_changed)
        .parameters["resolve_component"]
        .default
    )
    assert default is component_map.loom_component
    assert default is not service.no_component


def test_the_resolver_factory_returns_a_usable_resolver(model, loom) -> None:
    loom(model)
    resolver = component_map.loom_component_resolver()
    assert resolver("pocketpaw:file:src/pocketpaw/fabric/store.py") == "fabric"
