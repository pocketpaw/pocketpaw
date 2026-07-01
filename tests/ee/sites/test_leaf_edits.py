# tests/ee/sites/test_leaf_edits.py — the native-editing leaf-edit persist path
# (NE-4b). Created 2026-07-01 (feat/native-editing-ne4b).
#
# Two layers under test, both hermetic (no Bun / workerd — the external CLI is
# faked at its seam):
#   * generator_client.apply_leaf_edits — the Python bridge to the paw-sites
#     apply-leaf-edit CLI. A fake ``_exec`` returns a stub proc with a canned
#     ``communicate()`` so we assert (a) it invokes the tokenised generator command
#     + the apply-leaf-edit subcommand, writes the {source, edits} input file, and
#     parses the single JSON stdout line; and (b) a non-zero exit raises RuntimeError.
#   * sites_service.apply_leaf_edits — the orchestration. These use the shared
#     ``beanie_test_db`` (in-memory Mongo) so the pockets service persists a REAL
#     svelte Pocket and writes a REAL Branch draft; only the CLI bridge (``_apply``)
#     is faked. They prove: a changed file is persisted exactly once and lands a
#     reviewable draft; a rejected edit (source unchanged) persists nothing and
#     surfaces the reason; a non-svelte pocket and an empty batch raise
#     ValidationError before the bridge ever runs.

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import generator_client
from pocketpaw_ee.sites import service as sites_service

_HERO_V1 = "<section class='hero'><h1>Bright Smile</h1></section>"
_HERO_V2 = "<section class='hero'><h1>Brighter Smiles, Whiter Teeth</h1></section>"
_SVELTE_SOURCE = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte'</script><Hero/>"
    ),
    "src/lib/components/Hero.svelte": _HERO_V1,
    "src/app.css": ":root{--brand:#0A84FF}",
}


class _FakeProc:
    """Stub subprocess: a canned ``communicate()`` + ``returncode`` so the bridge is
    exercised without spawning Bun. ``pid`` is only touched on the timeout path,
    which these tests do not drive."""

    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = 4321

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _fake_exec(proc: _FakeProc, record: dict | None = None):
    """Build a fake ``asyncio.create_subprocess_exec`` seam that returns ``proc`` and
    (optionally) records the argv + the parsed ``--input`` file so a test can assert
    the CLI invocation and input contract."""

    async def _exec(*args, **kwargs):
        if record is not None:
            record["argv"] = args
            record["kwargs"] = kwargs
            path = args[args.index("--input") + 1]
            record["input"] = json.loads(Path(path).read_text())
        return proc

    return _exec


# ---------------------------------------------------------------------------
# generator bridge (generator_client.apply_leaf_edits)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generator_bridge_parses_cli_output(monkeypatch):
    """The bridge shells out to the tokenised generator command + the apply-leaf-edit
    subcommand, writes the {source, edits} input file, and returns the parsed single
    JSON stdout line."""
    monkeypatch.setenv("PAW_SITES_GEN_CMD", "paw-sites-gen")
    out = {
        "source": {"src/lib/components/Hero.svelte": _HERO_V2},
        "results": [{"uid": "Hero:headline:0", "applied": True}],
    }
    proc = _FakeProc(stdout=(json.dumps(out) + "\n").encode(), returncode=0)
    record: dict = {}

    result = await generator_client.apply_leaf_edits(
        source={"src/lib/components/Hero.svelte": _HERO_V1},
        edits=[{"uid": "Hero:headline:0", "op": {"kind": "setText", "html": "Brighter"}}],
        _exec=_fake_exec(proc, record),
    )

    assert result == out
    # Invoked "<gen cmd> apply-leaf-edit --input <path>" with its own process group.
    assert record["argv"][:3] == ("paw-sites-gen", "apply-leaf-edit", "--input")
    assert record["kwargs"]["start_new_session"] is True
    # The input file carried the {source, edits} contract.
    assert record["input"]["source"] == {"src/lib/components/Hero.svelte": _HERO_V1}
    assert record["input"]["edits"][0]["uid"] == "Hero:headline:0"


@pytest.mark.asyncio
async def test_generator_bridge_nonzero_exit_raises(monkeypatch):
    """A non-zero CLI exit is a failed splice → RuntimeError carrying the stderr."""
    monkeypatch.setenv("PAW_SITES_GEN_CMD", "paw-sites-gen")
    proc = _FakeProc(stderr=b"apply-leaf-edit: unparseable op", returncode=1)

    with pytest.raises(RuntimeError, match="apply-leaf-edit failed"):
        await generator_client.apply_leaf_edits(
            source={"a.svelte": "x"},
            edits=[{"uid": "a:0", "op": {"kind": "setText", "html": "y"}}],
            _exec=_fake_exec(proc),
        )


# ---------------------------------------------------------------------------
# service orchestration (sites_service.apply_leaf_edits) — real DB, faked CLI
# ---------------------------------------------------------------------------


