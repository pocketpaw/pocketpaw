# ee/pocketpaw_ee/cloud/belt/review.py — the Review Room's backend (paw-enterprise
# docs/design/belt-review-room.md): try a run's built app on this Mac, and record
# reviews of it. The router stays thin; tenancy is ``service._owned_run`` (a
# foreign or non-belt run is the same 404 everywhere).
#
#   TRY      ``start_try`` / ``get_try``: a process-local ``TryState`` per run.
#            macOS only (else 501). The run's PR (``pr_url``, else ``cloud.pr``)
#            gives the commits to look for: the merge commit when merged, then
#            the head. The newest CI run of one holding an ``app-builds`` artifact
#            (``cloud_station.ci_artifact_run``) is found up front (none = 409),
#            then in the background: ``gh run download``; its one
#            ``app-builds.tar.gz`` is screened (no absolute, ``..`` or escaping
#            link member) and unpacked with ``tar`` under
#            ``POCKETPAW_FACTORY_APP_DIR/<run>/``; the app is the only bundle, else
#            the one the mandate's ``line:<x>`` label names (``FlareApp`` for
#            ``line:flare``), else the one the run title names; still ambiguous =
#            ``failed`` with ``apps`` and nothing opens. Then ``xattr -dr
#            com.apple.quarantine`` (ad-hoc signed CI builds) and ``open <bundle>``.
#            Preparing past ``TRY_TIMEOUT`` reads ``failed``.
#   REVIEW   ``submit_review`` parses the multipart body as it streams
#            (``_ReviewParser``): a part over its limit, too many frames or a body
#            over ``REVIEW_REQUEST_BYTES`` is refused (413) at that byte. Video is
#            webm, frames PNG, both by declared type AND magic bytes (415); a frame
#            is ``<t_ms>.png`` of a note. Media go under
#            ``POCKETPAW_FACTORY_REVIEW_DIR/<run>/<review>/``; the record goes on
#            the blob's ``reviews`` (``cloud_station._patch``). An Idempotency-Key
#            seen before replays the stored review (200), nothing else.
#   ISSUE    ``needs_changes`` files a GitHub issue on the run's repo: labels
#            ``review``, ``gap``, ``bug`` and the line's (the charter's
#            ``issue_labels``, else the source issue's ``line:*``), created first;
#            frames committed to ``belt-review-assets`` under
#            ``reviews/<review>/<t_ms>.png`` (gh cannot attach images) and linked
#            by raw URL; the body names the reviewer by display name, never email.
#            A filing failure keeps the review (``issue_error``).
#   MEDIA    ``review_media``: only a review and file the blob lists, through
#            ``service.kept_file`` (straight inside the review's dir).
#
# Every subprocess goes through the injected ``Runner`` (argv, scrubbed env, no
# shell); tests fake it.

