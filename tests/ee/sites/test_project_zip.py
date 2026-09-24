# tests/ee/sites/test_project_zip.py — the downloadable project archive.
#
# Updated 2026-09-24 (feat/sites-author-dependencies, PP-1): ``src/routes/+layout.ts``
# is now generator-reserved on svelte (contract §2), so the "authored file beats the
# shell default" test uses ``src/app.html`` alone and a sibling pins that the shell's
# +layout.ts now wins. The pin-drift check reads ``vetted_pins.FALLBACK_PINS`` (the
# constants the loader falls back to), and author packages from
# ``paw.dependencies.json`` are checked to reach package.json at exact versions.
#
# What these pin, in the order the module's own invariants matter:
#
#   * the archive is assembled from the STORED pocket, so the capture placeholders
#     are resolved on the way out and the caller's source dict is never mutated;
#   * ripple (and the empty / unknown engine that normalizes to it) gets a DEFINED
#     no-op rather than an exception or an empty archive that looks like success;
#   * a dynamic svelte envelope's binding keys never become files;
#   * every dependency the shell names is a vetted pin, and an authored manifest
#     cannot displace it — nothing downstream re-runs paw-sites' ``assertAllowed``;
#   * both caps refuse, and every archive path is relative and inside the root.
#
# The dependency test also checks the Python pins against paw-sites'
# ``VETTED_DEPENDENCIES`` when that repo is next to this one, and skips with a reason
# when it is not: the mirror is the drift risk this module knowingly takes, so the
# check runs wherever it can rather than nowhere.

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pocketpaw_ee.sites import project_zip

_BASE = "https://capture.example/api/v1"
_KEY = "site_key_abcdefghijklmnop"
_SITE_ID = "68c0ffee0000000000000001"

_FORM = (
    '<form method="POST" action="__CAPTURE_API_BASE__/capture/form">'
    '<input type="hidden" name="paw_site_id" value="__SITE_ID__">'
    '<input type="hidden" name="paw_key" value="__CAPTURE_SIGNED_KEY__">'
    "</form>"
)


def _svelte_source() -> dict[str, object]:
    return {
        "src/routes/+page.svelte": "<script>let n = 1;</script>\n<h1>Hello</h1>\n" + _FORM,
        "src/routes/+layout.svelte": "<script>import '../app.css';</script>\n<slot />",
        "src/routes/+page.ts": "export const prerender = true;\n",
        "src/app.css": ":root { --ink: #111; }\n",
        "src/lib/components/Hero.svelte": "<section>hero</section>\n",
    }


def _react_source() -> dict[str, object]:
    return {
        "src/App.tsx": "export default function App() { return <main>hi</main>; }\n",
        "src/app.css": ":root { --ink: #111; }\n",
    }


def _html_source() -> dict[str, object]:
    return {
        "index.html": "<!doctype html><title>Site</title>" + _FORM,
        "styles.css": "body { margin: 0; }\n",
    }


async def _archive(engine: str, source: dict[str, object], **over: object):
    """Assemble and return ``(ProjectZip, {path: text})`` for one engine."""
    kwargs: dict[str, object] = {
        "engine": engine,
        "source": source,
        "site_id": _SITE_ID,
        "title": "Aster Dental",
        "capture_api_base": _BASE,
        "capture_signed_key": _KEY,
        "archive_name": _SITE_ID,
    }
    kwargs.update(over)
    built = await project_zip.build_project_zip_from_source(**kwargs)  # type: ignore[arg-type]
    assert built is not None
    with zipfile.ZipFile(io.BytesIO(built.data)) as zf:
        entries = {name: zf.read(name).decode("utf-8") for name in zf.namelist()}
    return built, entries


def _named_dependencies(entries: dict[str, str]) -> dict[str, str]:
    """Every dependency the archive's manifest names, dev and runtime together."""
    manifest = json.loads(entries["package.json"])
    return {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}


# ── criterion 1: only vetted dependencies reach a downloaded project ────────────


@pytest.mark.asyncio
async def test_a_svelte_project_names_only_vetted_dependencies():
    _built, entries = await _archive("svelte", _svelte_source())
    named = _named_dependencies(entries)
    # The trap is armed: the manifest names a real toolchain, not nothing.
    assert "@sveltejs/kit" in named
    assert "svelte" in named
    for name, pin in named.items():
        assert name in project_zip._VETTED_PINS, name
        assert pin == project_zip._VETTED_PINS[name]


