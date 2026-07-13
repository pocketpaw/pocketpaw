# tests/cloud/test_trust_ledger.py
# Created: 2026-06-18 (feat/instinct-gate-foundation, T2) — file-I/O tests
# for the per-(workspace, pocket, action) trust ledger that feeds the
# layered/learning Instinct gate. No DB. Each test points the ledger at a
# tmp_path home via monkeypatch so the real ~/.pocketpaw is never touched.
# Pins cases T-10..T-17 + T-37 from the 2026-06-18 gate-layered-learning
# design's Test plan: empty→(0.0,0), score = auto/total, per-workspace
# isolation, window filtering, append semantics, 0700/0600 permissions,
# warmup floor, and backend-change reset.

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud.pockets import trust_ledger


@pytest.fixture(autouse=True)
def _tmp_home(monkeypatch, tmp_path):
    """Redirect the ledger's home dir to a tmp path for every test."""
    monkeypatch.setattr(trust_ledger.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _sidecar(home, workspace_id: str):
    return home / ".pocketpaw" / "instinct" / "trust" / f"{workspace_id}.jsonl"


def _write_rows(home, workspace_id: str, rows: list[dict]) -> None:
    """Write raw ledger rows directly (bypassing record_correction) so a
    test can seed a specific approved/corrected mix + timestamps."""
    path = _sidecar(home, workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _row(pocket_id: str, action: str, *, auto: bool, ts: datetime) -> dict:
    return {
        "pocket_id": pocket_id,
        "action": action,
        "was_auto_approved": auto,
        "ts": ts.isoformat(),
    }


# T-10: fresh sidecar, 0 rows → (0.0, 0).
async def test_t10_empty_returns_zero() -> None:
    score, count = await trust_ledger.get_trust_score("w1", "pocket-a", "charge")
    assert score == 0.0
    assert count == 0


# T-11: 8 auto + 2 human-corrected over 30 days → score 0.8, count 10.
async def test_t11_score_is_auto_over_total(_tmp_home) -> None:
    now = datetime.now(UTC)
    rows = [_row("pocket-a", "charge", auto=True, ts=now) for _ in range(8)]
    rows += [_row("pocket-a", "charge", auto=False, ts=now) for _ in range(2)]
    _write_rows(_tmp_home, "w1", rows)

    score, count = await trust_ledger.get_trust_score("w1", "pocket-a", "charge")
    assert score == pytest.approx(0.8)
    assert count == 10


# T-12: per-workspace isolation — w2's rows don't affect w1.
async def test_t12_workspace_isolation(_tmp_home) -> None:
    now = datetime.now(UTC)
    _write_rows(_tmp_home, "w2", [_row("pocket-a", "charge", auto=True, ts=now) for _ in range(5)])
    score_w1, count_w1 = await trust_ledger.get_trust_score("w1", "pocket-a", "charge")
    assert (score_w1, count_w1) == (0.0, 0)

    score_w2, count_w2 = await trust_ledger.get_trust_score("w2", "pocket-a", "charge")
    assert count_w2 == 5


# T-13: window_days filters older rows.
async def test_t13_window_filters_old_rows(_tmp_home) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=20)
    rows = [_row("pocket-a", "charge", auto=True, ts=now) for _ in range(3)]
    rows += [_row("pocket-a", "charge", auto=False, ts=old) for _ in range(5)]
    _write_rows(_tmp_home, "w1", rows)

    # window of 7 days keeps only the 3 recent auto rows.
    score, count = await trust_ledger.get_trust_score("w1", "pocket-a", "charge", window_days=7)
    assert count == 3
    assert score == pytest.approx(1.0)


# T-14: record_correction appends; get_trust_score reflects it next call.
async def test_t14_record_correction_appends_and_reflects() -> None:
    s0, c0 = await trust_ledger.get_trust_score("w1", "pocket-a", "charge")
    assert (s0, c0) == (0.0, 0)

    await trust_ledger.record_correction("w1", "pocket-a", "charge", was_auto_approved=True)

    s1, c1 = await trust_ledger.get_trust_score("w1", "pocket-a", "charge")
    assert c1 == 1
    assert s1 == pytest.approx(1.0)


# T-15: two corrections in the same second → two distinct rows (no dedup).
async def test_t15_no_dedup_same_second(_tmp_home) -> None:
    await trust_ledger.record_correction("w1", "p", "a", was_auto_approved=True)
    await trust_ledger.record_correction("w1", "p", "a", was_auto_approved=False)

    _, count = await trust_ledger.get_trust_score("w1", "p", "a")
    assert count == 2

    # confirm both lines physically present on disk
    lines = _sidecar(_tmp_home, "w1").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


# T-16: directory 0700, file 0600.
async def test_t16_permissions(_tmp_home) -> None:
    await trust_ledger.record_correction("w1", "p", "a", was_auto_approved=True)

    path = _sidecar(_tmp_home, "w1")
    dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(path.stat().st_mode)
    assert dir_mode == 0o700, f"dir mode was {oct(dir_mode)}"
    assert file_mode == 0o600, f"file mode was {oct(file_mode)}"


# T-17: warmup floor — exactly one human-approved row yields proposed_count=1.
async def test_t17_warmup_floor_count_one() -> None:
    # a single human decision (was_auto_approved=False) still counts toward
    # proposed_count, so the classifier's cold-start floor (count==0) is
    # cleared after one execution.
    await trust_ledger.record_correction("w1", "p", "a", was_auto_approved=False)
    score, count = await trust_ledger.get_trust_score("w1", "p", "a")
    assert count == 1
    assert score == pytest.approx(0.0)


# T-37: backend credential change resets trust for the affected pocket.
async def test_t37_reset_pocket_trust(_tmp_home) -> None:
    now = datetime.now(UTC)
    _write_rows(
        _tmp_home,
        "w1",
        [_row("pocket-a", "charge", auto=True, ts=now) for _ in range(5)]
        + [_row("pocket-b", "charge", auto=True, ts=now) for _ in range(3)],
    )
    # sanity: both pockets have history
    assert (await trust_ledger.get_trust_score("w1", "pocket-a", "charge"))[1] == 5
    assert (await trust_ledger.get_trust_score("w1", "pocket-b", "charge"))[1] == 3

    await trust_ledger.reset_pocket_trust("w1", "pocket-a")

    # pocket-a is reset to cold-start; pocket-b is untouched.
    assert await trust_ledger.get_trust_score("w1", "pocket-a", "charge") == (0.0, 0)
    assert (await trust_ledger.get_trust_score("w1", "pocket-b", "charge"))[1] == 3


# Action isolation — distinct actions on the same pocket score independently.
async def test_action_isolation() -> None:
    await trust_ledger.record_correction("w1", "p", "charge", was_auto_approved=True)
    await trust_ledger.record_correction("w1", "p", "refund", was_auto_approved=False)

    s_charge, c_charge = await trust_ledger.get_trust_score("w1", "p", "charge")
    s_refund, c_refund = await trust_ledger.get_trust_score("w1", "p", "refund")
    assert (c_charge, s_charge) == (1, pytest.approx(1.0))
    assert (c_refund, s_refund) == (1, pytest.approx(0.0))
