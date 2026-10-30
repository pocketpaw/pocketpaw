# tests/cloud/test_belt_upstream_patrol.py — the mandate ``upstream`` patrol.
#
# The patrol reads a pinned ``rev`` out of a TOML pin file in the bound repo and
# reports commits on that GitHub repo since the pin via one ``gh api`` compare
# call. ``gh`` is a fake executor here (canned compare JSON), the bound repo is
# a tmp dir holding a two-crate Cargo.toml plus a ``[patch]`` block, and the
# ``run_patrols`` test drives the real create DTO + service against mongomock to
# pin the dedup contract: a second pass at the same upstream head files nothing.

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

# Mandates here bind tmp repos outside the default allowlist roots.
pytestmark = pytest.mark.usefixtures("any_repo_root")

from pocketpaw_ee.cloud.mandates import patrols  # noqa: E402
from pocketpaw_ee.cloud.mandates.dto import CreateMandateRequest  # noqa: E402
from pocketpaw_ee.cloud.mandates.patrols import upstream_patrol  # noqa: E402
from pydantic import ValidationError  # noqa: E402

REPO = "storytold/photocraft"
PIN = "c89c33c704e7ca988dde34703bb90b6b422f80ff"
HEAD = "7c6a78b05abd32ce6363be4cdfb2856220ed1897"
PIN_FILE = "crates/photo/Cargo.toml"

CARGO = f"""[package]
name = "photo-engine"
version = "0.0.0"

[dependencies]
photocraft-engine = {{ git = "https://github.com/{REPO}", rev = "{PIN}" }}
photocraft-text = {{ git = "https://github.com/{REPO}.git", rev = "{PIN}" }}
serde_json = "1"

[patch."https://github.com/other/thing"]
thing = {{ git = "https://github.com/other/thing", rev = "{"a" * 40}" }}
"""


def _repo(tmp_path: Path, text: str = CARGO) -> Path:
    root = tmp_path / "surface"
    (root / "crates" / "photo").mkdir(parents=True)
    (root / PIN_FILE).write_text(text, encoding="utf-8")
    return root


def _commit(i: int, title: str) -> dict:
    return {"sha": f"{i:07d}" + "f" * 33, "commit": {"message": f"{title}\n\nbody text"}}


def _compare(titles: list[str], *, ahead: int | None = None, head: str = HEAD) -> dict:
    commits = [_commit(i, t) for i, t in enumerate(titles)]
    if commits:
        commits[-1]["sha"] = head
    n = len(titles) if ahead is None else ahead
    return {
        "status": "ahead",
        "ahead_by": n,
        "total_commits": n,
        "html_url": f"https://github.com/{REPO}/compare/{PIN[:7]}...HEAD",
        "permalink_url": f"https://github.com/{REPO}/compare/storytold:{PIN[:7]}...storytold:{head[:7]}",
        "commits": commits,
    }


class FakeGh:
    """Records argv and answers with a canned ``(code, stdout, stderr)``."""

    def __init__(self, payload: dict | None = None, *, code: int = 0, err: str = "") -> None:
        self.payload = payload
        self.code = code
        self.err = err
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        out = json.dumps(self.payload) if self.payload is not None else ""
        return self.code, out, self.err


WATCH = [{"repo": REPO, "pin_file": PIN_FILE}]