@pytest.mark.asyncio
async def test_a_react_project_names_only_vetted_dependencies():
    _built, entries = await _archive("react", _react_source())
    named = _named_dependencies(entries)
    assert named["react"] == "^19.0.0"
    assert "@vitejs/plugin-react" in named
    for name, pin in named.items():
        assert name in project_zip._VETTED_PINS, name
        assert pin == project_zip._VETTED_PINS[name]


@pytest.mark.asyncio
async def test_an_html_project_ships_no_manifest_because_it_has_no_build():
    """html's source IS the served site, so the archive invents no build step.

    The vetted-dependency invariant holds here by there being no manifest to hold
    it over, which is stated rather than left as an absent assertion.
    """
    _built, entries = await _archive("html", _html_source())
    assert "package.json" not in entries
    assert not any(name.endswith("vite.config.ts") for name in entries)
    # The authored tree is all there, plus the note.
    assert entries["index.html"].startswith("<!doctype html>")
    assert entries["styles.css"] == "body { margin: 0; }\n"
    assert "README.md" in entries


def test_every_shell_dependency_is_a_vetted_pin():
    for engine, (deps, dev_deps) in project_zip._SHELL_DEPENDENCIES.items():
        for name in (*deps, *dev_deps):
            assert name in project_zip._VETTED_PINS, f"{engine} names unvetted {name}"


def test_the_vetted_pins_agree_with_paw_sites_allowlist():
    """Cross-repo check of the mirror, when paw-sites is checked out alongside."""
    here = Path(__file__).resolve()
    allowlist = next(
        (
            candidate
            for parent in here.parents
            if (candidate := parent / "paw-sites" / "src" / "allowlist.ts").is_file()
        ),
        None,
    )
    if allowlist is None:
        pytest.skip("paw-sites is not checked out beside this repo")
    text = allowlist.read_text(encoding="utf-8")
    body = text.split("VETTED_DEPENDENCIES", 1)[1].split("export function", 1)[0]
    pairs = dict(re.findall(r"""['"]?([@a-z0-9/._-]+)['"]?:\s*['"]([^'"]+)['"]""", body))
    assert pairs, "could not parse VETTED_DEPENDENCIES out of allowlist.ts"
    from pocketpaw_ee.sites import vetted_pins

    # The FALLBACK constants are what the loader serves when no vendored allowlist
    # is present, so they are the copy that can silently rot.
    for name, pin in vetted_pins.FALLBACK_PINS.items():
        assert name in pairs, f"{name} is no longer on the paw-sites allowlist"
        assert pairs[name] == pin, f"{name} pin drifted: ours {pin}, theirs {pairs[name]}"


# ── criterion 2: a binding key never becomes a file ────────────────────────────


@pytest.mark.asyncio
async def test_a_dynamic_svelte_project_holds_no_file_named_for_a_binding_key():
    source = _svelte_source()
    source["objects"] = [{"name": "bookings", "columns": []}]
    source["sources"] = [{"kind": "data", "object": "bookings"}]
    source["actions"] = [{"kind": "create", "object": "bookings"}]
    source["auth"] = True
    # An authored file whose name merely CONTAINS a binding key proves the name
    # comparison below would find a bare one if it were there.
    source["src/lib/objects.ts"] = "export const objects = [];\n"

    _built, entries = await _archive("svelte", source)

    assert "src/lib/objects.ts" in entries
    for key in ("objects", "sources", "actions", "auth"):
        assert key not in entries


# ── criterion 3: the defined no-op ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_ripple_pocket_returns_the_no_op_rather_than_raising():
    built = await project_zip.build_project_zip_from_source(
        engine="ripple",
        source=None,
        site_id=_SITE_ID,
        title="Aster Dental",
        capture_api_base=_BASE,
        capture_signed_key=_KEY,
        archive_name=_SITE_ID,
    )
    assert built is None


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", [None, "", "  ", "svelt", "vue", "RIPPLE"])
async def test_an_empty_or_unknown_engine_returns_the_same_no_op(engine):
    """Everything that normalizes to ripple gets ripple's answer, not an error."""
    built = await project_zip.build_project_zip_from_source(
        engine=engine,
        source=_svelte_source(),
        site_id=_SITE_ID,
        title="Aster Dental",
        capture_api_base=_BASE,
        capture_signed_key=_KEY,
        archive_name=_SITE_ID,
    )
    assert built is None


