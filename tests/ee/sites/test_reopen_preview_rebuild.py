# tests/ee/sites/test_reopen_preview_rebuild.py: a project draft whose preview files
# were purged (a publish, then "open draft") gets a real rebuild, never a stall.
#
# Publish purges the preview files but keeps the build record and arq's result for the
# deterministic job id. A reopen then finds a ``built`` record with no preview URL and
# retries the build: the stored success result must be freed so the new job queues
# (instead of arq refusing the id and the old result answering ``built`` with a null
# URL), and the per-process retry claim is keyed by the reopen, so a second
# publish → reopen of the same content on one replica retries too. A ``built`` answer
# never comes without a preview URL unless the build has none (``server_only``).
#
# The draft registry's first purge write clears ``reopened_hash`` only for a reopen
# older than the publish, so an open-draft landing between the deploy and the purge's
# first write is not undone.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.sites import build_job, draft_worker, project_build
from pocketpaw_ee.sites import service as sites_service

from tests.ee.sites.test_preview_infra_retry import (  # noqa: F401 - fixtures
    _ArqLike,
    _fresh_raise_log,
    _Info,
    arq_like,
)
from tests.ee.sites.test_project_engine import SOURCE, _Records, _Store
from tests.ee.sites.test_project_no_build import NOBUILD

POCKET = "pk-reopen-rebuild"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "https://preview.paw.test")
    sites_service._preview_retry_at.clear()
    draft_worker._reset_caches()
    yield
    sites_service._preview_retry_at.clear()


class _PurgingStore(_Store):
    """``_Store`` with the ``purge_previews`` a publish calls."""

    def purge_previews(self, pocket_id):
        for key in [k for k in self.tokens if k[0] == pocket_id]:
            del self.tokens[key]
        return True


def _built_record(records: _Records, content_hash: str, mode: str = "full") -> str:
    job_id = build_job._preview_job_id(POCKET, content_hash)
    project_build.write_build_record(
        records,
        POCKET,
        project_build.new_record(job_id, content_hash, "built", preview_mode=mode),
    )
    return job_id


def _never_built_without_a_url(art: dict) -> None:
    if art["build_status"] == "built":
        assert art["preview_url"] or art["preview_mode"] == "server_only", art


# ---------------------------------------------------------------- D3: sandbox lane


async def test_a_reopened_sandbox_draft_queues_a_fresh_build(arq_like: _ArqLike) -> None:  # noqa: F811
    """The record says built, the files are gone (purged on publish) and arq still holds
    the old job's success result: the rebuild frees that result and really queues."""
    records, store = _Records(), _Store()
    base = project_build.project_content_hash(SOURCE)
    job_id = _built_record(records, base)
    arq_like.finish(
        job_id,
        _Info(
            success=True,
            result={"status": "built", "reason": None, "preview_mode": "full", "job_id": job_id},
        ),
    )

    art = await sites_service._project_draft_artifact(
        pocket_id=POCKET, source=dict(SOURCE), store=store, _pool=arq_like, _records=records
    )

    _never_built_without_a_url(art)
    assert art["build_status"] == "queued", art
    assert arq_like.enqueued == [job_id]
    # The next poll attaches to the job in flight; it is not "built" with nothing.
    again = await sites_service._project_draft_artifact(
        pocket_id=POCKET, source=dict(SOURCE), store=store, _pool=arq_like, _records=records
    )
    _never_built_without_a_url(again)
    assert again["build_status"] in ("queued", "building"), again
    assert arq_like.enqueued == [job_id]


async def test_a_plain_refusal_of_a_finished_build_still_attaches(arq_like: _ArqLike) -> None:  # noqa: F811
    """Outside the evicted-retry path a stored success is not freed (no rebuild per poll)."""
    job_id = build_job._preview_job_id(POCKET, "h1")
    arq_like.finish(job_id, _Info(success=True, result={"status": "built", "reason": None}))

    out = await build_job.enqueue_preview_build(
        pocket_id=POCKET,
        content_hash="h1",
        engine="project",
        generator_input={},
        _pool_override=arq_like,
    )

    assert out.status == "built"
    assert arq_like.enqueued == [] and arq_like.deleted == []


