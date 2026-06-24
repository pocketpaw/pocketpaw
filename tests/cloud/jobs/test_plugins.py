# tests/cloud/jobs/test_plugins.py
# Created: 2026-06-22 (feat/jobs-custom-job-entrypoints) — TDD coverage for the
# SAFE entry-point discovery path that registers WORKSPACE-CUSTOM jobs. These
# tests never install a real package: they monkeypatch the
# ``entry_points`` symbol the loader calls and feed it fake entry-points whose
# ``.load()`` returns a factory. They pin:
#   - a single-JobCallable factory registers and resolves by name;
#   - a factory returning a LIST registers every job;
#   - no entry-points → graceful no-op (returns 0, no raise);
#   - a provider that fails to ``.load()`` is skipped with a warning, doesn't
#     crash, and doesn't block a sibling valid provider;
#   - a factory that raises on call is skipped, sibling still loads;
#   - a resolved object that is NOT a JobCallable is skipped with a warning,
#     sibling still loads;
#   - re-running is idempotent (re-registering the same name just overwrites).
# Mirrors the registry-isolation convention in test_jobs.py (save/clear/restore
# the module-level registry per test).

from __future__ import annotations

import logging

import pytest
from pocketpaw_ee.cloud.jobs import plugins as jobs_plugins
from pocketpaw_ee.cloud.jobs import registry as jobs_registry
from pocketpaw_ee.cloud.jobs.registry import UnknownJobError

# ---------------------------------------------------------------------------
# Test doubles — JobCallables, factories, and a fake entry-point.
# ---------------------------------------------------------------------------


class _StubJob:
    """A minimal valid JobCallable a custom-job package would ship."""

    def __init__(self, name: str = "stub_custom_job") -> None:
        self.name = name

    async def __call__(
        self, *, workspace_id: str, pocket_id: str, job_id: str, params: dict
    ) -> dict:
        return {"state": {"ran": self.name}}


class _NotAJob:
    """Resolves from a factory but has no `name` / `__call__` — not a JobCallable."""


class _FakeEntryPoint:
    """Mimics ``importlib.metadata.EntryPoint`` for the loader's needs.

    The loader only touches ``.name`` and ``.load()``; ``load`` here returns
    the supplied factory (or raises ``load_exc`` to simulate an import failure).
    """

    def __init__(self, name: str, factory=None, *, load_exc: Exception | None = None) -> None:
        self.name = name
        self._factory = factory
        self._load_exc = load_exc

    def load(self):
        if self._load_exc is not None:
            raise self._load_exc
        return self._factory


