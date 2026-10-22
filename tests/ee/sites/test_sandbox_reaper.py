# tests/ee/sites/test_sandbox_reaper.py — the seams around orphaned build sandboxes that
# test_build_sandbox_teardown.py does not pin: the job-leftover delete a re-run does
# before its create, the reaper's failure handling and age parsing, the cron's no-op
# without Daytona, the build jobs' labels, and DaytonaClient forwarding labels to the SDK.
from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.sites import sandbox_reaper as sr

from tests.ee.sites.test_build_sandbox_teardown import LedgerDaytona, _build


class LabelledLedger(LedgerDaytona):
    """LedgerDaytona whose ``list_sandboxes`` filters by label, like the real client."""

    async def list_sandboxes(self, labels: dict[str, str] | None = None) -> list[Any]:
        boxes = []
        for sid, info in self.boxes.items():
            have = self.labels.get(sid, {})
            if all(have.get(k) == v for k, v in (labels or {}).items()):
                boxes.append(replace(info, labels=dict(have)))
        return boxes

    async def find_sandbox_by_name(self, name: str) -> Any:
        info = await super().find_sandbox_by_name(name)
        return None if info is None else replace(info, labels=dict(self.labels.get(info.id, {})))


class TestJobLeftovers:
    async def test_a_rerun_deletes_the_box_its_first_attempt_left(self) -> None:
        client = LabelledLedger(memory_cap_gib=100)
        first = client.add_box("paw-build-1")
        client.labels[first.id] = {"paw_lane": "site-build", "paw_job": "j-1"}
        other = client.add_box("paw-build-2")
        client.labels[other.id] = {"paw_lane": "site-build", "paw_job": "j-2"}

        await _build(client, labels={"paw_job": "j-1"})

        assert first.id in client.deleted
        assert other.id in client.boxes, "a different job's box must survive"

    async def test_only_an_exact_job_match_is_deleted_whatever_the_server_returns(
        self,
    ) -> None:
        client = LabelledLedger(memory_cap_gib=100)
        other = client.add_box("paw-build-2")
        client.labels[other.id] = {"paw_lane": "site-build", "paw_job": "j-2"}
        everything = client.list_sandboxes

        async def sloppy(labels: dict[str, str] | None = None) -> list[Any]:
            return await everything()  # a server that ignored the filter

        client.list_sandboxes = sloppy  # type: ignore[method-assign]
        labels = {"paw_lane": "site-build", "paw_job": "j-1"}
        assert await sr.delete_job_leftovers(client, labels) == []
        assert other.id in client.boxes

    async def test_no_job_label_means_no_lookup(self) -> None:
        client = LabelledLedger(memory_cap_gib=100)
        stray = client.add_box("paw-build-1")
        client.labels[stray.id] = {"paw_lane": "site-build", "paw_job": "j-1"}
        assert await sr.delete_job_leftovers(client, {"paw_lane": "site-build"}) == []
        assert stray.id in client.boxes

    async def test_a_client_without_label_filtering_is_skipped(self) -> None:
        client = LedgerDaytona(memory_cap_gib=100)  # list_sandboxes() takes no labels
        client.add_box("paw-build-1")
        labels = {"paw_lane": "site-build", "paw_job": "j-1"}
        assert await sr.delete_job_leftovers(client, labels) == []
        assert len(client.boxes) == 1


class TestReaper:
    async def test_a_failed_delete_is_skipped_and_the_sweep_continues(self) -> None:
        now = datetime.now(UTC)
        old = now - timedelta(seconds=sr.default_max_age_seconds() + 60)
        client = LedgerDaytona(memory_cap_gib=100)
        stuck = client.add_box("paw-build-1", created_at=old)
        free = client.add_box("paw-verify-2", created_at=old)
        real_delete = client.delete_sandbox

        async def delete(sandbox_id: str) -> None:
            if sandbox_id == stuck.id:
                raise RuntimeError("daytona 500")
            await real_delete(sandbox_id)

        client.delete_sandbox = delete  # type: ignore[method-assign]
        assert await sr.reap_orphan_build_sandboxes(client, now=now) == [free.id]
        assert stuck.id in client.boxes

    async def test_max_age_is_honoured_and_unreadable_ages_are_left(self) -> None:
        now = datetime.now(UTC)
        client = LedgerDaytona(memory_cap_gib=100)
        aged = client.add_box("paw-build-1", created_at=now - timedelta(seconds=120))
        client.boxes[client.add_box("paw-build-2").id].created_at = "not a date"
        deleted = await sr.reap_orphan_build_sandboxes(client, now=now, max_age_seconds=60)
        assert deleted == [aged.id]

    def test_default_max_age_covers_the_longest_job_with_under_an_hour_margin(self) -> None:
        from pocketpaw_ee.sites.build_job import (
            site_build_job_timeout_seconds,
            site_preview_job_timeout_seconds,
        )

        longest = max(site_build_job_timeout_seconds(), site_preview_job_timeout_seconds())
        assert longest < sr.default_max_age_seconds() < longest + 3600

    async def test_the_cron_is_a_no_op_without_daytona(self, monkeypatch) -> None:
        from pocketpaw_ee.cloud.daytona import client as client_mod

        monkeypatch.setattr(client_mod, "get_daytona_client", lambda: None)
        assert await sr.reap_build_sandboxes_job({}) == 0

    async def test_the_cron_reaps_with_the_process_client(self, monkeypatch) -> None:
        from pocketpaw_ee.cloud.daytona import client as client_mod

        ledger = LedgerDaytona(memory_cap_gib=100)
        ledger.add_box("paw-build-1", created_at=datetime.now(UTC) - timedelta(days=1))
        monkeypatch.setattr(client_mod, "get_daytona_client", lambda: ledger)
        assert await sr.reap_build_sandboxes_job({}) == 1
        assert ledger.boxes == {}


