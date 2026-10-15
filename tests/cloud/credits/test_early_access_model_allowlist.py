# tests/cloud/credits/test_early_access_model_allowlist.py: a platform-funded
# turn on an unpaid early-access workspace may only use a model on
# ``free_model_allowlist`` that the metering ladder can price. Opus picked
# anywhere (per-send override, agent config) is refused with
# ``model.not_allowed`` on both seams before any backend is built; an
# allowlisted model nothing prices is refused too, since it would bill 0. The
# launch default ``litellm:claude-haiku-4-5`` runs and its run debits. Paid
# workspaces and BYOK turns are untouched.
#
# Same harness as ``test_early_access_byok.py``: early-access posture, real
# ledger, real guard, real ChatRunDoc rows and Agent documents. Only scope
# resolution, the Redis stream and the agent event source are stubbed.
from __future__ import annotations

import pytest

from tests.cloud.credits.test_early_access_byok import (
    _post_turn,
    _spec,
    _store_key,
    _terminal,
    _wire_executor,
)
from tests.cloud.credits.test_early_access_cap import _rate_card
from tests.cloud.credits.test_early_access_run_bounds import _set_plan

OPUS = "claude-opus-4-6"
HAIKU_DEFAULT = "litellm:claude-haiku-4-5"


async def _agent(ws: str, uid: str, *, backend: str = "pydantic_ai", model: str = "") -> str:
    from pocketpaw_ee.cloud.models.agent import Agent, AgentConfig

    doc = Agent(
        workspace=ws,
        name="A",
        slug=f"a-{backend}-{model or 'default'}",
        owner=uid,
        config=AgentConfig(backend=backend, model=model),
    )
    await doc.insert()
    return str(doc.id)


@pytest.fixture
def haiku_default(early_access_posture, monkeypatch):
    """The launch deployment default: pydantic_ai on Haiku 4.5 via the gateway."""
    monkeypatch.setattr(early_access_posture, "pydantic_ai_model", HAIKU_DEFAULT)
    return early_access_posture


async def _execute(monkeypatch, ws: str, uid: str, agent_id: str, run_id: str, **spec_update):
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    transport, seen = _wire_executor(monkeypatch, ws, uid, agent_id)
    spec = _spec(ws, uid, run_id).model_copy(update=spec_update)
    await run_service.create_run(spec)
    await run_core.execute_run(spec)
    return await _terminal(transport, spec.run_id), seen


async def _assert_refused(end, seen, spy, run_id: str) -> None:
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    assert end.event == "error", end.data
    assert end.data["code"] == "model.not_allowed"
    assert end.data["message"]
    assert seen == []
    assert spy == []
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == run_id)
    assert doc is not None and doc.status == "failed"


# ---------------------------------------------------------------------------
# Opus is refused wherever it was picked
# ---------------------------------------------------------------------------


async def test_claude_agent_sdk_agent_on_opus_is_refused(
    ea_user, haiku_default, backend_factory_spy, monkeypatch
):
    """Required test: an agent with ``backend=claude_agent_sdk`` and an Opus
    model runs on pydantic_ai with an allowed model, or is refused. Here the
    user picked Opus, so it is refused, and no backend is ever built."""
    ws, uid = ea_user.workspace_id, ea_user.user_id
    agent_id = await _agent(ws, uid, backend="claude_agent_sdk", model=OPUS)

    end, seen = await _execute(monkeypatch, ws, uid, agent_id, "r-sdk-opus")
    await _assert_refused(end, seen, backend_factory_spy, "r-sdk-opus")


async def test_pydantic_ai_agent_on_opus_is_refused(
    ea_user, haiku_default, backend_factory_spy, monkeypatch
):
    ws, uid = ea_user.workspace_id, ea_user.user_id
    agent_id = await _agent(ws, uid, model=f"litellm:{OPUS}")

    end, seen = await _execute(monkeypatch, ws, uid, agent_id, "r-pai-opus")
    await _assert_refused(end, seen, backend_factory_spy, "r-pai-opus")


async def test_per_send_opus_override_is_refused_on_execute_run(
    ea_user, haiku_default, backend_factory_spy, monkeypatch
):
    ws, uid = ea_user.workspace_id, ea_user.user_id
    agent_id = await _agent(ws, uid)

    end, seen = await _execute(
        monkeypatch, ws, uid, agent_id, "r-override-opus", model_override=OPUS
    )
    await _assert_refused(end, seen, backend_factory_spy, "r-override-opus")


async def test_per_send_opus_override_is_refused_on_chat_http(
    ea_user, haiku_default, backend_factory_spy, monkeypatch
):
    ws, uid = ea_user.workspace_id, ea_user.user_id

    resp, submitted = await _post_turn(monkeypatch, ws, uid, await _agent(ws, uid), model=OPUS)

    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "model.not_allowed"
    assert error["message"]
    assert submitted == []
    assert backend_factory_spy == []


async def test_agent_stored_on_opus_is_refused_on_chat_http(
    ea_user, haiku_default, backend_factory_spy, monkeypatch
):
    """No per-send model: the agent's own Opus is the effective model, so the
    HTTP seam answers 400 before a run doc, a stream or a turn counter exists.

    Mutation: the HTTP check reads only the per-send model.
    """
    ws, uid = ea_user.workspace_id, ea_user.user_id
    agent_id = await _agent(ws, uid, backend="claude_agent_sdk", model=OPUS)

    resp, submitted = await _post_turn(monkeypatch, ws, uid, agent_id)

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "model.not_allowed"
    assert submitted == []
    assert backend_factory_spy == []


