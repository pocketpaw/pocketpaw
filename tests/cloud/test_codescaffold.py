# test_codescaffold.py — prompt -> starter -> source map (CS-1b).
#
# Rewritten 2026-07-22 when recipes became starters.
#
# Three layers:
#
#   * MATCHING and NAMING are pure — no network, no disk.
#   * EXTRACTION runs against tarballs the tests build in memory, so npm's
#     packaging quirks (dotfile smuggling, binary assets, subdirectory
#     selection) are exercised deterministically and offline.
#   * A few LIVE tests really download from npm, behind a flag. They are the
#     only thing that can catch a pinned package whose subdirectory moved, which
#     is the failure this design is most exposed to.
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codescaffold import domain, registry
from pocketpaw_ee.cloud.codescaffold import service as scaffold_service

WS = "ws-1"
USER = "user-1"

live = pytest.mark.skipif(
    os.environ.get("PAW_TEST_LIVE_NPM", "") == "",
    reason="hits registry.npmjs.org; set PAW_TEST_LIVE_NPM=1 to run",
)


# ── Helpers: build a tarball shaped like a real npm package ─────────────────


def _tgz(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def _starter(**overrides) -> domain.Starter:  # noqa: ANN003
    base = {
        "id": "test",
        "label": "Test",
        "summary": "a test starter",
        "package": "create-test",
        "version": "1.0.0",
        "integrity": "sha512-x",
        "subdir": "template-x",
        "dotfile_prefix": "_",
        "keywords": ("test",),
        "dev_port": 5173,
    }
    base.update(overrides)
    return domain.Starter(**base)


# ── The catalog ─────────────────────────────────────────────────────────────


def test_every_starter_pins_a_sha512() -> None:
    """A pinned VERSION without a hash still trusts whatever the network
    returns, and these files are installed and executed in a user's sandbox."""
    for starter in domain.STARTERS:
        assert starter.integrity.startswith("sha512-"), starter.id
        assert len(starter.integrity) > 80, starter.id


def test_starter_ids_are_unique() -> None:
    assert len({s.id for s in domain.STARTERS}) == len(domain.STARTERS)


def test_the_default_starter_exists() -> None:
    assert domain.DEFAULT_STARTER_ID in domain.BY_ID


def test_the_tarball_url_uses_the_pinned_version() -> None:
    """Resolving `/latest` would defeat the pin entirely."""
    url = registry.tarball_url(domain.BY_ID["react"])

    assert url.endswith("/create-vite/-/create-vite-9.1.1.tgz")
    assert "latest" not in url


# ── Matching ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("a react dashboard", "react"),
        ("build me something in Vue", "vue"),
        ("a svelte app", "svelte"),
        ("a Next.js blog", "next"),
        ("a nextjs blog", "next"),
        ("use next js please", "next"),
        ("SvelteKit todo list", "svelte"),
    ],
)
def test_matching_picks_the_named_framework(prompt: str, expected: str) -> None:
    assert domain.match_starter(prompt).starter.id == expected


def test_next_wins_over_react_when_both_are_named() -> None:
    """ "a Next.js app with React" names both, and Next is the more specific
    claim — it already IS React. Matching order encodes that."""
    assert domain.match_starter("a Next.js app using React").starter.id == "next"


def test_an_unmatched_prompt_falls_back_and_says_so() -> None:
    """Guessing beats refusing, but the UI has to be able to tell a guess from a
    match so it can invite the user to change it."""
    match = domain.match_starter("a booking app with sign-in")

    assert match.starter.id == domain.DEFAULT_STARTER_ID
    assert match.matched is False
    assert "default" in match.reason


def test_a_match_carries_the_users_own_words() -> None:
    match = domain.match_starter("a REACT app")

    assert match.matched is True
    assert match.reason == 'you said "react"'


def test_a_framework_name_inside_another_word_does_not_match() -> None:
    """Substring matching finds "vue" in "revue" and "next" in "nextdoor"."""
    assert domain.match_starter("a revue of nextdoor listings").matched is False


# ── Naming ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("a react booking app", "booking"),
        ("Next.js blog for recipes", "blog-recipes"),
        ("build me an app", "new-project"),
        ("", "new-project"),
        ("🎉", "new-project"),
        ("2048 game", "p-2048-game"),
    ],
)
def test_derive_project_name(prompt: str, expected: str) -> None:
    assert domain.derive_project_name(prompt) == expected