class TestJobLabels:
    def test_labels_name_the_pocket_and_the_arq_job(self) -> None:
        from pocketpaw_ee.sites.build_job import sandbox_labels

        assert sandbox_labels({"job_id": "site-build-x"}, "p-1") == {
            "paw_pocket": "p-1",
            "paw_job": "site-build-x",
        }
        assert sandbox_labels({}, "") == {}


class TestClientLabels:
    async def test_create_sandbox_sends_labels_to_the_sdk(self, monkeypatch) -> None:
        from pocketpaw_ee.cloud.daytona import client as client_mod

        sent: list[Any] = []

        class _Sdk:
            async def create(self, params: Any) -> Any:
                sent.append(params)
                return SimpleNamespace(
                    id="sb-1", name=params.name, state="creating", organization_id="o"
                )

        c = client_mod.DaytonaClient.__new__(client_mod.DaytonaClient)

        async def _ensure() -> Any:
            return _Sdk()

        monkeypatch.setattr(c, "_ensure_daytona", _ensure)
        monkeypatch.setattr(client_mod, "resolve_sandbox_image", lambda: "img")
        info = await c.create_sandbox(name="paw-build-1", labels={"paw_lane": "site-build"})
        assert sent[0].labels == {"paw_lane": "site-build"}
        assert info.labels == {}


