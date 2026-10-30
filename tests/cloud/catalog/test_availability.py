# tests/cloud/catalog/test_availability.py — per-workspace model availability.
# A run that fails on a provider credit error marks that provider out of credit
# for the workspace, for a short TTL; a later success clears it. The catalog read
# overlays the marks on its entries. Asserts:
#   * a mark covers every model of the provider, and only that provider.
#   * marks never cross workspaces.
#   * a mark expires on its own after the TTL, and a clear drops it at once.
#   * a BYOK mark covers every model (the key pays for all of them).
#   * a model id with no provider prefix is matched by its bare name.
#   * Redis being down or unset reads as "nothing marked" and never raises.
from __future__ import annotations

import fakeredis
import fakeredis.aioredis
import pytest
from pocketpaw_ee.catalog import availability
from pocketpaw_ee.catalog.models import Modality, ModelCatalogEntry
from pocketpaw_ee.cloud._core import redis_client

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_redis(monkeypatch):
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        redis_client,
        "get_redis",
        lambda: fakeredis.aioredis.FakeRedis(server=server, decode_responses=True),
    )
    return server


def _entry(id_: str, provider: str) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        id=id_, display_name=id_.split("/")[-1], provider=provider, modality=Modality.CHAT
    )


_FLASH = _entry("deepseek/deepseek-v4-flash", "deepseek")
_PRO = _entry("deepseek/deepseek-v4-pro", "deepseek")
_SONNET = _entry("anthropic/claude-sonnet-4-6", "anthropic")


async def test_a_credit_failure_marks_every_model_of_that_provider(fake_redis):  # noqa: ARG001
    await availability.record_out_of_credit("w1", "deepseek/deepseek-v4-flash", byok=False)

    marks = await availability.read_marks("w1")
    flash = availability.availability_for(_FLASH, marks)
    assert flash.status == "out_of_credit"
    assert flash.since is not None
    assert flash.message and "credit" in flash.message
    assert availability.availability_for(_PRO, marks).status == "out_of_credit"
    assert availability.availability_for(_SONNET, marks).status == "ok"


async def test_marks_never_cross_workspaces(fake_redis):  # noqa: ARG001
    await availability.record_out_of_credit("w1", "deepseek/deepseek-v4-flash", byok=False)

    assert await availability.read_marks("w2") == {}
    marks = await availability.read_marks("w2")
    assert availability.availability_for(_FLASH, marks).status == "ok"


async def test_a_mark_expires_after_the_ttl(fake_redis, monkeypatch):  # noqa: ARG001
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(availability, "_now", lambda: now["t"])
    await availability.record_out_of_credit("w1", "deepseek/deepseek-v4-flash", byok=False)

    now["t"] += availability.ttl_seconds() - 1
    assert availability.availability_for(_FLASH, await availability.read_marks("w1")).status == (
        "out_of_credit"
    )
    now["t"] += 2
    assert await availability.read_marks("w1") == {}


async def test_a_success_clears_the_mark(fake_redis):  # noqa: ARG001
    await availability.record_out_of_credit("w1", "deepseek/deepseek-v4-flash", byok=False)
    await availability.record_out_of_credit("w1", "anthropic/claude-sonnet-4-6", byok=False)

    await availability.clear("w1", "deepseek/deepseek-v4-pro", byok=False)

    marks = await availability.read_marks("w1")
    assert availability.availability_for(_FLASH, marks).status == "ok"
    assert availability.availability_for(_SONNET, marks).status == "out_of_credit"


async def test_a_byok_mark_covers_every_model_and_a_byok_success_clears_it(fake_redis):  # noqa: ARG001
    await availability.record_out_of_credit("w1", "deepseek/deepseek-v4-flash", byok=True)

    marks = await availability.read_marks("w1")
    assert availability.availability_for(_SONNET, marks).status == "out_of_credit"
    assert "key" in (availability.availability_for(_SONNET, marks).message or "")

    await availability.clear("w1", "anthropic/claude-sonnet-4-6", byok=True)
    assert await availability.read_marks("w1") == {}


async def test_a_bare_model_id_is_matched_by_name(fake_redis):  # noqa: ARG001
    # The agent default rides pydantic_ai's ``provider:model`` form.
    await availability.record_out_of_credit("w1", "litellm:claude-sonnet-4-6", byok=False)

    marks = await availability.read_marks("w1")
    assert availability.availability_for(_SONNET, marks).status == "out_of_credit"
    assert availability.availability_for(_FLASH, marks).status == "ok"


async def test_an_empty_model_records_nothing(fake_redis):  # noqa: ARG001
    await availability.record_out_of_credit("w1", "", byok=False)
    await availability.record_out_of_credit("", "deepseek/deepseek-v4-flash", byok=False)

    assert await availability.read_marks("w1") == {}


async def test_redis_down_reads_as_nothing_marked(monkeypatch):
    def _boom():
        raise RuntimeError("POCKETPAW_REDIS_URL is not set")

    monkeypatch.setattr(redis_client, "get_redis", _boom)

    await availability.record_out_of_credit("w1", "deepseek/deepseek-v4-flash", byok=False)
    await availability.clear("w1", "deepseek/deepseek-v4-flash", byok=False)
    assert await availability.read_marks("w1") == {}


async def test_overlay_copies_entries_and_leaves_the_originals_alone(fake_redis):  # noqa: ARG001
    await availability.record_out_of_credit("w1", "deepseek/deepseek-v4-flash", byok=False)

    out = await availability.overlay("w1", [_FLASH, _SONNET])

    assert [e.availability.status for e in out] == ["out_of_credit", "ok"]
    assert _FLASH.availability is None
    assert _SONNET.availability is None


async def test_overlay_without_a_workspace_leaves_availability_unset():
    out = await availability.overlay(None, [_FLASH])

    assert out[0].availability is None
