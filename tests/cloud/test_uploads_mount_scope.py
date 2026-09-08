"""The /uploads static mount serves agent avatars and nothing else.

``mount_cloud`` mounted ``StaticFiles`` at the WHOLE of ``~/.pocketpaw/uploads``
while the comment above it said "Serve uploaded avatars". Avatars are a
subdirectory. Everything any subsystem writes under that root was therefore
served too, over a prefix the auth middleware exempted:

  * the EE customer "My Files" store and its file versions,
  * websandbox durability and per-project build files,
  * site screenshots, agent deliverables, meeting artifacts,
  * the thumbnail cache and the remote-materialisation cache (which streams a
    copy of every S3 blob an agent has touched back onto local disk, so S3 mode
    did not empty the directory),
  * and ``_idx.jsonl`` — an index carrying id, storage_key, filename, mime,
    size, owner_id and chat_id for every OSS-path upload and agent-delivered
    artifact.

Storage keys are uuid4, so the bulk of it was capability-based rather than
enumerable. ``_idx.jsonl`` turned that subset into a list. And every URL that
ever leaked was a permanent, unrevocable bearer token carrying no workspace: it
ignored the soft-delete tombstone and survived the user leaving the workspace,
unlike ``GET /api/v1/uploads/{id}`` which mints a signed 5-minute grant.

THE FIX IS THE MOUNT ROOT, NOT A DENYLIST

Enumerating what must not be served is the version of this that rots. The mount
is the avatars directory itself, so the set of reachable files is defined by
where it points rather than by a list somebody has to maintain. ``_idx.jsonl``
stops being served without being moved, which matters because moving it would
strand the index on every existing deployment.

WHY NARROWING BREAKS NOTHING

``ee/pocketpaw_ee/cloud/agents/router.py`` is the ONLY place in either package
that builds a root-level ``/uploads/...`` URL, and it builds exactly
``{base}/uploads/avatars/{filename}``. User avatars are a different subsystem:
they live in the sibling ``~/.pocketpaw/avatars`` and are served by the
authenticated ``GET /api/v1/auth/avatar/{filename}``. Everything else addresses
bytes through ``/api/v1/uploads/{id}``.

Mutations that must fail these tests: re-rooting the mount at the uploads
parent, and widening either auth exemption back to the bare ``/uploads``
prefix.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CLOUD_INIT = REPO / "ee" / "pocketpaw_ee" / "cloud" / "__init__.py"
DASHBOARD_AUTH = REPO / "src" / "pocketpaw" / "dashboard_auth.py"
AGENTS_ROUTER = REPO / "ee" / "pocketpaw_ee" / "cloud" / "agents" / "router.py"


def _mount_call() -> ast.Call:
    """The app.mount(...) call whose name is "uploads"."""
    tree = ast.parse(CLOUD_INIT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mount"
        ):
            continue
        for kw in node.keywords:
            if kw.arg == "name" and getattr(kw.value, "value", None) == "uploads":
                return node
    raise AssertionError('no app.mount(..., name="uploads") found in mount_cloud')


def test_the_mount_path_is_the_avatars_subtree():
    call = _mount_call()
    path = call.args[0].value
    assert path == "/uploads/avatars", (
        f"the uploads mount is at {path!r}. At '/uploads' it serves the whole "
        "~/.pocketpaw/uploads tree, including _idx.jsonl and every tenant's files."
    )


def test_the_mount_directory_is_the_avatars_directory():
    """The path alone is not enough — the DIRECTORY is what bounds the reach.

    Mounting the parent directory at the '/uploads/avatars' path would serve
    the whole tree under a narrower-looking URL, and the path assertion above
    would still pass.
    """
    source = open(CLOUD_INIT, encoding="utf-8").read()
    tree = ast.parse(source)

    target = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and getattr(node.targets[0], "id", None) == "avatars_dir"
        ):
            target = ast.get_source_segment(source, node.value)
    assert target is not None, "expected an avatars_dir assignment feeding the mount"
    assert '"avatars"' in target, f"avatars_dir does not end at the avatars directory: {target}"

    call = _mount_call()
    directory = [kw for kw in call.keywords if kw.arg == "directory"]
    if not directory:
        static = call.args[1]
        directory = [kw for kw in static.keywords if kw.arg == "directory"]
    rendered = ast.get_source_segment(source, directory[0].value)
    assert "avatars_dir" in rendered, f"the mount does not serve avatars_dir; it serves {rendered}"


@pytest.mark.parametrize(
    "needle",
    [
        '"/uploads",',
        'startswith("/uploads")',
        'startswith("/uploads/")',
    ],
)
def test_no_auth_exemption_covers_the_bare_uploads_prefix(needle):
    """Three places exempted '/uploads' from auth. All three are narrowed.

    Left broad, re-rooting the mount later would silently inherit an
    unauthenticated prefix — which is how this was reachable in the first
    place.
    """
    text = DASHBOARD_AUTH.read_text(encoding="utf-8")
    live = [
        line for line in text.splitlines() if needle in line and not line.lstrip().startswith("#")
    ]
    assert live == [], f"a broad /uploads auth exemption is still live: {live}"


def test_the_only_root_level_uploads_url_is_an_agent_avatar():
    """The premise the narrowing rests on, asserted rather than assumed.

    If some other module starts handing out a '{base}/uploads/<something-else>'
    URL, narrowing the mount silently 404s it. This fails when that happens,
    naming the file, instead of the link just breaking in production.
    """
    offenders: list[str] = []
    for package in ("src", "ee"):
        for path in (REPO / package).rglob("*.py"):
            if "test" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "/uploads/" not in line or "/api/v1/uploads/" in line:
                    continue
                if "/uploads/avatars/" in line:
                    continue
                # Only flag URL CONSTRUCTION, not prose or path handling.
                if 'f"' in line and "/uploads/" in line and "base" in line:
                    offenders.append(f"{path.relative_to(REPO)}:{number} {line.strip()}")
    assert offenders == [], (
        "a root-level /uploads/ URL is built outside the avatars path; the "
        "narrowed mount will 404 it:\n  " + "\n  ".join(offenders)
    )


def test_the_agent_avatar_url_still_matches_the_mount():
    """The one consumer keeps working — the URL and the mount must agree."""
    text = AGENTS_ROUTER.read_text(encoding="utf-8")
    assert "/uploads/avatars/{filename}" in text, (
        "the agent avatar URL changed; the mount path must change with it"
    )