class TestShieldedTeardown:
    async def test_a_second_cancel_does_not_abandon_the_delete(self) -> None:
        """A superseded job can be cancelled again while it cleans up; the delete must
        finish anyway, or the box it was deleting leaks."""
        import asyncio

        client = LedgerDaytona()
        client.block_create = True
        gate = asyncio.Event()
        in_lookup = asyncio.Event()
        real_find = client.find_sandbox_by_name

        async def slow_find(name: str) -> Any:
            in_lookup.set()
            await gate.wait()
            return await real_find(name)

        client.find_sandbox_by_name = slow_find  # type: ignore[method-assign]
        task = asyncio.create_task(_build(client, sandbox_name="paw-build-twice"))
        await asyncio.wait_for(client.registered.wait(), timeout=2)
        task.cancel()
        await asyncio.wait_for(in_lookup.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        await asyncio.wait_for(asyncio.gather(*list(sr._background)), timeout=2)
        assert client.boxes == {}


# A real preview job id: "site-preview-" + a 24-hex pocket id + "-" + a sha256 content hash.
LONG_JOB = "site-preview-" + "a" * 24 + "-" + "b" * 64
_K8S_VALUE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")


class TestLabelBound:
    def test_a_long_preview_job_id_becomes_a_valid_bounded_label(self) -> None:
        assert len(LONG_JOB) > 100
        value = sr.label_value(LONG_JOB)
        assert len(value) <= sr.LABEL_VALUE_MAX
        assert _K8S_VALUE.match(value), value
        assert value.startswith("site-preview-")

    def test_the_bound_is_stable_and_idempotent(self) -> None:
        value = sr.label_value(LONG_JOB)
        assert sr.label_value(LONG_JOB) == value
        assert sr.label_value(value) == value
        assert sr.label_value(LONG_JOB + "x") != value

    def test_short_valid_values_pass_through_and_unsafe_ones_are_fixed(self) -> None:
        assert sr.label_value("site-build") == "site-build"
        assert sr.label_value("p-1") == "p-1"
        for raw in ("-lead", "trail_", "has space", "slash/es", "é", ""):
            value = sr.label_value(raw)
            assert _K8S_VALUE.match(value), (raw, value)

    def test_every_stamped_value_is_bounded(self) -> None:
        labels = sr.lane_labels("site-build", {"paw_pocket": "p" * 80, "paw_job": LONG_JOB})
        assert all(_K8S_VALUE.match(v) for v in labels.values()), labels

    async def test_a_rerun_with_a_long_job_id_still_finds_its_leftover(self) -> None:
        client = LabelledLedger(memory_cap_gib=100)
        first = client.add_box("paw-build-1")
        client.labels[first.id] = sr.lane_labels("site-build", {"paw_job": LONG_JOB})

        # Raw id in, as a caller that skipped lane_labels would pass it.
        deleted = await sr.delete_job_leftovers(
            client, {"paw_lane": "site-build", "paw_job": LONG_JOB}
        )
        assert deleted == [first.id]

    async def test_run_build_stamps_the_bounded_id_and_cleans_up_by_it(self) -> None:
        client = LabelledLedger(memory_cap_gib=100)
        first = client.add_box("paw-build-1")
        client.labels[first.id] = sr.lane_labels("site-build", {"paw_job": LONG_JOB})

        await _build(client, labels={"paw_job": LONG_JOB})

        assert first.id in client.deleted
        sent = client.create_kwargs[-1]["labels"]
        assert sent["paw_job"] == sr.label_value(LONG_JOB)


class TestUniqueNames:
    async def test_two_creates_in_the_same_millisecond_get_distinct_names(
        self, monkeypatch
    ) -> None:
        from pocketpaw_ee.sites import browser_check as bc

        from tests.ee.sites.test_build_sandbox_teardown import _verify

        monkeypatch.setattr(sr.time, "time", lambda: 1_700_000_000.123)
        client = LedgerDaytona(memory_cap_gib=0)  # records the create, then refuses
        for _ in range(2):
            with pytest.raises(Exception):
                await _build(client)
            with pytest.raises(Exception):
                await _verify(client)
        names = [kw["name"] for kw in client.create_kwargs]
        assert len(set(names)) == 4, names
        assert all(n.startswith(sr.REAPABLE_PREFIXES) for n in names)
        assert all(re.fullmatch(r"paw-(build|verify)-1700000000123-[0-9a-f]{8}", n) for n in names)
        assert bc.VERIFY_LANE == "site-verify"

    async def test_a_failed_create_never_deletes_another_jobs_box(self) -> None:
        """Even if two creates did share a name, the by-name teardown only deletes a box
        stamped with its own ``paw_job``."""
        client = LabelledLedger(memory_cap_gib=4)
        theirs = client.add_box("paw-build-shared")
        client.labels[theirs.id] = {"paw_lane": "site-build", "paw_job": "j-theirs"}

        # The org is full (their box holds it all), so our create refuses after the
        # teardown path has looked the shared name up.
        with pytest.raises(Exception):
            await _build(client, sandbox_name="paw-build-shared", labels={"paw_job": "j-mine"})
        assert theirs.id in client.boxes, "a failed create deleted another job's live box"

    async def test_a_matching_job_label_is_still_torn_down(self) -> None:
        client = LabelledLedger()
        client.fail_after_register = RuntimeError("create wait timed out")
        with pytest.raises(RuntimeError):
            await _build(client, sandbox_name="paw-build-mine", labels={"paw_job": "j-mine"})
        assert client.boxes == {}


class TestReaperQuery:
    async def test_the_reaper_asks_the_server_per_prefix_with_an_age_cutoff(self) -> None:
        now = datetime.now(UTC)
        old = now - timedelta(days=1)
        ledger = LedgerDaytona(memory_cap_gib=100)
        build = ledger.add_box("paw-build-1", created_at=old)
        ledger.add_box("PAW-BUILD-2", created_at=old)  # the server prefix is case-insensitive
        calls: list[tuple[str, datetime | None]] = []

        async def by_prefix(prefix: str, *, created_before: datetime | None = None) -> list[Any]:
            calls.append((prefix, created_before))
            return [b for b in ledger.boxes.values() if b.name.lower().startswith(prefix)]

        async def everything() -> list[Any]:
            raise AssertionError("the reaper listed the whole org")

        ledger.list_sandboxes_by_prefix = by_prefix  # type: ignore[attr-defined]
        ledger.list_sandboxes = everything  # type: ignore[method-assign]
        deleted = await sr.reap_orphan_build_sandboxes(ledger, now=now, max_age_seconds=60)

        assert deleted == [build.id], "the exact prefix check still applies"
        assert [p for p, _ in calls] == list(sr.REAPABLE_PREFIXES)
        assert all(c == now - timedelta(seconds=60) for _, c in calls)

    async def test_daytona_client_builds_the_narrowed_query(self, monkeypatch) -> None:
        from pocketpaw_ee.cloud.daytona import client as client_mod

        queries: list[Any] = []

        class _Sdk:
            async def list(self, query: Any = None):
                queries.append(query)
                return
                yield

        c = client_mod.DaytonaClient.__new__(client_mod.DaytonaClient)

        async def _ensure() -> Any:
            return _Sdk()

        monkeypatch.setattr(c, "_ensure_daytona", _ensure)
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        assert await c.list_sandboxes_by_prefix("paw-build-", created_before=cutoff) == []
        assert (queries[0].name, queries[0].created_at_before) == ("paw-build-", cutoff)