def test_a_derived_name_is_always_a_safe_slug() -> None:
    """This becomes a directory name AND an npm package name."""
    for prompt in ["../../etc/passwd", "a" * 500, "!!!", "My App: The Sequel!"]:
        name = domain.derive_project_name(prompt)
        assert name and name.replace("-", "").isalnum() and len(name) <= 40
        assert name[0].isalpha()


# ── Requirements ────────────────────────────────────────────────────────────


def test_no_starter_needs_raw_sockets() -> None:
    """The change the pivot bought. The old Cloudflare/D1 template needed
    `workerd` and could only ever run in a VM; a Vite or Next dev server runs in
    an in-tab WebContainer, so nothing here rules that runtime out."""
    for starter in domain.STARTERS:
        assert domain.requirements_for(starter).rawSockets is False


def test_requirements_never_name_a_runtime() -> None:
    blob = " ".join(domain.requirements_for(domain.BY_ID["react"]).reasons).lower()

    assert "daytona" not in blob
    assert "webcontainer" not in blob


# ── Integrity ───────────────────────────────────────────────────────────────


def test_a_tampered_tarball_is_refused() -> None:
    """The check that makes the pin mean something."""
    data = _tgz({"package/template-x/a.txt": b"hello"})
    starter = _starter(integrity=_integrity(data))

    with pytest.raises(CloudError) as exc:
        registry._verify(b"not the same bytes", starter.integrity, starter)

    assert exc.value.code == "codescaffold.integrity_mismatch"


def test_matching_bytes_pass_verification() -> None:
    data = _tgz({"package/template-x/a.txt": b"hello"})

    registry._verify(data, _integrity(data), _starter())


def test_a_weak_integrity_algorithm_is_refused() -> None:
    """Accepting sha1 because a registry entry used one would make the check
    decorative."""
    with pytest.raises(CloudError) as exc:
        registry._verify(b"x", "sha1-abc", _starter())

    assert exc.value.code == "codescaffold.bad_integrity_pin"


# ── Extraction ──────────────────────────────────────────────────────────────


def test_extract_takes_only_the_requested_subdirectory() -> None:
    """create-vite carries sixteen templates and its own CLI. Fifteen of them
    must not end up in the user's project."""
    data = _tgz(
        {
            "package/package.json": b'{"name":"create-vite"}',
            "package/template-x/index.html": b"<html>",
            "package/template-y/index.html": b"<other>",
        }
    )

    template = registry.extract(data, _starter())

    assert set(template.files) == {"index.html"}


def test_extract_restores_npms_smuggled_dotfiles() -> None:
    """npm STRIPS a real .gitignore from a published tarball, so every one of
    these packages ships an alias. Without the rename, the scaffolded project's
    first commit includes node_modules."""
    data = _tgz({"package/template-x/_gitignore": b"node_modules\n"})

    template = registry.extract(data, _starter())

    assert ".gitignore" in template.files
    assert "_gitignore" not in template.files


def test_a_bare_dotfile_alias_is_restored_too() -> None:
    """create-next-app uses no prefix at all — just `gitignore`."""
    data = _tgz({"package/tpl/gitignore": b"node_modules\n", "package/tpl/README.md": b"hi"})

    template = registry.extract(data, _starter(subdir="tpl", dotfile_prefix=""))

    assert ".gitignore" in template.files
    # And it did NOT dot-prefix an ordinary file on the way past.
    assert "README.md" in template.files