"""The Belt Review Room: try a run's app, record reviews, file the gaps."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tarfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from pocketpaw_ee.cloud.belt.service import BeltConsoleError, _owned_run, factory_dir, kept_file

logger = logging.getLogger(__name__)

ARTIFACT = "app-builds"
TRY_TIMEOUT = 600.0
# Contract limits (§2.1).
REVIEW_VIDEO_BYTES = 200 * 1024 * 1024
REVIEW_FRAMES = 60
REVIEW_FRAME_BYTES = 5 * 1024 * 1024
REVIEW_REQUEST_BYTES = 512 * 1024 * 1024
REVIEW_NOTES = 200
NOTE_CHARS = 2000
MAX_T_MS = 7_200_000
ASSETS_BRANCH = "belt-review-assets"

_WEBM = b"\x1a\x45\xdf\xa3"
_PNG = b"\x89PNG\r\n\x1a\n"
_FRAME_NAME = re.compile(r"^(\d{1,7})\.png$")
_REVIEW_ID = re.compile(r"^rv_[0-9a-f]{16}$")
_PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_ISSUE_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+")
_GATEKEEPER = re.compile(r"damaged|verif|gatekeeper|not permitted|quarantine", re.I)

# run id -> TryState; ``_t`` is its start (epoch seconds), never on the wire.
_TRIES: dict[str, dict[str, Any]] = {}
_TASKS: set[asyncio.Task[None]] = set()

Run = Callable[..., Awaitable[tuple[int, str, str]]]


async def _run_subprocess(
    argv: list[str], *, cwd: Path, timeout: float, stdin: str | None = None
) -> tuple[int, str, str]:
    from pocketpaw_ee.cloud.belt.develop_station import run_subprocess

    return await run_subprocess(argv, cwd=cwd, timeout=timeout, stdin=stdin)


async def _default_charter_for(workspace_id: str, mandate_id: str) -> dict[str, Any] | None:
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    return await mandate_service.charter_for_mandate(workspace_id, mandate_id)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _public(state: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state.items() if not k.startswith("_")}


def _idle() -> dict[str, Any]:
    return {
        "state": "idle",
        "step": None,
        "reason": None,
        "artifact": None,
        "started_at": None,
        "updated_at": None,
    }


def _current(run_id: str) -> dict[str, Any]:
    state = _TRIES.get(run_id)
    if state is None:
        return _idle()
    if state["state"] == "preparing" and time.time() - state["_t"] > TRY_TIMEOUT:
        state.update(state="failed", step=None, reason="Timed out preparing the app.")
        state["updated_at"] = _now()
    return state


def _safe_slug(slug: str) -> str:
    return "" if any(p in {".", ".."} or p.startswith("-") for p in slug.split("/")) else slug


def _title(blob: dict[str, Any]) -> str:
    title = str(blob.get("title") or "").strip()
    return (title or (str(blob.get("task") or "").strip().splitlines() or ["this run"])[0])[:120]


def _app_key(app: Path) -> str:
    return re.sub(r"app$", "", app.stem.lower())


def _pick(apps: list[Path], labels: list[str], title: str) -> Path | None:
    """The only bundle; else the one a ``line:<x>`` label names; else the one
    the title names; else ``None`` (ambiguous)."""
    if len(apps) == 1:
        return apps[0]
    lines = {lb.split(":", 1)[1].strip().lower() for lb in labels if lb.lower().startswith("line:")}
    words = set(re.findall(r"[a-z0-9]+", title.lower()))
    for found in (
        [a for a in apps if _app_key(a) in lines],
        [a for a in apps if _app_key(a) in words or a.stem.lower() in words],
    ):
        if len(found) == 1:
            return found[0]
    return None


def _safe_tar(path: Path) -> bool:
    """Every member a relative path with no ``..``, and every link resolving
    inside the archive root."""
    with tarfile.open(path, "r:gz") as tar:
        for m in tar.getmembers():
            name = m.name
            if name.startswith("/") or ".." in Path(name).parts:
                return False
            if m.issym() or m.islnk():
                target = (
                    m.linkname if m.islnk() else os.path.join(os.path.dirname(name), m.linkname)
                )
                norm = os.path.normpath(target)
                if m.linkname.startswith("/") or norm == ".." or norm.startswith("../"):
                    return False
            elif not (m.isfile() or m.isdir()):
                return False
    return True


def _bundles(root: Path) -> list[Path]:
    """The ``.app`` bundle dirs under ``root`` (never following symlinks)."""
    found: list[Path] = []
    for here, dirs, _files in os.walk(root):
        for d in list(dirs):
            path = Path(here) / d
            if d.endswith(".app") and not path.is_symlink():
                found.append(path)
                dirs.remove(d)
    return sorted(found)


# ---------------------------------------------------------------------------
# multipart, limited as it streams
# ---------------------------------------------------------------------------


class _TooLarge(MultiPartException):
    """A review limit crossed mid-stream (413)."""


async def _capped(stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    seen = 0
    async for chunk in stream:
        seen += len(chunk)
        if seen > REVIEW_REQUEST_BYTES:
            raise _TooLarge(f"A review is at most {REVIEW_REQUEST_BYTES // 2**20} MB in all.")
        yield chunk


class _ReviewParser(MultiPartParser):
    """Starlette's parser with the review limits checked per chunk."""

    def __init__(self, headers: Headers, stream: AsyncIterator[bytes]) -> None:
        super().__init__(headers, _capped(stream), max_fields=8, max_part_size=2 * 2**20)
        self._frames = 0
        self._videos = 0
        self._bytes = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._bytes = 0

    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        part = self._current_part
        if part.file is None:
            return
        if part.field_name == "frames":
            self._frames += 1
            if self._frames > REVIEW_FRAMES:
                raise _TooLarge(f"A review carries at most {REVIEW_FRAMES} frames.")
        elif part.field_name == "video":
            self._videos += 1
            if self._videos > 1:
                raise MultiPartException("A review carries one video.")
        else:
            raise MultiPartException(f"Unexpected file part {part.field_name!r}.")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        part = self._current_part
        if part.file is not None:
            self._bytes += end - start
            video = part.field_name == "video"
            cap = REVIEW_VIDEO_BYTES if video else REVIEW_FRAME_BYTES
            if self._bytes > cap:
                what = "The video" if video else "A frame"
                raise _TooLarge(f"{what} is over the {cap // 2**20} MB limit.")
        super().on_part_data(data, start, end)