def test_has_downloadable_project_answers_for_each_engine():
    assert project_zip.has_downloadable_project("svelte") is True
    assert project_zip.has_downloadable_project("react") is True
    assert project_zip.has_downloadable_project("html") is True
    assert project_zip.has_downloadable_project("ripple") is False
    assert project_zip.has_downloadable_project(None) is False
    assert project_zip.has_downloadable_project("nonsense") is False


# ── criterion 4: both caps refuse ──────────────────────────────────────────────


def _plan(source: dict[str, object], engine: str = "html") -> dict[str, str]:
    return project_zip.plan_project_files(
        engine=engine,
        source=source,
        site_id=_SITE_ID,
        title="Aster Dental",
        capture_api_base=_BASE,
        capture_signed_key=_KEY,
    )


def test_the_file_cap_refuses_a_map_with_more_entries_than_it_allows():
    under = {f"page-{i}.html": "x" for i in range(project_zip.MAX_FILES - 2)}
    under["index.html"] = "<!doctype html>"
    # Control: one under the cap plans fine, so the refusal below is the cap and
    # not some unrelated breakage in a map this shape.
    assert len(_plan(dict(under))) <= project_zip.MAX_FILES

    over = {f"page-{i}.html": "x" for i in range(project_zip.MAX_FILES + 1)}
    with pytest.raises(project_zip.ProjectZipTooLarge) as excinfo:
        _plan(over)
    assert "files" in str(excinfo.value)


def test_the_byte_cap_refuses_a_map_over_the_total():
    under = {"index.html": "x" * 1024}
    assert _plan(dict(under))  # control: a small map plans fine

    over = {"index.html": "x" * (project_zip.MAX_TOTAL_BYTES + 1)}
    with pytest.raises(project_zip.ProjectZipTooLarge) as excinfo:
        _plan(over)
    assert "bytes" in str(excinfo.value)


def test_the_byte_cap_counts_encoded_bytes_not_characters():
    """A multi-byte character costs what it costs on disk, not one per char."""
    wide = "\u00e9" * (project_zip.MAX_TOTAL_BYTES // 2 + 1)  # 2 bytes each in utf-8
    assert len(wide) <= project_zip.MAX_TOTAL_BYTES
    with pytest.raises(project_zip.ProjectZipTooLarge):
        _plan({"index.html": wide})


# ── criterion 5: every archive path is safe ────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "/index.html",
        "../outside.html",
        "src/../../outside.html",
        "..",
        "C:/Windows/system.ini",
        "c:nope.html",
        "..\\outside.html",
        "src\\..\\..\\outside.html",
        "   ",
        "",
        ".",
    ],
)
def test_an_unsafe_source_path_is_refused(bad):
    with pytest.raises(project_zip.UnsafeSourcePath):
        _plan({bad: "<!doctype html>", "index.html": "<!doctype html>"})


@pytest.mark.asyncio
async def test_every_archive_path_is_relative_and_inside_the_root():
    source = _html_source()
    source["assets/nested/deep/thing.css"] = "a{}"
    source["./index.css"] = "b{}"
    source["assets//double.css"] = "c{}"
    _built, entries = await _archive("html", source)

    # The redundant "./" and "//" are normalized away rather than refused; only a
    # path that leaves the root is refused (see the parametrized test above).
    assert "index.css" in entries
    assert "assets/double.css" in entries
    assert "assets/nested/deep/thing.css" in entries
    for name in entries:
        assert not name.startswith("/")
        assert not name.startswith("\\")
        assert ".." not in name.split("/")
        assert not re.match(r"^[A-Za-z]:", name)
        assert "\\" not in name


def test_a_nested_traversal_inside_a_longer_path_is_refused():
    """The dangerous segment is not always at the front."""
    with pytest.raises(project_zip.UnsafeSourcePath):
        _plan({"src/lib/../../../../etc/passwd": "x"})


# ── the stored pocket is the source, and it stays unmodified ────────────────────


