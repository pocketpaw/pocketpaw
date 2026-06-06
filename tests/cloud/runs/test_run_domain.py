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


def test_run_spec_roundtrips_surface_fields():
    """The surface hint must ride the spec across the arq pickle boundary so
    the executor can re-resolve ``surface_context`` from it."""
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
        history=[],
        intent=None,
        surface="sites",
        surface_meta={"engine": "svelte"},
    )
    restored = RunSpec.model_validate(spec.model_dump())
    assert restored == spec
    assert restored.surface == "sites"
    assert restored.surface_meta == {"engine": "svelte"}


def test_run_spec_surface_fields_default_to_legacy():
    """Older callers that don't set surface fields get the legacy shape:
    ``surface=None`` + empty ``surface_meta`` (the resolver then yields a
    GENERIC context with no deny)."""
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
        history=[],
        intent=None,
    )
    assert spec.surface is None
    assert spec.surface_meta == {}