async def test_forced_run_checks_the_default_it_will_really_run_on(
    ea_user, haiku_default, backend_factory_spy, monkeypatch
):
    """A claude_agent_sdk agent forced onto pydantic_ai runs on the deployment
    default, not its own model. An allowed agent model does not let the run
    spend on a default that is not allowed.

    Mutation: check only the requested model.
    """
    ws, uid = ea_user.workspace_id, ea_user.user_id
    monkeypatch.setattr(haiku_default, "pydantic_ai_model", f"litellm:{OPUS}")
    agent_id = await _agent(ws, uid, backend="claude_agent_sdk", model="claude-sonnet-4-6")

    end, seen = await _execute(monkeypatch, ws, uid, agent_id, "r-sdk-default-opus")
    await _assert_refused(end, seen, backend_factory_spy, "r-sdk-default-opus")


# ---------------------------------------------------------------------------
# Unpriced models are refused even when allowlisted
# ---------------------------------------------------------------------------


async def test_allowlisted_but_unpriced_model_is_refused(
    ea_user, haiku_default, backend_factory_spy, monkeypatch
):
    """An unpriced model bills 0, so a platform-funded turn on it runs free.
    The allowlist alone is not enough.

    Mutation: drop the price check.
    """
    ws, uid = ea_user.workspace_id, ea_user.user_id
    monkeypatch.setattr(
        haiku_default,
        "free_model_allowlist",
        [*haiku_default.free_model_allowlist, "house-model-xyz"],
    )
    agent_id = await _agent(ws, uid, model="litellm:house-model-xyz")

    end, seen = await _execute(monkeypatch, ws, uid, agent_id, "r-unpriced")
    await _assert_refused(end, seen, backend_factory_spy, "r-unpriced")


# ---------------------------------------------------------------------------
# The launch default runs, and its run debits
# ---------------------------------------------------------------------------


async def test_default_haiku_runs_and_its_run_debits(ea_user, haiku_default, monkeypatch):
    """The default agent on the launch default ``litellm:claude-haiku-4-5``
    passes the allowlist and the price check through the provider prefix, and
    the run it makes is billed for real.

    Mutation: match or price the model without stripping the provider prefix.
    """
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.metering import service as metering

    ws, uid = ea_user.workspace_id, ea_user.user_id
    monkeypatch.setattr(metering, "load_rate_card", _rate_card)
    balance_before = await credits_service.balance_micro(ws)

    end, seen = await _execute(monkeypatch, ws, uid, await _agent(ws, uid), "r-haiku")

    assert end.event == "stream_end", end.data
    assert len(seen) == 1
    result = await metering.bill_run_now("r-haiku")
    assert result is not None
    assert result.debited is True
    assert result.micro_charged > 0
    assert await credits_service.balance_micro(ws) == balance_before - result.micro_charged


@pytest.mark.parametrize("override", ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"])
async def test_allowed_per_send_models_run(ea_user, haiku_default, monkeypatch, override):
    ws, uid = ea_user.workspace_id, ea_user.user_id

    end, seen = await _execute(
        monkeypatch, ws, uid, await _agent(ws, uid), f"r-{override}", model_override=override
    )
    assert end.event == "stream_end", end.data
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Paid workspaces, BYOK turns and the pre-launch posture are untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payer", ["paid_plan", "byok"])
async def test_paid_and_byok_turns_may_use_opus(ea_user, haiku_default, monkeypatch, payer):
    """Mutation: apply the allowlist without ``is_platform_funded_free`` or for
    ``source="byok"``."""
    ws, uid = ea_user.workspace_id, ea_user.user_id
    if payer == "paid_plan":
        await _set_plan(ws, "go")
    else:
        await _store_key(monkeypatch, ws)
    agent_id = await _agent(ws, uid)

    end, seen = await _execute(monkeypatch, ws, uid, agent_id, f"r-{payer}", model_override=OPUS)
    assert end.event == "stream_end", end.data
    assert len(seen) == 1

    resp, submitted = await _post_turn(monkeypatch, ws, uid, agent_id, model=OPUS)
    assert resp.status_code != 400, resp.text
    assert submitted


async def test_allowlist_is_off_while_credits_are_not_enforced(ea_user, monkeypatch):
    from pocketpaw_ee.cloud.credits import guards

    from pocketpaw.config import get_settings

    settings = get_settings().model_copy(
        update={"credits_enforced": False, "billing_enforced": False}
    )
    monkeypatch.setattr(guards, "get_settings", lambda: settings)
    assert await guards.free_model_refusal(ea_user.workspace_id, "platform", None, OPUS) is None


# ---------------------------------------------------------------------------
# The pure check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "allowed"),
    [
        ("claude-haiku-4-5", True),
        ("litellm:claude-haiku-4-5", True),
        ("claude-haiku-4-5-20251001", True),
        ("Claude-Haiku-4-5", True),
        ("claude-sonnet-4-6", True),
        ("litellm:claude-sonnet-4-6", True),
        ("claude-opus-4-6", False),
        ("litellm:claude-opus-4-6", False),
        ("opus", False),
        ("claude-haiku-4-5-evil", False),
        ("anthropic:claude-haiku-4-5", True),
        ("foo:claude-haiku-4-5", False),
        ("foo:bar:claude-haiku-4-5", False),
        ("litellm:litellm:claude-haiku-4-5", False),
        ("", False),
    ],
)
def test_free_model_denial(early_access_posture, model, allowed):
    from pocketpaw_ee.cloud.credits import guards

    exc = guards.free_model_denial(model)
    if allowed:
        assert exc is None
    else:
        assert exc is not None
        assert exc.code == "model.not_allowed"
        assert exc.status_code == 400
        # Copy rules: no dollar values in a refusal the user reads.
        assert "$" not in exc.message