@pytest.mark.asyncio
async def test_the_capture_placeholders_are_resolved_in_the_archive():
    _built, entries = await _archive("html", _html_source())
    page = entries["index.html"]
    assert f'action="{_BASE}/capture/form"' in page
    assert f'value="{_SITE_ID}"' in page
    assert f'value="{_KEY}"' in page
    assert "__CAPTURE_API_BASE__" not in page
    assert "__SITE_ID__" not in page
    assert "__CAPTURE_SIGNED_KEY__" not in page


@pytest.mark.asyncio
async def test_the_stored_source_map_is_never_mutated():
    source = _html_source()
    before = dict(source)
    await _archive("html", source)
    assert source == before
    assert "__CAPTURE_SIGNED_KEY__" in str(source["index.html"])


@pytest.mark.asyncio
async def test_a_site_with_no_key_yet_keeps_the_placeholders():
    """An unpublished draft has no capture key; the archive says so rather than
    resolving the tokens to empty strings and shipping a form that posts nowhere."""
    _built, entries = await _archive("html", _html_source(), site_id="", capture_signed_key="")
    assert "__CAPTURE_SIGNED_KEY__" in entries["index.html"]
    assert "capture key yet" in entries["README.md"]


# ── the shell wins, and a malformed envelope is refused ────────────────────────


@pytest.mark.asyncio
async def test_an_authored_manifest_cannot_displace_the_vetted_shell():
    source = _svelte_source()
    source["package.json"] = json.dumps({"dependencies": {"axios": "1.0.0"}})
    source["src/lib/components/Hero.svelte"] = "<section>authored hero</section>\n"

    _built, entries = await _archive("svelte", source)

    # Control: the author's other file DID land, so the absence below is the shell
    # winning one path and not the overlay failing wholesale.
    assert entries["src/lib/components/Hero.svelte"] == "<section>authored hero</section>\n"
    assert "axios" not in entries["package.json"]
    assert "@sveltejs/kit" in _named_dependencies(entries)


@pytest.mark.asyncio
async def test_an_authored_file_in_the_authorable_tree_beats_the_shell_default():
    """The split the other way round: the generator lets a map override these, so
    the archive must too, or a download silently discards authored work."""
    source = _svelte_source()
    source["src/app.html"] = "<!doctype html><body>%sveltekit.body%</body>"

    _built, entries = await _archive("svelte", source)

    assert entries["src/app.html"] == "<!doctype html><body>%sveltekit.body%</body>"
    # Control: the reserved half of the same shell still won, so this is the split
    # and not the shell failing to apply at all.
    assert "@sveltejs/kit" in _named_dependencies(entries)


@pytest.mark.asyncio
async def test_the_shell_layout_ts_beats_an_authored_one_now_that_it_is_reserved():
    """PP-1: ``src/routes/+layout.ts`` joined the generator's reserved svelte shell
    (contract §2), because an authored one could turn prerendering off. The download
    follows the generator: the shell copy wins. Breaks if the path is dropped from
    ``svelte_paths.SVELTE_RESERVED_SHELL_FILES``."""
    source = _svelte_source()
    source["src/routes/+layout.ts"] = "export const prerender = false;\n"

    _built, entries = await _archive("svelte", source)

    assert "prerender = true" in entries["src/routes/+layout.ts"]


def test_every_overriding_shell_path_is_one_the_generator_reserves():
    """The shell may only outrank an author at a path paw-sites also reserves.

    Anything else and the archive would be enforcing a rule the generator does not,
    which is how the two drift into disagreeing about who owns a file.
    """
    for engine in ("svelte", "react"):
        shell = project_zip._build_shell(
            engine, site_id=_SITE_ID, title="Aster Dental", tokens_resolved=True
        )
        reserved = [p for p in shell if project_zip._is_reserved_path(engine, p)]
        assert "package.json" in reserved, engine
        for path in reserved:
            assert project_zip._is_reserved_path(engine, path)


def test_the_svelte_shell_supplies_what_adapter_static_cannot_build_without():
    shell = project_zip._build_shell(
        "svelte", site_id=_SITE_ID, title="Aster Dental", tokens_resolved=True
    )
    assert "prerender = true" in shell["src/routes/+layout.ts"]
    assert "src/routes/thank-you/+page.svelte" in shell


