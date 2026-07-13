# tests/ee/sites/test_leaf_edits.py — the native-editing leaf-edit persist path
# (NE-4b). Created 2026-07-01 (feat/native-editing-ne4b).
# Updated 2026-07-02 (harden the persist path): + a bridge RuntimeError and a
# malformed CLI result (missing ``results``) both map to a clean CloudError
# (sites.leaf_edit_failed), not a 500; + a DYNAMIC pocket splits its binding keys so
# the CLI gets ONLY files and the persist loop is confined to the input file keyspace
# (a binding key / invented path is never written back); + a json.dump serialization
# failure in the bridge cleans up its tempfile (no leak, no NameError).
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
from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError
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

# A DYNAMIC svelte pocket (DSV-5): the live-data bindings (objects/sources/actions/
# auth) ride as SIBLING keys of the {path: contents} SvelteKit files on the SAME
# source envelope. apply_leaf_edits must split these out before the CLI splice.
_DYNAMIC_OBJECTS = [{"name": "signups", "columns": [{"name": "email", "type": "text"}]}]
_DYNAMIC_SOURCE = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte'</script><Hero/>"
    ),
    "src/lib/components/Hero.svelte": _HERO_V1,
    "objects": _DYNAMIC_OBJECTS,
    "sources": [{"object": "signups", "as": "signups"}],
    "actions": [{"object": "signups", "op": "insert"}],
    "auth": False,
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


# ---------------------------------------------------------------------------
# hardening — graceful CloudError envelopes, dynamic binding-key safety,
# and the bridge temp-file cleanup (2026-07-02)
# ---------------------------------------------------------------------------


async def _make_dynamic_svelte_pocket(workspace_id: str, user_id: str) -> str:
    """Persist a real DYNAMIC svelte-engine Pocket (source carries the DSV-5 binding
    keys as siblings of the files) and return its id."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=workspace_id,
        owner_id=user_id,
        name="Signups",
        type_="site",
        pattern="dynamic",
        ripple_spec=None,
        engine="svelte",
        source=dict(_DYNAMIC_SOURCE),
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None
    return pocket_id


@pytest.mark.asyncio
async def test_apply_leaf_edits_bridge_runtime_error_maps_to_cloud_error(beanie_test_db):
    """The bridge raises a bare RuntimeError on a non-zero CLI exit / timed-out splice.
    The service maps it to a clean CloudError (sites.leaf_edit_failed) so the client
    gets a structured envelope — NOT an opaque, unhandled 500."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    async def _fake_apply(*, source, edits):
        raise RuntimeError("apply-leaf-edit failed: unparseable op")

    with pytest.raises(CloudError) as excinfo:
        await sites_service.apply_leaf_edits(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            edits=[{"uid": "Hero:headline:0", "op": {"kind": "setText", "html": "x"}}],
            _apply=_fake_apply,
        )

    assert excinfo.value.code == "sites.leaf_edit_failed"
    assert excinfo.value.status_code >= 500
    # The raw RuntimeError did not escape — the mapped CloudError did.
    assert not isinstance(excinfo.value, RuntimeError)


@pytest.mark.asyncio
async def test_apply_leaf_edits_malformed_result_maps_to_cloud_error(beanie_test_db):
    """A CLI result missing the ``results`` key (malformed output) would KeyError on
    the parse. The service maps that to a CloudError too — a structured envelope, not
    a 500."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    async def _fake_apply(*, source, edits):
        # ``source`` present but ``results`` missing → KeyError on the parse.
        return {"source": dict(source)}

    with pytest.raises(CloudError) as excinfo:
        await sites_service.apply_leaf_edits(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            edits=[{"uid": "Hero:headline:0", "op": {"kind": "setText", "html": "x"}}],
            _apply=_fake_apply,
        )

    assert excinfo.value.code == "sites.leaf_edit_failed"


@pytest.mark.asyncio
async def test_apply_leaf_edits_dynamic_pocket_splits_bindings_and_confines_persist(
    beanie_test_db, monkeypatch
):
    """A DYNAMIC svelte pocket carries binding keys (objects/sources/actions/auth) as
    siblings of the files. apply_leaf_edits must (a) send ONLY the file map to the CLI
    (never a binding key), and (b) confine the persist loop to the input file keyspace
    — so a binding key echoed back CHANGED, or a brand-new path the CLI invents, is
    NEVER written via set_svelte_source_file (which would corrupt a live-data binding
    or 404 on the unknown path)."""
    pocket_id = await _make_dynamic_svelte_pocket("ws1", "u1")

    captured: dict = {}

    async def _fake_apply(*, source, edits):
        captured["source"] = source
        new = dict(source)
        new["src/lib/components/Hero.svelte"] = _HERO_V2  # a real file changed
        # The CLI misbehaves: echoes a binding key back CHANGED and invents a new path
        # — neither may be persisted as a component file.
        new["objects"] = "CORRUPTED-BINDING"
        new["src/lib/components/Invented.svelte"] = "<section>invented</section>"
        return {"source": new, "results": [{"uid": edits[0]["uid"], "applied": True}]}

    calls = _spy_set_svelte_source_file(monkeypatch)

    results = await sites_service.apply_leaf_edits(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        edits=[{"uid": "Hero:headline:0", "op": {"kind": "setText", "html": "Brighter"}}],
        _apply=_fake_apply,
    )

    # (a) The CLI received ONLY the file map — no binding keys mixed in.
    assert set(captured["source"].keys()) == {
        "src/routes/+page.svelte",
        "src/lib/components/Hero.svelte",
    }
    for binding_key in ("objects", "sources", "actions", "auth"):
        assert binding_key not in captured["source"]

    # (b) Persist was CONFINED to the input file keyspace: only the one real changed
    # file — never the echoed binding key, never the invented path.
    assert calls == ["src/lib/components/Hero.svelte"]

    # Verdict passes through; the changed file landed; the live-data binding is intact
    # (NOT overwritten with the CLI's "CORRUPTED-BINDING"); the invented path was not
    # created.
    assert results == [{"uid": "Hero:headline:0", "applied": True}]
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V2
    assert wire["source"]["objects"] == _DYNAMIC_OBJECTS
    assert "src/lib/components/Invented.svelte" not in wire["source"]


@pytest.mark.asyncio
async def test_generator_bridge_cleans_temp_on_dump_failure(monkeypatch):
    """Temp-file leak fix: if ``json.dump`` fails mid-write (a non-serializable
    source), the created tempfile is STILL cleaned up — ``input_path`` is assigned
    BEFORE the write and the guarded finally unlinks it. No NameError, no leak."""
    monkeypatch.setenv("PAW_SITES_GEN_CMD", "paw-sites-gen")

    created: list[str] = []
    real_ntf = generator_client.tempfile.NamedTemporaryFile

    def _spy_ntf(*args, **kwargs):
        fh = real_ntf(*args, **kwargs)
        created.append(fh.name)
        return fh

    monkeypatch.setattr(generator_client.tempfile, "NamedTemporaryFile", _spy_ntf)

    class _Unserializable:
        pass

    # json.dump can't serialize _Unserializable → raises TypeError mid-write, AFTER
    # NamedTemporaryFile has already materialized the (delete=False) tempfile on disk.
    with pytest.raises(TypeError):
        await generator_client.apply_leaf_edits(
            source={"a.svelte": _Unserializable()},
            edits=[{"uid": "a:0", "op": {"kind": "setText", "html": "y"}}],
            _exec=_fake_exec(_FakeProc(returncode=0)),
        )

    assert created, "expected a temp file to be created"
    for path in created:
        assert not Path(path).exists(), f"temp file leaked after dump failure: {path}"