def test_binary_files_are_carried_as_base64_not_dropped() -> None:
    """A silently missing favicon is a mystery to whoever hits it."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    data = _tgz({"package/template-x/src/hero.png": png, "package/template-x/a.ts": b"x"})

    template = registry.extract(data, _starter())

    assert base64.b64decode(template.assets["src/hero.png"]) == png
    assert template.files == {"a.ts": "x"}


def test_non_utf8_content_is_carried_as_an_asset() -> None:
    """Not on the known-binary list and not text either — carry it rather than
    lose it."""
    data = _tgz({"package/template-x/weird.dat": b"\xff\xfe\x00binary"})

    template = registry.extract(data, _starter())

    assert "weird.dat" in template.assets


def test_a_path_escaping_the_template_is_skipped() -> None:
    data = _tgz({"package/template-x/../../evil.sh": b"rm -rf /", "package/template-x/ok.ts": b"x"})

    template = registry.extract(data, _starter())

    assert list(template.files) == ["ok.ts"]


def test_a_missing_subdirectory_is_our_bug_not_the_users() -> None:
    """The failure this design is most exposed to: a pinned package reorganises
    and the subdir path goes stale."""
    data = _tgz({"package/somewhere-else/a.ts": b"x"})

    with pytest.raises(CloudError) as exc:
        registry.extract(data, _starter())

    assert exc.value.status_code == 500
    assert exc.value.code == "codescaffold.template_empty"


def test_extra_files_fill_a_gap_but_never_override() -> None:
    """create-next-app ships NO package.json — its CLI generates one — so we
    supply it. If a future version starts shipping its own, theirs must win and
    our catalog entry gets deleted."""
    supplied = _starter(extra_files=(("package.json", '{"name":"ours"}'),))

    without = registry.extract(_tgz({"package/template-x/a.ts": b"x"}), supplied)
    assert without.files["package.json"] == '{"name":"ours"}'

    with_own = registry.extract(
        _tgz(
            {
                "package/template-x/a.ts": b"x",
                "package/template-x/package.json": b'{"name":"theirs"}',
            }
        ),
        supplied,
    )
    assert with_own.files["package.json"] == '{"name":"theirs"}'


# ── plan() ──────────────────────────────────────────────────────────────────


async def test_plan_is_pure_and_names_its_source() -> None:
    result = await scaffold_service.plan(WS, USER, {"prompt": "a react booking app"})

    assert result.starter.id == "react"
    assert result.starter.matched is True
    assert result.projectName == "booking"
    assert result.devPort == 5173
    # The UI should be able to say exactly what it is about to install.
    assert result.starter.source == "create-vite@9.1.1"


async def test_plan_reports_the_right_port_per_framework() -> None:
    """Next serves on 3000 and Vite on 5173. A preview pane pointed at the wrong
    one shows nothing, with no indication why."""
    react = await scaffold_service.plan(WS, USER, {"prompt": "react app"})
    nextjs = await scaffold_service.plan(WS, USER, {"prompt": "next app"})

    assert (react.devPort, nextjs.devPort) == (5173, 3000)


async def test_plan_rejects_an_empty_prompt() -> None:
    with pytest.raises(Exception):
        await scaffold_service.plan(WS, USER, {"prompt": ""})


# ── compose() ───────────────────────────────────────────────────────────────


async def test_an_unknown_starter_is_rejected_before_any_fetch() -> None:
    with pytest.raises(CloudError) as exc:
        await scaffold_service.compose(WS, USER, {"starter": "angular"})

    assert exc.value.status_code == 400
    assert "react" in str(exc.value.message)


def test_the_project_name_is_stamped_into_package_json() -> None:
    """The gap in the previous implementation: projectName was derived, returned,
    and then never written anywhere, so every project kept the template's name."""
    files = {"package.json": '{"name": "template", "version": "0.0.0"}'}

    scaffold_service._rename_package(files, "booking")

    assert json.loads(files["package.json"])["name"] == "booking"
    assert json.loads(files["package.json"])["version"] == "0.0.0"


def test_an_unparseable_package_json_is_left_alone() -> None:
    """A template we cannot rename is still a template worth scaffolding. The
    wrong name is a blemish; a refused project is a broken feature."""
    files = {"package.json": "{not json"}

    scaffold_service._rename_package(files, "booking")

    assert files["package.json"] == "{not json"


# ── Live: the only thing that catches a stale pin ───────────────────────────


@live
@pytest.mark.parametrize("starter_id", sorted(domain.BY_ID))
async def test_every_pinned_starter_really_downloads_and_extracts(starter_id: str) -> None:
    """Verifies the pin, the integrity hash, and the subdirectory path against
    the real registry. Nothing offline can catch a package that reorganised."""
    template = await registry.fetch_template(domain.BY_ID[starter_id])

    assert template.files, starter_id
    package = json.loads(template.files["package.json"])
    # Every starter must be runnable: `npm install && npm run dev`.
    assert "dev" in package.get("scripts", {}), starter_id
    assert ".gitignore" in template.files, starter_id