def _invalid(message: str) -> BeltConsoleError:
    return BeltConsoleError(400, message, code="belt.review_invalid")


def _bad_media(message: str) -> BeltConsoleError:
    return BeltConsoleError(415, message, code="belt.review_bad_media")


def _notes(raw: Any, verdict: str) -> list[dict[str, Any]]:
    try:
        items = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        items = None
    if not isinstance(items, list) or len(items) > REVIEW_NOTES:
        raise _invalid(f"notes must be a JSON array of at most {REVIEW_NOTES} notes.")
    notes: list[dict[str, Any]] = []
    for item in items:
        t_ms = item.get("t_ms") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if type(t_ms) is not int or not 0 <= t_ms <= MAX_T_MS or not isinstance(text, str):
            raise _invalid("Each note needs an integer t_ms (0 to 7200000) and a text.")
        text = text.strip()
        if not 1 <= len(text) <= NOTE_CHARS:
            raise _invalid(f"A note's text is 1 to {NOTE_CHARS} characters.")
        notes.append({"t_ms": t_ms, "text": text})
    if verdict == "needs_changes" and not notes:
        raise _invalid("A needs_changes review needs at least one note.")
    return sorted(notes, key=lambda n: n["t_ms"])


async def _head(upload: UploadFile, n: int) -> bytes:
    await upload.seek(0)
    data = await upload.read(n)
    await upload.seek(0)
    return data


def _copy(upload: UploadFile, dest: Path) -> None:
    with dest.open("wb") as out:
        shutil.copyfileobj(upload.file, out)


def _clock(t_ms: int) -> str:
    seconds = t_ms // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# ---------------------------------------------------------------------------
# the room
# ---------------------------------------------------------------------------