async def test_summary_and_area_sightings(tmp_path):
    root = _repo(tmp_path)
    titles = (
        [f"fix(ui): ui fix {i}" for i in range(10)]
        + [f"photocraft-text: shaping {i}" for i in range(3)]
        + ["feat(render): faster tiles", "perf(render): fewer copies"]
        + ["Release 0.2.0", "Gradient dithering (reduces banding)"]
        + ["GPU: fallback", "io: x", "docs: y"]
    )
    gh = FakeGh(_compare(titles))

    drafts = await upstream_patrol(str(root), upstream=WATCH, gh=gh)

    # The pin comes from the Cargo.toml; argv is a list (no shell).
    assert gh.calls == [["gh", "api", f"repos/{REPO}/compare/{PIN}...HEAD"]]

    summary, *areas = drafts
    assert summary["summary"] == f"{REPO}: {len(titles)} commits since pin {PIN[:7]}"
    assert summary["severity"] == 2  # 1-20 commits
    assert summary["evidence"]["head"] == HEAD
    assert summary["evidence"]["dedup_key"] == f"{REPO}@{PIN}..{HEAD}"

    # Up to 5 areas, biggest first, "other" never ahead of a named area.
    assert len(areas) == 5
    names = [a["evidence"]["area"] for a in areas]
    assert names[:3] == ["ui", "photocraft-text", "render"]
    assert "other" not in names  # 6 named areas outrank the catch-all
    ui = areas[0]
    assert len(ui["evidence"]["commits"]) == 8  # capped at 8 citations
    assert ui["evidence"]["commits"][0] == {"sha": "0000000", "title": "fix(ui): ui fix 0"}
    assert ui["summary"] == f"{REPO} [ui]: 10 commit(s) since pin {PIN[:7]}"
    assert all(a["evidence"]["dedup_key"].startswith(f"{REPO}@{PIN}..{HEAD}#") for a in areas)


@pytest.mark.parametrize(
    ("ahead", "severity"), [(1, 2), (20, 2), (21, 3), (100, 3), (101, 4), (900, 4)]
)
async def test_severity_scales_with_commit_count(tmp_path, ahead, severity):
    gh = FakeGh(_compare(["fix(io): one"], ahead=ahead))
    drafts = await upstream_patrol(str(_repo(tmp_path)), upstream=WATCH, gh=gh)
    assert drafts[0]["severity"] == severity
    assert drafts[0]["summary"].startswith(f"{REPO}: {ahead} commits since pin")


async def test_head_from_permalink_when_first_page_is_partial(tmp_path):
    payload = _compare(["fix(io): one", "fix(io): two"], ahead=400)
    drafts = await upstream_patrol(str(_repo(tmp_path)), upstream=WATCH, gh=FakeGh(payload))
    assert drafts[0]["evidence"]["head"] == HEAD[:7]


async def test_up_to_date_pin_files_nothing(tmp_path):
    payload = {"status": "identical", "ahead_by": 0, "total_commits": 0, "commits": []}
    assert await upstream_patrol(str(_repo(tmp_path)), upstream=WATCH, gh=FakeGh(payload)) == []


async def test_no_watch_list_files_nothing(tmp_path):
    gh = FakeGh(_compare(["x"]))
    assert await upstream_patrol(str(_repo(tmp_path)), upstream=[], gh=gh) == []
    assert gh.calls == []


async def _one_error(root: Path, gh, watch=WATCH) -> dict:
    drafts = await upstream_patrol(str(root), upstream=watch, gh=gh)
    assert len(drafts) == 1, drafts
    (draft,) = drafts
    assert draft["patrol"] == "upstream"
    assert draft["severity"] == 1
    return draft


async def test_gh_missing_is_one_low_sighting(tmp_path):
    async def missing(argv):
        raise FileNotFoundError("gh")

    draft = await _one_error(_repo(tmp_path), missing)
    assert "gh CLI is not installed" in draft["summary"]


async def test_gh_404_is_one_low_sighting(tmp_path):
    gh = FakeGh(None, code=1, err="gh: Not Found (HTTP 404)\n")
    draft = await _one_error(_repo(tmp_path), gh)
    assert "Not Found (HTTP 404)" in draft["summary"]
    assert draft["evidence"]["dedup_key"] == f"{REPO}!gh-failed@{PIN[:7]}"


async def test_gh_garbage_is_one_low_sighting(tmp_path):
    async def garbage(argv):
        return 0, "not json", ""

    draft = await _one_error(_repo(tmp_path), garbage)
    assert "unexpected payload" in draft["summary"]


