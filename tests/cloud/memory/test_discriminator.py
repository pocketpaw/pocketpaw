"""Session and Message context_type discriminator validator tests."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.message import Message
from pocketpaw_ee.cloud.models.session import Session


class TestMessageDiscriminator:
    """Validator runs on construction before Beanie collection lookup.

    The ``_raises`` tests don't need Beanie because the model_validator fires
    before the collection check. The ``_passes`` tests need Beanie initialized
    (via ``beanie_memory_db`` fixture) because Beanie's ``__init__`` requires
    a bound collection on success.
    """

    def test_group_message_requires_group_field(self):
        with pytest.raises(ValueError, match="group message must have group set"):
            Message(context_type="group", sender="u1", content="hi")

    def test_group_message_cannot_carry_session_key(self):
        with pytest.raises(ValueError, match="must not set session_key"):
            Message(
                context_type="group",
                group="g1",
                sender="u1",
                content="hi",
                session_key="s1",
            )

    def test_group_message_cannot_carry_role(self):
        with pytest.raises(ValueError, match="must not set role"):
            Message(
                context_type="group",
                group="g1",
                sender="u1",
                content="hi",
                role="user",
            )

    def test_pocket_message_requires_session_key(self):
        with pytest.raises(ValueError, match="must have session_key"):
            Message(context_type="pocket", role="user", content="hi")

    def test_pocket_message_requires_valid_role(self):
        # Pydantic's Literal validator rejects bogus role values before our
        # model_validator runs; either layer is acceptable.
        with pytest.raises(Exception):  # noqa: B017  — pydantic/ValueError both OK
            Message(
                context_type="pocket",
                session_key="s1",
                role="not-a-role",  # type: ignore[arg-type]
                content="hi",
            )

    def test_pocket_message_cannot_carry_group(self):
        with pytest.raises(ValueError, match="must not have group"):
            Message(
                context_type="pocket",
                session_key="s1",
                role="user",
                group="g1",
                content="hi",
            )

    def test_pocket_message_cannot_carry_mentions(self):
        from pocketpaw_ee.cloud.models.message import Mention

        with pytest.raises(ValueError, match="must not have mentions"):
            Message(
                context_type="pocket",
                session_key="s1",
                role="user",
                content="hi",
                mentions=[Mention(type="user", id="u1", display_name="@u")],
            )

    async def test_valid_group_message_passes(self, beanie_memory_db):
        m = Message(context_type="group", group="g1", sender="u1", content="hello")
        assert m.context_type == "group"
        assert m.group == "g1"

    async def test_valid_pocket_message_passes(self, beanie_memory_db):
        m = Message(context_type="pocket", session_key="s1", role="assistant", content="hello")
        assert m.context_type == "pocket"
        assert m.role == "assistant"

    async def test_context_type_inferred_when_absent_group(self, beanie_memory_db):
        # Legacy constructor — no context_type, but group is set → group.
        m = Message(group="g1", sender="u1", content="legacy")
        assert m.context_type == "group"

    async def test_context_type_inferred_when_absent_pocket(self, beanie_memory_db):
        # Legacy constructor — session_key + role set → pocket.
        m = Message(session_key="s1", role="user", content="agent-memory")
        assert m.context_type == "pocket"


class TestSessionDiscriminator:
    def test_group_session_requires_group(self):
        with pytest.raises(ValueError, match="group session must have group"):
            Session(
                sessionId="s1",
                context_type="group",
                workspace="w1",
                owner="u1",
            )

    def test_group_session_cannot_carry_pocket(self):
        with pytest.raises(ValueError, match="must not have pocket"):
            Session(
                sessionId="s1",
                context_type="group",
                group="g1",
                pocket="p1",
                workspace="w1",
                owner="u1",
            )

    def test_pocket_session_cannot_carry_group(self):
        with pytest.raises(ValueError, match="pocket session must not have group"):
            Session(
                sessionId="s1",
                context_type="pocket",
                group="g1",
                workspace="w1",
                owner="u1",
            )

    async def test_valid_group_session_passes(self, beanie_memory_db):
        s = Session(
            sessionId="s1",
            context_type="group",
            group="g1",
            workspace="w1",
            owner="u1",
        )
        assert s.context_type == "group"

    async def test_valid_pocket_session_passes(self, beanie_memory_db):
        s = Session(
            sessionId="s1",
            context_type="pocket",
            pocket="p1",
            workspace="w1",
            owner="u1",
        )
        assert s.context_type == "pocket"

    async def test_context_type_inferred_from_group_field(self, beanie_memory_db):
        s = Session(sessionId="s1", group="g1", workspace="w1", owner="u1")
        assert s.context_type == "group"

    async def test_context_type_defaults_to_pocket_when_no_group(self, beanie_memory_db):
        s = Session(sessionId="s1", workspace="w1", owner="u1")
        assert s.context_type == "pocket"
