# tests/cloud/test_belt_foreman_failure.py — a failed foreman call never stops a line.
#
# The foreman's claude call has a timeout from ``POCKETPAW_MANDATE_FOREMAN_TIMEOUT``
# (default 300 s). When the call fails (timeout, CLI error, unparseable plan),
# the shift is marked ``done`` with the failure as its outcome and one warning
# names the mandate and the prompt size, so a ``continuous`` mandate is due
# again after the minimum gap instead of sitting in ``planning`` until the
# stall timeout. After 3 consecutive foreman failures the next shift waits the
# stall timeout. The pawprints feed reads such a shift as ``failed``.

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.cloud._core.errors import CloudError  # noqa: E402
from pocketpaw_ee.cloud.mandates import foreman as foreman_mod  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402
from pocketpaw_ee.cloud.mandates.domain import ShiftDoc  # noqa: E402

from tests.cloud.test_belt_mandates import USER, WS, store  # noqa: E402,F401

pytestmark = pytest.mark.usefixtures("any_repo_root", "store")


async def test_the_foreman_timeout_comes_from_the_env(monkeypatch):
    seen: list[float] = []

    async def run(argv, *, cwd, timeout, stdin=None, on_line=None):
        seen.append(timeout)
        return 0, '{"result": "{}"}', ""

    llm = foreman_mod.ClaudeCliLlm(run=run)
    monkeypatch.delenv("POCKETPAW_MANDATE_FOREMAN_TIMEOUT", raising=False)
    await llm.plan(prompt="p", context=None)
    monkeypatch.setenv("POCKETPAW_MANDATE_FOREMAN_TIMEOUT", "420")
    await llm.plan(prompt="p", context=None)
    monkeypatch.setenv("POCKETPAW_MANDATE_FOREMAN_TIMEOUT", "soon")
    await llm.plan(prompt="p", context=None)
    assert seen == [300.0, 420.0, 300.0]


async def _mandate(tmp_path: Path) -> str:
    created = await mandate_service.create_mandate(
        WS,
        USER,
        {
            "name": "flare line",
            "surface": {"repo_id": str(tmp_path)},
            "charter": {"goal": "ship flare", "cadence": "continuous"},
        },
    )
    mid = created["mandate"]["id"]
    await mandate_service.file_feedback(
        WS, USER, mid, {"text": "text tool crashes", "severity": 4, "source": "support"}
    )
    return mid


async def test_a_failed_foreman_call_finishes_the_shift(tmp_path, mongo_db, monkeypatch, caplog):
    mid = await _mandate(tmp_path)
    monkeypatch.setenv("POCKETPAW_MANDATE_LLM", "mock")
    real_plan_shift = foreman_mod.plan_shift

    async def timed_out(context, llm=None):
        raise RuntimeError("claude CLI failed (exit -1): timed out after 300s")

    monkeypatch.setattr(foreman_mod, "plan_shift", timed_out)
    with caplog.at_level(logging.WARNING), pytest.raises(CloudError) as err:
        await mandate_service.trigger_shift(WS, USER, mid)

    assert err.value.code == "mandate.foreman_failed"
    shift = await ShiftDoc.find_one(ShiftDoc.mandate_id == mid)
    assert shift.state == "done"
    assert shift.outcome.startswith("foreman call failed: claude CLI failed")
    (line,) = [r.getMessage() for r in caplog.records if "foreman call failed" in r.getMessage()]
    assert mid in line
    size = re.search(r"prompt (\d+) chars", line)
    assert size and int(size.group(1)) > 200  # the real prompt's size, not a placeholder

    # The continuous scheduler is due again once the minimum gap has passed.
    later = datetime.now(UTC) + timedelta(minutes=6)
    assert mid in {r["mandate_id"] for r in await mandate_service.list_cadence_due(later)}

    prints = (await mandate_service.get_pawprints(WS, USER, mid))["pawprints"]
    assert [p["kind"] for p in prints] == ["failed"]
    assert "timed out" in prints[0]["summary"]

    # The next shift (the mock foreman, the failed shift in its history) and
    # the digest both read a ``done`` shift that has no plan.
    monkeypatch.setattr(foreman_mod, "plan_shift", real_plan_shift)
    nxt = (await mandate_service.trigger_shift(WS, USER, mid))["shift"]
    assert nxt["no"] == 2 and nxt["state"] == "in_gate"
    digest = await mandate_service.digest(WS, USER)
    (row,) = digest["mandates"]
    assert [s["no"] for s in row["shifts"]] == [2, 1]


async def _failed_shifts(mid: str, n: int, age: timedelta) -> None:
    for no in range(1, n + 1):
        shift = ShiftDoc(
            workspace=WS,
            mandate_id=mid,
            no=no,
            state="done",
            outcome="foreman call failed: claude CLI failed (exit -1): timed out",
        )
        await shift.insert()
        await shift.set({ShiftDoc.createdAt: datetime.now(UTC) - age - timedelta(seconds=n - no)})


async def _due(mid: str) -> bool:
    rows = await mandate_service.list_cadence_due(datetime.now(UTC))
    return mid in {r["mandate_id"] for r in rows}


async def test_two_foreman_failures_still_retry_after_the_gap(tmp_path, mongo_db):
    mid = await _mandate(tmp_path)
    await _failed_shifts(mid, 2, timedelta(minutes=10))
    assert await _due(mid)


async def test_three_foreman_failures_wait_the_stall_timeout(tmp_path, mongo_db, monkeypatch):
    mid = await _mandate(tmp_path)
    await _failed_shifts(mid, 3, timedelta(minutes=10))
    assert not await _due(mid), "backing off after 3 failures in a row"
    monkeypatch.setenv("POCKETPAW_MANDATE_CONTINUOUS_STALL_HOURS", "0.1")
    assert await _due(mid), "the stall timeout releases it"