async def test_missing_pin_file(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    gh = FakeGh(_compare(["x"]))
    draft = await _one_error(root, gh)
    assert "not found" in draft["summary"]
    assert gh.calls == []  # never reaches the network


async def test_pin_file_not_toml(tmp_path):
    draft = await _one_error(_repo(tmp_path, "this is = = not toml"), FakeGh(_compare(["x"])))
    assert "not readable TOML" in draft["summary"]


async def test_pin_file_without_the_repo(tmp_path):
    draft = await _one_error(_repo(tmp_path, '[dependencies]\nserde = "1"\n'), FakeGh({}))
    assert f"no git dependency on github.com/{REPO}" in draft["summary"]


async def test_pin_must_be_a_sha(tmp_path):
    text = f'[dependencies]\np = {{ git = "https://github.com/{REPO}", rev = "main; rm -rf" }}\n'
    gh = FakeGh(_compare(["x"]))
    draft = await _one_error(_repo(tmp_path, text), gh)
    assert "not a commit sha" in draft["summary"]
    assert gh.calls == []


async def test_pin_file_cannot_escape_the_repo(tmp_path):
    root = _repo(tmp_path)
    (tmp_path / "outside.toml").write_text(CARGO, encoding="utf-8")
    watch = [{"repo": REPO, "pin_file": "../outside.toml"}]
    draft = await _one_error(root, FakeGh(_compare(["x"])), watch)
    assert "outside the bound repo" in draft["summary"]


async def test_missing_bound_repo(tmp_path):
    draft = await _one_error(tmp_path / "gone", FakeGh(_compare(["x"])))
    assert "bound repo path is missing" in draft["summary"]


def test_dto_validates_the_watch_list():
    base = {"name": "m", "surface": {"repo_id": "/r"}, "charter": {"goal": "g"}}
    ok = CreateMandateRequest.model_validate({**base, "upstream": WATCH})
    assert ok.upstream[0].repo == REPO
    assert CreateMandateRequest.model_validate(base).upstream == []  # backward compatible
    for bad in (
        {"repo": "storytold", "pin_file": PIN_FILE},
        {"repo": "../../user", "pin_file": PIN_FILE},
        {"repo": "a/b?x=1", "pin_file": PIN_FILE},
        {"repo": REPO, "pin_file": "/etc/Cargo.toml"},
        {"repo": REPO, "pin_file": "../Cargo.toml"},
    ):
        with pytest.raises(ValidationError):
            CreateMandateRequest.model_validate({**base, "upstream": [bad]})


def test_registered_patrol():
    assert patrols.PATROLS["upstream"] is upstream_patrol


async def test_run_patrols_dedupes_on_upstream_head(tmp_path, mongo_db, monkeypatch):
    """Through the real create DTO + ``run_patrols``: the watch list reaches the
    patrol, the first pass files sightings, a quiet second pass files nothing,
    and a new upstream head files a fresh summary."""
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    root = _repo(tmp_path)
    gh = FakeGh(_compare(["fix(ui): a", "fix(ui): b", "Release 1.0"]))

    async def patrol(repo_id, *, upstream=None):
        return await upstream_patrol(repo_id, upstream=upstream, gh=gh)

    monkeypatch.setitem(patrols.PATROLS, "upstream", patrol)

    created = await mandate_service.create_mandate(
        "w1",
        "u1",
        {
            "name": "photo engine",
            "surface": {"repo_id": str(root)},
            "charter": {"goal": "keep the photo engine current", "cadence": "daily"},
            "patrols": ["upstream"],
            "upstream": WATCH,
        },
    )
    detail = created["mandate"]
    assert detail["upstream"] == WATCH
    mandate_id = detail["id"]

    first = (await mandate_service.run_patrols("w1", "u1", mandate_id))["sightings"]
    assert [s["summary"] for s in first] == [
        f"{REPO}: 3 commits since pin {PIN[:7]}",
        f"{REPO} [ui]: 2 commit(s) since pin {PIN[:7]}",
        f"{REPO} [other]: 1 commit(s) since pin {PIN[:7]}",
    ]
    assert all(s["patrol"] == "upstream" for s in first)

    again = await mandate_service.run_patrols("w1", "u1", mandate_id)
    assert again["sightings"] == []  # same pin, same head: a quiet day

    # A new upstream head re-files even when the summary text is unchanged:
    # the key is repo + pin + head, not the wording.
    gh.payload = _compare(["fix(ui): a", "fix(ui): c", "Release 1.1"], head="e" * 40)
    moved = (await mandate_service.run_patrols("w1", "u1", mandate_id))["sightings"]
    assert [s["summary"] for s in moved] == [s["summary"] for s in first]
    assert moved[0]["evidence"]["head"] == "e" * 40
    assert len(gh.calls) == 3
