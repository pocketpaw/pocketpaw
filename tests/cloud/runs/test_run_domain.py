import pydantic
import pytest
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec


def test_run_spec_roundtrips_json():
    spec = RunSpec(
        run_id="r1",
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="m1",
        content="hello",
        history=[{"role": "user", "content": "hi"}],
        intent=None,
    )
    restored = RunSpec.model_validate(spec.model_dump())
    assert restored == spec


def test_run_spec_requires_tenancy():
    with pytest.raises(pydantic.ValidationError):
        RunSpec(run_id="r1")  # missing workspace_id etc.