def test_an_unknown_non_string_entry_is_refused():
    """A value the peeler did not recognize as a binding is not file contents."""
    with pytest.raises(project_zip.ProjectZipError) as excinfo:
        _plan({"index.html": "<!doctype html>", "weird": {"a": 1}})
    assert "weird" in str(excinfo.value)


# ── archive mechanics ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_same_pocket_assembles_to_the_same_bytes():
    first, _ = await _archive("svelte", _svelte_source())
    second, _ = await _archive("svelte", _svelte_source())
    assert first.data == second.data


@pytest.mark.asyncio
async def test_the_deflate_runs_off_the_event_loop(monkeypatch):
    """Deflating is CPU-bound, so doing it inline freezes every other request.

    The control is the assertion that the archive still came back: the recorder
    delegates to the real ``to_thread``, so a mutation that stops offloading is the
    only way the count stays at zero.
    """
    import asyncio as asyncio_module

    real = asyncio_module.to_thread
    offloaded: list[str] = []

    async def recording(func, /, *args, **kwargs):
        offloaded.append(getattr(func, "__name__", repr(func)))
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio_module, "to_thread", recording)
    built, entries = await _archive("svelte", _svelte_source())
    assert entries  # the archive was really assembled, not short-circuited
    assert built.file_count > 0
    assert "_write_zip" in offloaded


@pytest.mark.asyncio
async def test_the_result_reports_the_engine_the_file_count_and_a_filename():
    built, entries = await _archive("react", _react_source())
    assert built.engine == "react"
    assert built.file_count == len(entries)
    assert built.filename == f"paw-site-{_SITE_ID}.zip"


@pytest.mark.asyncio
async def test_a_svelte_archive_carries_the_app_shell_the_source_map_never_holds():
    """SvelteKit cannot build without src/app.html, and no authored map ships one."""
    _built, entries = await _archive("svelte", _svelte_source())
    assert "%sveltekit.body%" in entries["src/app.html"]
    assert "adapter-static" in entries["svelte.config.js"]
    assert "Aster Dental" in entries["src/app.html"]


@pytest.mark.asyncio
async def test_a_title_with_markup_is_escaped_into_the_shell():
    _built, entries = await _archive("svelte", _svelte_source(), title='Bob & "Co" <b>')
    assert "&amp;" in entries["src/app.html"]
    assert "<b>" not in entries["src/app.html"]


@pytest.mark.asyncio
async def test_no_archive_ships_a_lockfile():
    """The generator emits none deliberately; fabricating one would be a lie about
    which versions the published site was built from."""
    for engine, source in (
        ("svelte", _svelte_source()),
        ("react", _react_source()),
        ("html", _html_source()),
    ):
        _built, entries = await _archive(engine, source)
        for lock in ("bun.lock", "bun.lockb", "package-lock.json", "yarn.lock"):
            assert lock not in entries


# ── the pocket-id entry point ──────────────────────────────────────────────────