@dataclass
class ReviewRoom:
    """The Review Room's side effects, injectable: ``runner`` runs every
    subprocess, ``background`` runs a try's preparation as a task (tests run
    it inline), ``charter_for`` reads a mandate's charter."""

    runner: Run = _run_subprocess
    background: bool = True
    charter_for: Callable[[str, str], Awaitable[dict[str, Any] | None]] = _default_charter_for

    async def _ok(self, argv: list[str], cwd: Path, timeout: float = 120.0) -> str:
        code, out, err = await self.runner(argv, cwd=cwd, timeout=timeout)
        if code != 0:
            raise RuntimeError(f"{argv[0]} {argv[1]} failed (exit {code}): {err.strip()[-300:]}")
        return out

    async def _slug(self, blob: dict[str, Any]) -> str:
        """The run's GitHub ``owner/repo``: its PR URL, else its repo's origin."""
        found = _PR_URL.search(str(blob.get("pr_url") or ""))
        if found:
            return _safe_slug(found.group(1))
        from pocketpaw_ee.cloud.mandates.patrols import _github_slug

        async def gh(argv: list[str]) -> tuple[int, str, str]:
            return await self.runner(argv, cwd=Path.home(), timeout=60.0)

        return await _github_slug(str(blob.get("repo") or ""), gh) or ""

    async def _labels(self, workspace_id: str, blob: dict[str, Any]) -> list[str]:
        """The line's labels: the mandate charter's ``issue_labels``."""
        mandate_id = str(blob.get("mandate_id") or "")
        if not mandate_id:
            return []
        try:
            found = await self.charter_for(workspace_id, mandate_id) or {}
        except Exception:  # noqa: BLE001 — no charter, no line label
            logger.info("belt review: no charter for %s", mandate_id, exc_info=True)
            return []
        return [str(lb) for lb in (found.get("charter") or {}).get("issue_labels") or []]

    # -- try ------------------------------------------------------------------

    async def start_try(self, workspace_id: str, run_id: str) -> tuple[int, dict[str, Any]]:
        """Start (202) or join (200) a try of the run's app; see the header."""
        from pocketpaw_ee.cloud.belt.cloud_station import ci_artifact_run

        _, blob = await _owned_run(workspace_id, run_id)
        current = _current(run_id)
        if current["state"] in ("preparing", "opened"):
            return 200, _public(current)
        if sys.platform != "darwin":
            raise BeltConsoleError(
                501, "This station is not a Mac, so it cannot open apps.", "belt.try_unsupported"
            )
        slug = await self._slug(blob)
        found = _PR_URL.search(str(blob.get("pr_url") or ""))
        number = found.group(2) if found else str((blob.get("cloud") or {}).get("pr") or "")
        if not slug or not number.isdigit():
            raise BeltConsoleError(
                409,
                "This run has no pull request on GitHub, so CI built no app for it.",
                "belt.no_artifact",
            )
        home = Path.home()
        try:
            argv = [
                "gh",
                "pr",
                "view",
                number,
                "-R",
                slug,
                "--json",
                "state,headRefOid,mergeCommit",
            ]
            pr = json.loads(await self._ok(argv, home))
            shas = [str(pr.get("headRefOid") or "")]
            if pr.get("state") == "MERGED":
                shas.insert(0, str((pr.get("mergeCommit") or {}).get("oid") or ""))
            ci = ""
            for sha in shas:
                ci = await ci_artifact_run(lambda a: self._ok(a, home), slug, sha, ARTIFACT)
                if ci:
                    break
        except Exception as exc:  # noqa: BLE001 — gh unauthenticated, offline, or missing
            raise BeltConsoleError(
                501,
                f"This station could not read the run's CI from GitHub: {exc}",
                "belt.try_unsupported",
            ) from exc
        if not ci:
            raise BeltConsoleError(409, "CI built no app for this run.", "belt.no_artifact")
        now = _now()
        state = {
            "state": "preparing",
            "step": "Downloading the app",
            "reason": None,
            "artifact": ARTIFACT,
            "started_at": now,
            "updated_at": now,
            "_t": time.time(),
        }
        _TRIES[run_id] = state
        work = self._prepare(workspace_id, run_id, blob, slug, ci, state)
        if self.background:
            task = asyncio.ensure_future(work)
            _TASKS.add(task)
            task.add_done_callback(_TASKS.discard)
        else:
            await work
        return 202, _public(state)

    async def get_try(self, workspace_id: str, run_id: str) -> dict[str, Any]:
        await _owned_run(workspace_id, run_id)
        return _public(_current(run_id))

    async def _prepare(
        self,
        workspace_id: str,
        run_id: str,
        blob: dict[str, Any],
        slug: str,
        ci: str,
        state: dict[str, Any],
    ) -> None:
        """Download, unpack, pick and open; every failure is a ``failed`` state."""

        def step(**fields: Any) -> None:
            if _TRIES.get(run_id) is state:  # a newer try replaced this one
                state.update(fields, updated_at=_now())

        try:
            root = factory_dir("POCKETPAW_FACTORY_APP_DIR", "apps", run_id)
            shutil.rmtree(root, ignore_errors=True)
            got, apps_dir = root / "artifact", root / "apps"
            apps_dir.mkdir(parents=True)
            argv = ["gh", "run", "download", ci, "-R", slug, "-n", ARTIFACT, "-D", str(got)]
            code, _, err = await self.runner(argv, cwd=root, timeout=TRY_TIMEOUT - 60)
            if code != 0:
                step(state="failed", step=None, reason=f"Downloading the app failed: {err[-200:]}")
                return
            step(step="Unpacking the app")
            for tgz in sorted(got.rglob("*.tar.gz")):
                if tgz.is_symlink() or not _safe_tar(tgz):
                    step(state="failed", step=None, reason="The app archive has unsafe paths.")
                    return
                await self._ok(["tar", "xzf", str(tgz), "-C", str(apps_dir)], root, 300.0)
            apps = _bundles(apps_dir)
            if not apps:
                step(state="failed", step=None, reason="The CI artifact holds no macOS app.")
                return
            app = _pick(apps, await self._labels(workspace_id, blob), _title(blob))
            if app is None:
                names = [a.name for a in apps]
                reason = (
                    f"The build has {len(apps)} apps ({', '.join(names)}); this run names none."
                )
                step(state="failed", step=None, reason=reason, apps=names)
                return
            step(step=f"Opening {app.stem}")
            # Ad-hoc signed CI builds may carry quarantine; absent attrs exit non-zero.
            await self.runner(
                ["xattr", "-dr", "com.apple.quarantine", str(app)], cwd=root, timeout=60.0
            )
            code, _, err = await self.runner(["open", str(app)], cwd=root, timeout=60.0)
            if code != 0:
                why = (
                    "Gatekeeper refused the app"
                    if _GATEKEEPER.search(err)
                    else "The app did not launch"
                )
                step(state="failed", step=None, reason=f"{why}: {err.strip()[-200:]}")
                return
            step(state="opened", step=None)
        except Exception as exc:  # noqa: BLE001 — a try never crashes the loop
            logger.warning("belt review: try of %s failed", run_id, exc_info=True)
            step(state="failed", step=None, reason=f"Preparing the app failed: {exc}"[:300])

    # -- reviews --------------------------------------------------------------

    async def submit_review(
        self,
        workspace_id: str,
        run_id: str,
        *,
        by: str,
        who: str,
        headers: Headers,
        stream: AsyncIterator[bytes],
        idempotency_key: str = "",
    ) -> tuple[int, dict[str, Any]]:
        """Store one review (201), or replay the one this key stored (200)."""
        from pocketpaw.stores import get_instinct_store
        from pocketpaw_ee.cloud.belt.cloud_station import _blob, _patch

        _, blob = await _owned_run(workspace_id, run_id)
        key = idempotency_key.strip()[:200]
        for old in blob.get("reviews") or []:
            if key and isinstance(old, dict) and old.get("key") == key:
                return 200, _wire(run_id, old)
        if not headers.get("content-type", "").startswith("multipart/form-data"):
            raise _invalid("A review is multipart/form-data.")
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > REVIEW_REQUEST_BYTES:
            raise BeltConsoleError(
                413,
                f"A review is at most {REVIEW_REQUEST_BYTES // 2**20} MB in all.",
                "belt.review_too_large",
            )
        try:
            form = await _ReviewParser(headers, stream).parse()
        except _TooLarge as exc:
            raise BeltConsoleError(413, exc.message, "belt.review_too_large") from exc
        except MultiPartException as exc:
            raise _invalid(exc.message) from exc
        try:
            verdict = form.get("verdict")
            if verdict not in ("looks_good", "needs_changes"):
                raise _invalid("verdict must be looks_good or needs_changes.")
            notes = _notes(form.get("notes"), verdict)
            noted = {n["t_ms"] for n in notes}
            video = next(iter(form.getlist("video")), None)
            if video is not None:
                if not isinstance(video, UploadFile):
                    raise _invalid("video must be a file.")
                if (video.content_type or "").split(";")[0].strip().lower() != "video/webm":
                    raise _bad_media("The video must be video/webm.")
                if await _head(video, 4) != _WEBM:
                    raise _bad_media("The video is not a webm file.")
            frames: dict[int, UploadFile] = {}
            for frame in form.getlist("frames"):
                if not isinstance(frame, UploadFile):
                    raise _invalid("frames must be files.")
                named = _FRAME_NAME.match(frame.filename or "")
                if not named or int(named.group(1)) not in noted:
                    raise _invalid("Each frame is named <t_ms>.png after one of the notes.")
                if int(named.group(1)) in frames:
                    raise _invalid("Two frames share one note's t_ms.")
                if (frame.content_type or "").lower() != "image/png":
                    raise _bad_media("Frames must be image/png.")
                if await _head(frame, 8) != _PNG:
                    raise _bad_media("A frame is not a PNG file.")
                frames[int(named.group(1))] = frame
            review_id = f"rv_{secrets.token_hex(8)}"
            folder = factory_dir("POCKETPAW_FACTORY_REVIEW_DIR", "reviews", run_id) / review_id
            folder.mkdir(parents=True)
            if video is not None:
                await asyncio.to_thread(_copy, video, folder / "video.webm")
            for t_ms, frame in frames.items():
                await asyncio.to_thread(_copy, frame, folder / f"{t_ms}.png")
        finally:
            await form.close()

        review: dict[str, Any] = {
            "id": review_id,
            "by": by,
            "verdict": verdict,
            "notes": notes,
            "video": video is not None,
            "frames": sorted(frames),
            "issue_url": None,
            "issue_error": None,
            "at": _now(),
            "key": key or None,
        }
        store = get_instinct_store(workspace_id=workspace_id or None)
        fresh = await _blob(store, run_id) or {}
        await _patch(store, run_id, reviews=[*(fresh.get("reviews") or []), review])
        if verdict == "needs_changes":
            try:
                review["issue_url"] = await self._file_issue(
                    workspace_id, run_id, blob, review, who, folder
                )
            except Exception as exc:  # noqa: BLE001 — the review stands without its issue
                logger.info("belt review: no issue for %s", run_id, exc_info=True)
                review["issue_error"] = str(exc)[:300] or "Filing the issue failed."
            fresh = await _blob(store, run_id) or {}
            kept = [review if r.get("id") == review_id else r for r in fresh.get("reviews") or []]
            await _patch(store, run_id, reviews=kept)
        return 201, _wire(run_id, review)

    async def _file_issue(
        self,
        workspace_id: str,
        run_id: str,
        blob: dict[str, Any],
        review: dict[str, Any],
        who: str,
        folder: Path,
    ) -> str:
        """File the needs-changes issue; raises with one readable sentence."""
        from pocketpaw_ee.cloud.notifications.email import app_base_url

        slug = await self._slug(blob)
        if not slug:
            raise RuntimeError("The run's repo is not on GitHub, so no issue was filed.")
        line = await self._labels(workspace_id, blob)
        if not line and blob.get("issue_url"):
            argv = ["gh", "issue", "view", str(blob["issue_url"]), "--json", "labels"]
            code, out, _ = await self.runner(argv, cwd=folder, timeout=60.0)
            if code == 0:
                names = [str(lb.get("name") or "") for lb in json.loads(out).get("labels") or []]
                line = [n for n in names if n.lower().startswith("line:")]
        labels = list(dict.fromkeys(["review", "gap", "bug", *line]))
        for label in labels:  # an existing label exits non-zero; that is fine
            await self.runner(
                ["gh", "label", "create", label, "-R", slug], cwd=folder, timeout=60.0
            )
        shots = await self._upload_frames(slug, review, folder)
        pr, commit = blob.get("pr_url"), blob.get("commit_sha")
        lines = [
            f"**Needs changes**, reviewed by {who} at {review['at']}.",
            "",
            f"Review Room: {app_base_url()}/belt-next/runs/{run_id}/review",
            " · ".join(x for x in (pr and f"PR: {pr}", commit and f"commit {commit}") if x),
            "",
        ]
        for note in review["notes"]:
            lines.append(f"**{_clock(note['t_ms'])}** {note['text']}")
            if note["t_ms"] in shots:
                lines.append(f"![frame at {_clock(note['t_ms'])}]({shots[note['t_ms']]})")
            lines.append("")
        title = f"Review: {_title(blob)} needs changes"
        argv = ["gh", "issue", "create", "-R", slug, "--title", title, "--body-file", "-"]
        for label in labels:
            argv += ["--label", label]
        code, out, err = await self.runner(argv, cwd=folder, timeout=120.0, stdin="\n".join(lines))
        found = _ISSUE_URL.search(out or "")
        if code != 0 or not found:
            raise RuntimeError(f"GitHub refused the issue: {err.strip()[-200:] or 'no issue URL'}")
        return found.group(0)

    async def _upload_frames(
        self, slug: str, review: dict[str, Any], folder: Path
    ) -> dict[int, str]:
        """Commit the frames to ``ASSETS_BRANCH`` (created off the default
        branch once) and return their raw URLs; best-effort, ``{}`` on failure."""
        if not review["frames"]:
            return {}
        try:
            ref = f"repos/{slug}/git/ref/heads/{ASSETS_BRANCH}"
            code, _, _ = await self.runner(["gh", "api", ref], cwd=folder, timeout=60.0)
            if code != 0:
                argv = ["gh", "api", f"repos/{slug}/commits/HEAD", "--jq", ".sha"]
                sha = (await self._ok(argv, folder)).strip()
                await self._ok(
                    [
                        "gh",
                        "api",
                        "-X",
                        "POST",
                        f"repos/{slug}/git/refs",
                        "-f",
                        f"ref=refs/heads/{ASSETS_BRANCH}",
                        "-f",
                        f"sha={sha}",
                    ],
                    folder,
                )
            urls: dict[int, str] = {}
            for t_ms in review["frames"]:
                path = f"reviews/{review['id']}/{t_ms}.png"
                body = json.dumps(
                    {
                        "message": f"Review {review['id']} frame at {_clock(t_ms)}",
                        "branch": ASSETS_BRANCH,
                        "content": base64.b64encode((folder / f"{t_ms}.png").read_bytes()).decode(),
                    }
                )
                argv = ["gh", "api", "-X", "PUT", f"repos/{slug}/contents/{path}", "--input", "-"]
                code, _, _ = await self.runner(argv, cwd=folder, timeout=120.0, stdin=body)
                if code == 0:
                    urls[t_ms] = f"https://github.com/{slug}/raw/{ASSETS_BRANCH}/{path}"
            return urls
        except Exception:  # noqa: BLE001 — the issue goes out without its pictures
            logger.info("belt review: frames not uploaded for %s", review["id"], exc_info=True)
            return {}