def _patch_entry_points(monkeypatch, eps: list[_FakeEntryPoint]) -> None:
    """Patch the ``entry_points`` symbol the loader imported into its module.

    The loader calls ``entry_points(group=...)`` and iterates the result, so the
    fake must accept the ``group`` kwarg and return our list.
    """

    def _fake_entry_points(*, group: str):
        assert group == jobs_plugins.JOBS_ENTRYPOINT_GROUP
        return eps

    monkeypatch.setattr(jobs_plugins, "entry_points", _fake_entry_points)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate the module-level registry per test (save / clear / restore)."""
    saved = dict(jobs_registry.get_job_registry())
    jobs_registry.get_job_registry().clear()
    yield
    reg = jobs_registry.get_job_registry()
    reg.clear()
    reg.update(saved)


# ---------------------------------------------------------------------------
# Happy path — a factory returning a single JobCallable.
# ---------------------------------------------------------------------------


def test_loads_single_job_from_factory(monkeypatch) -> None:
    def make_jobs():
        return _StubJob("the_stub_name")

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("my_jobs", make_jobs)])

    count = jobs_plugins.load_entrypoint_jobs()

    assert count == 1
    # The contract: the resolved job is now in the process registry by name.
    resolved = jobs_registry.resolve_job("the_stub_name")
    assert resolved.name == "the_stub_name"


def test_loads_list_of_jobs_from_factory(monkeypatch) -> None:
    def make_jobs():
        return [_StubJob("job_a"), _StubJob("job_b")]

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("my_jobs", make_jobs)])

    count = jobs_plugins.load_entrypoint_jobs()

    assert count == 2
    assert jobs_registry.resolve_job("job_a").name == "job_a"
    assert jobs_registry.resolve_job("job_b").name == "job_b"


# ---------------------------------------------------------------------------
# Graceful no-op when there are no entry-points.
# ---------------------------------------------------------------------------


def test_no_entry_points_is_a_noop(monkeypatch) -> None:
    _patch_entry_points(monkeypatch, [])

    count = jobs_plugins.load_entrypoint_jobs()

    assert count == 0
    assert jobs_registry.get_job_registry() == {}


# ---------------------------------------------------------------------------
# A bad provider is skipped with a warning and doesn't block a valid sibling.
# ---------------------------------------------------------------------------


def test_load_failure_is_skipped_and_does_not_block_siblings(monkeypatch, caplog) -> None:
    def good_factory():
        return _StubJob("good_one")

    bad = _FakeEntryPoint("broken", load_exc=ImportError("no module named whatever"))
    good = _FakeEntryPoint("good", good_factory)
    _patch_entry_points(monkeypatch, [bad, good])

    with caplog.at_level(logging.WARNING, logger=jobs_plugins.__name__):
        count = jobs_plugins.load_entrypoint_jobs()

    # The good sibling still registered despite the broken one.
    assert count == 1
    assert jobs_registry.resolve_job("good_one").name == "good_one"
    assert any("failed to load" in rec.message for rec in caplog.records)


def test_factory_that_raises_is_skipped_and_does_not_block_siblings(monkeypatch, caplog) -> None:
    def boom_factory():
        raise RuntimeError("factory blew up")

    def good_factory():
        return _StubJob("survivor")

    boom = _FakeEntryPoint("boomer", boom_factory)
    good = _FakeEntryPoint("good", good_factory)
    _patch_entry_points(monkeypatch, [boom, good])

    with caplog.at_level(logging.WARNING, logger=jobs_plugins.__name__):
        count = jobs_plugins.load_entrypoint_jobs()

    assert count == 1
    assert jobs_registry.resolve_job("survivor").name == "survivor"
    assert any("raised on call" in rec.message for rec in caplog.records)


def test_non_jobcallable_is_skipped_and_does_not_block_siblings(monkeypatch, caplog) -> None:
    def bad_factory():
        return _NotAJob()  # no name / no __call__ → not a JobCallable

    def good_factory():
        return _StubJob("also_survives")

    bad = _FakeEntryPoint("bad_shape", bad_factory)
    good = _FakeEntryPoint("good", good_factory)
    _patch_entry_points(monkeypatch, [bad, good])

    with caplog.at_level(logging.WARNING, logger=jobs_plugins.__name__):
        count = jobs_plugins.load_entrypoint_jobs()

    assert count == 1
    assert jobs_registry.resolve_job("also_survives").name == "also_survives"
    with pytest.raises(UnknownJobError):
        jobs_registry.resolve_job("_NotAJob")
    assert any("not a JobCallable" in rec.message for rec in caplog.records)


def test_mixed_list_skips_only_the_bad_member(monkeypatch) -> None:
    """A factory returning [good, bad] registers the good one, skips the bad."""

    def make_jobs():
        return [_StubJob("kept"), _NotAJob()]

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("mixed", make_jobs)])

    count = jobs_plugins.load_entrypoint_jobs()

    assert count == 1
    assert jobs_registry.resolve_job("kept").name == "kept"


# ---------------------------------------------------------------------------
# Idempotent — calling twice re-registers the same name (last-writer-wins).
# ---------------------------------------------------------------------------


def test_idempotent_reload_overwrites_same_name(monkeypatch) -> None:
    def make_jobs():
        return _StubJob("dupe")

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("my_jobs", make_jobs)])

    first = jobs_plugins.load_entrypoint_jobs()
    second = jobs_plugins.load_entrypoint_jobs()

    assert first == 1
    assert second == 1  # re-registers, no error, no duplicate growth
    assert len(jobs_registry.get_job_registry()) == 1
    assert jobs_registry.resolve_job("dupe").name == "dupe"
