from pocketpaw_ee.cloud.models import ALL_DOCUMENTS
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc


def test_chat_run_doc_registered():
    assert ChatRunDoc in ALL_DOCUMENTS


def test_chat_run_doc_defaults(mongo_db):  # noqa: ARG001 — fixture forces Beanie init
    doc = ChatRunDoc(
        run_id="r1",
        workspace="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="m1",
    )
    assert doc.status == "queued"
    assert doc.partial_text == ""
    assert doc.assistant_message_id is None