@pytest.mark.parametrize(
    ("mode", "with_url"), [("server_only", False), ("full", True), ("static", True)]
)
async def test_a_built_answer_from_the_queue_reads_the_record(
    monkeypatch, mode: str, with_url: bool
) -> None:
    """A refused enqueue whose job already finished answers from what that job left:
    the served shape (``none`` plus the URL, or ``server_only`` with no URL)."""
    from pocketpaw_ee.sites import preview_origin

    records, store = _Records(), _Store()

    async def _finished(**kw):
        job_id = _built_record(records, kw["content_hash"], mode)
        if with_url:
            preview_origin.publish_draft(store, POCKET, kw["content_hash"], b"files")
        return build_job.PreviewBuildEnqueue(job_id=job_id, status="built")

    monkeypatch.setattr(build_job, "enqueue_preview_build", _finished)
    art = await sites_service._project_draft_artifact(
        pocket_id=POCKET, source=dict(SOURCE), store=store, _pool=object(), _records=records
    )

    assert art["build_status"] != "built", art
    assert art["preview_mode"] == mode
    assert bool(art["preview_url"]) is with_url


async def test_a_built_answer_with_nothing_stored_is_not_built(monkeypatch) -> None:
    records, store = _Records(), _Store()

    async def _finished(**kw):
        job_id = build_job._preview_job_id(POCKET, kw["content_hash"])
        return build_job.PreviewBuildEnqueue(job_id=job_id, status="built")

    monkeypatch.setattr(build_job, "enqueue_preview_build", _finished)
    art = await sites_service._project_draft_artifact(
        pocket_id=POCKET, source=dict(SOURCE), store=store, _pool=object(), _records=records
    )

    assert art["build_status"] == "failed", art
    assert art["build_reason"] == build_job.RUNG_ARTIFACT_MISSING
    assert art["preview_url"] is None


# ---------------------------------------------------------------- D4: no-build lane


async def test_a_second_publish_and_reopen_rebuilds_the_no_build_draft(monkeypatch) -> None:
    """publish → reopen → publish → reopen of the same content on one process: each
    reopen gets a preview URL; the retry claim of the first reopen does not block the
    second."""
    monkeypatch.setenv("PAW_SITES_DRAFT_WORKERS", "1")
    registry = draft_worker.MemoryRegistry()
    monkeypatch.setattr(draft_worker, "default_registry", lambda: registry)
    records, store = _Records(), _PurgingStore()
    content_hash = project_build.project_content_hash(NOBUILD)

    async def _artifact() -> dict:
        return await sites_service._project_draft_artifact(
            pocket_id=POCKET, source=dict(NOBUILD), store=store, _records=records
        )

    async def _publish() -> None:
        await draft_worker.purge_pocket_drafts(
            POCKET,
            reason="published",
            published_hash=content_hash,
            published_url="https://bakery.acct.workers.dev",
            registry=registry,
            store=store,
        )

    first = await _artifact()
    assert first["preview_url"]
    for _round in range(2):
        await _publish()
        assert (await _artifact())["preview_mode"] == "published"
        assert await draft_worker.reopen_for_editing(POCKET, content_hash, registry=registry)
        art = await _artifact()
        assert art["preview_url"], f"round {_round}: {art}"
        assert art["preview_mode"] == "full"


# ---------------------------------------------------------------- D6: purge vs reopen


async def test_a_reopen_after_the_publish_survives_the_purges_first_write(monkeypatch) -> None:
    """An open-draft that lands after the deploy returned but before the background
    purge's first write keeps its ``reopened_hash``; an older reopen is cleared."""
    registry = draft_worker.MemoryRegistry()
    registry.rows[POCKET] = draft_worker.DraftRecord(
        pocket_id=POCKET, state="purged", published_hash="h1", published_url="https://x"
    )
    published_at = datetime.now(UTC)
    # The reopen lands after the deploy returned (published_at), before the purge.
    assert await draft_worker.reopen_for_editing(POCKET, "h1", registry=registry)

    await draft_worker.purge_pocket_drafts(
        POCKET,
        reason="published",
        published_hash="h1",
        published_url="https://x",
        published_at=published_at,
        registry=registry,
        store=_PurgingStore(),
    )

    assert registry.rows[POCKET].reopened_hash == "h1"
    assert await draft_worker.published_view(POCKET, "h1", registry=registry) == (False, None)


async def test_a_reopen_before_the_publish_is_cleared_by_it() -> None:
    registry = draft_worker.MemoryRegistry()
    registry.rows[POCKET] = draft_worker.DraftRecord(
        pocket_id=POCKET, state="purged", published_hash="h1", published_url="https://x"
    )
    assert await draft_worker.reopen_for_editing(POCKET, "h1", registry=registry)

    await draft_worker.purge_pocket_drafts(
        POCKET,
        reason="published",
        published_hash="h1",
        published_url="https://x",
        published_at=datetime.now(UTC) + timedelta(seconds=1),
        registry=registry,
        store=_PurgingStore(),
    )

    assert registry.rows[POCKET].reopened_hash == ""
    assert (await draft_worker.published_view(POCKET, "h1", registry=registry))[0] is True