async def _make_svelte_pocket(workspace_id: str, user_id: str) -> str:
    """Persist a real svelte-engine Pocket via the pockets service and return its id
    (mirrors how create_svelte_site lands a pocket)."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=workspace_id,
        owner_id=user_id,
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="svelte",
        source=dict(_SVELTE_SOURCE),
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None
    return pocket_id


def _spy_set_svelte_source_file(monkeypatch) -> list[str]:
    """Wrap pockets_service.set_svelte_source_file with a spy that records the
    component_path of every call AND still performs the real persist (so the DB +
    Branch draft actually update). Returns the recording list."""
    calls: list[str] = []
    real = pockets_service.set_svelte_source_file

    async def _spy(*args, **kwargs):
        calls.append(kwargs.get("component_path"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(pockets_service, "set_svelte_source_file", _spy)
    return calls


@pytest.mark.asyncio
async def test_apply_leaf_edits_persists_changed_file_as_draft(beanie_test_db, monkeypatch):
    """A landed edit persists ONLY the changed file (once) and leaves a reviewable
    Branch draft snapshotting the edited source. The verdict passes straight
    through. The CLI bridge is faked — no Bun runs."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    async def _fake_apply(*, source, edits):
        new = dict(source)
        new["src/lib/components/Hero.svelte"] = _HERO_V2
        return {"source": new, "results": [{"uid": edits[0]["uid"], "applied": True}]}

    calls = _spy_set_svelte_source_file(monkeypatch)

    results = await sites_service.apply_leaf_edits(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        edits=[{"uid": "Hero:headline:0", "op": {"kind": "setText", "html": "Brighter Smiles"}}],
        _apply=_fake_apply,
    )

    # Verdict passes through unchanged.
    assert results == [{"uid": "Hero:headline:0", "applied": True}]
    # Persisted EXACTLY the one changed file — untouched siblings are skipped.
    assert calls == ["src/lib/components/Hero.svelte"]
    # The edit landed on the pocket (real persist; a re-read shows the new source,
    # siblings verbatim).
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V2
    assert wire["source"]["src/app.css"] == ":root{--brand:#0A84FF}"
    # And it left a reviewable Branch draft of the full edited map.
    from pocketpaw_ee.versions import service as versions

    draft = await versions.get_draft(scope_type="pocket", scope_id=pocket_id)
    assert draft is not None
    assert draft.content["src/lib/components/Hero.svelte"] == _HERO_V2


@pytest.mark.asyncio
async def test_apply_leaf_edits_rejected_edit_persists_nothing(beanie_test_db, monkeypatch):
    """A rejected edit comes back with the source byte-identical + applied:false +
    reason. Nothing changed → set_svelte_source_file is never called and the pocket
    source is untouched; the reason is surfaced to the caller."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    async def _fake_apply(*, source, edits):
        # Source returned UNCHANGED (the whole-file re-author stays with the caller).
        return {
            "source": dict(source),
            "results": [{"uid": edits[0]["uid"], "applied": False, "reason": "no unique match"}],
        }

    calls = _spy_set_svelte_source_file(monkeypatch)

    results = await sites_service.apply_leaf_edits(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        edits=[{"uid": "Hero:headline:0", "op": {"kind": "setText", "html": "x"}}],
        _apply=_fake_apply,
    )

    assert calls == []  # nothing changed → nothing persisted
    assert results == [{"uid": "Hero:headline:0", "applied": False, "reason": "no unique match"}]
    # The pocket source is untouched.
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V1


@pytest.mark.asyncio
async def test_apply_leaf_edits_non_svelte_pocket_rejected(beanie_test_db):
    """A ripple-engine pocket has no svelte source map — apply_leaf_edits raises
    ValidationError (422) BEFORE the bridge runs."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Ripple Pocket",
        type_="site",
        pattern="landing",
        ripple_spec={"type": "container"},
    )
    assert err is None, err

    async def _fake_apply(*, source, edits):  # pragma: no cover - must not be reached
        raise AssertionError("the bridge must not run for a non-svelte pocket")

    with pytest.raises(ValidationError):
        await sites_service.apply_leaf_edits(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            edits=[{"uid": "x:0", "op": {"kind": "setText", "html": "y"}}],
            _apply=_fake_apply,
        )


@pytest.mark.asyncio
async def test_apply_leaf_edits_empty_batch_rejected():
    """An empty edit batch raises ValidationError (422) before any pocket read or
    bridge call — so it needs no DB and the bridge must never run."""

    async def _fake_apply(*, source, edits):  # pragma: no cover - must not be reached
        raise AssertionError("the bridge must not run for an empty batch")

    with pytest.raises(ValidationError):
        await sites_service.apply_leaf_edits(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="0123456789abcdef01234567",
            edits=[],
            _apply=_fake_apply,
        )