def _wire(run_id: str, review: dict[str, Any]) -> dict[str, Any]:
    """A stored review in the contract's ``Review`` shape (API paths for media)."""
    base = f"/belt/runs/{run_id}/reviews/{review.get('id')}"
    return {
        "id": review.get("id"),
        "run_id": run_id,
        "by": review.get("by"),
        "verdict": review.get("verdict"),
        "notes": review.get("notes") or [],
        "video_url": f"{base}/video" if review.get("video") else None,
        "frames": [f"{base}/frames/{t}.png" for t in review.get("frames") or []],
        "issue_url": review.get("issue_url"),
        "issue_error": review.get("issue_error"),
        "at": review.get("at"),
    }


async def list_reviews(workspace_id: str, run_id: str) -> list[dict[str, Any]]:
    """The run's reviews, newest first."""
    _, blob = await _owned_run(workspace_id, run_id)
    reviews = [r for r in blob.get("reviews") or [] if isinstance(r, dict)]
    reviews.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return [_wire(run_id, r) for r in reviews]


async def review_media(workspace_id: str, run_id: str, review_id: str, name: str) -> Path:
    """A review's ``video.webm`` or ``<t_ms>.png``: only one the blob lists,
    straight inside the review's dir; anything else is a 404."""
    _, blob = await _owned_run(workspace_id, run_id)
    review = next(
        (r for r in blob.get("reviews") or [] if isinstance(r, dict) and r.get("id") == review_id),
        None,
    )
    listed = set()
    if review is not None and _REVIEW_ID.match(review_id):
        listed = {f"{t}.png" for t in review.get("frames") or []}
        if review.get("video"):
            listed.add("video.webm")
    path = None
    if name in listed:
        path = kept_file(
            factory_dir("POCKETPAW_FACTORY_REVIEW_DIR", "reviews", run_id) / review_id, name
        )
    if path is None:
        raise BeltConsoleError(404, "Not found.", "belt.review_media_not_found")
    return path


__all__ = [
    "REVIEW_FRAMES",
    "REVIEW_FRAME_BYTES",
    "REVIEW_REQUEST_BYTES",
    "REVIEW_VIDEO_BYTES",
    "ReviewRoom",
    "list_reviews",
    "review_media",
]