@pytest.fixture
def patched_reads(monkeypatch):
    """Patch the two reads ``build_project_zip`` makes, and report what it asked."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import service as sites_service

    state: dict[str, object] = {
        "pocket": {"engine": "html", "source": _html_source(), "name": "Pocket name"},
        "site": SimpleNamespace(id=_SITE_ID, signed_key=_KEY, name="Aster Dental"),
        "calls": [],
    }

    async def fake_get(pocket_id, user_id):
        state["calls"].append(("pocket", pocket_id, user_id))
        return state["pocket"]

    async def fake_site(workspace_id, pocket_id):
        state["calls"].append(("site", workspace_id, pocket_id))
        return state["site"]

    monkeypatch.setattr(pockets_service, "get", fake_get)
    monkeypatch.setattr(sites_service, "canonical_site_for_pocket", fake_site)
    monkeypatch.setenv("PAW_CAPTURE_API_BASE", _BASE)
    return state


@pytest.mark.asyncio
async def test_a_pocket_id_assembles_through_the_pockets_service(patched_reads):
    built = await project_zip.build_project_zip(
        workspace_id="ws-1", pocket_id="pkt-1", user_id="user-1"
    )
    assert built is not None
    assert ("pocket", "pkt-1", "user-1") in patched_reads["calls"]
    assert ("site", "ws-1", "pkt-1") in patched_reads["calls"]
    with zipfile.ZipFile(io.BytesIO(built.data)) as zf:
        assert _KEY in zf.read("index.html").decode("utf-8")


@pytest.mark.asyncio
async def test_a_ripple_pocket_id_returns_the_no_op(patched_reads):
    patched_reads["pocket"] = {"engine": "ripple", "rippleSpec": {"widgets": []}}
    built = await project_zip.build_project_zip(
        workspace_id="ws-1", pocket_id="pkt-1", user_id="user-1"
    )
    assert built is None


@pytest.mark.asyncio
async def test_a_source_engine_with_no_source_map_fails_instead_of_coming_back_empty(
    patched_reads,
):
    patched_reads["pocket"] = {"engine": "svelte", "source": {}}
    with pytest.raises(project_zip.ProjectZipError) as excinfo:
        await project_zip.build_project_zip(
            workspace_id="ws-1", pocket_id="pkt-1", user_id="user-1"
        )
    assert "no source map" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_pocket_with_no_site_doc_yet_still_assembles(patched_reads):
    patched_reads["site"] = None
    built = await project_zip.build_project_zip(
        workspace_id="ws-1", pocket_id="pkt-1", user_id="user-1"
    )
    assert built is not None
    assert built.filename == "paw-site-pkt-1.zip"
    with zipfile.ZipFile(io.BytesIO(built.data)) as zf:
        assert "__CAPTURE_SIGNED_KEY__" in zf.read("index.html").decode("utf-8")


# ── PP-1: author packages reach the downloaded package.json ────────────────────


def _manifest(packages: dict[str, dict[str, str]]) -> str:
    from pocketpaw_ee.sites.dependency_manifest import render_manifest

    return render_manifest(packages)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["svelte", "react"])
async def test_author_packages_land_in_package_json_at_exact_versions(engine):
    """Mutation: drop ``author_deps`` from ``_package_json`` → three is missing."""
    source = _svelte_source() if engine == "svelte" else _react_source()
    source["paw.dependencies.json"] = _manifest({"three": {"version": "0.170.0"}})

    _built, entries = await _archive(engine, source)

    manifest = json.loads(entries["package.json"])
    assert manifest["dependencies"]["three"] == "0.170.0"
    # package.json IS the manifest in a node project; the reserved file stays out.
    assert "paw.dependencies.json" not in entries


@pytest.mark.asyncio
async def test_an_author_entry_never_displaces_a_toolchain_pin():
    source = _react_source()
    source["paw.dependencies.json"] = _manifest({"react": {"version": "18.0.0"}})

    _built, entries = await _archive("react", source)

    assert json.loads(entries["package.json"])["dependencies"]["react"] == "^19.0.0"


@pytest.mark.asyncio
async def test_an_html_archive_keeps_the_manifest_and_invents_no_package_json():
    source = _html_source()
    source["paw.dependencies.json"] = _manifest({"three": {"version": "0.170.0"}})

    _built, entries = await _archive("html", source)

    assert "package.json" not in entries
    assert json.loads(entries["paw.dependencies.json"])["packages"]["three"]["version"] == "0.170.0"


def test_vetted_pins_prefer_the_vendored_allowlist(tmp_path, monkeypatch):
    from pocketpaw_ee.sites import vetted_pins

    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps({"authorDeclarable": ["motion"], "pinned": {"motion": "^12.99.0"}}))
    monkeypatch.setenv(vetted_pins.ALLOWLIST_ENV, str(path))
    vetted_pins.vetted_pins.cache_clear()
    try:
        pins = vetted_pins.vetted_pins()
        assert pins["motion"] == "^12.99.0"
        # Names the allowlist does not print fall back to the constants.
        assert pins["svelte"] == vetted_pins.FALLBACK_PINS["svelte"]
    finally:
        vetted_pins.vetted_pins.cache_clear()


def test_the_motion_rewrite_reads_the_shared_pin(monkeypatch):
    from pocketpaw_ee.sites import generator_client, vetted_pins

    monkeypatch.delenv("PAW_SITES_MOTION_DEP", raising=False)
    assert generator_client._ripple_motion_dep() == vetted_pins.pin_for("motion")
