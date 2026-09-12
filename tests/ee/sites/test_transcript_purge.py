# tests/ee/sites/test_transcript_purge.py — a destroyed site forgets what its
# visitors said to it (sites lifecycle wave 4).
#
# Created 2026-09-12 (feat/sites-pause).
#
# THE BUG THIS CLOSES was a deliberate refusal with a real reason: the sites package
# would not reach into ``chat_runs`` and the Paw Bar tables, because that puts a
# second writer on rows only that surface knows the shape of. The refusal was right
# about the seam and wrong about the outcome — the rows survived the site, so a
# visitor's free text outlived the tenant's reason to hold it.
#
# So what is under test is mostly the SCOPE of the delete, not that it happens. Three
# predicates narrow it, and each one, removed, destroys something it must not:
#
#   * ``workspace``    — removed, one tenant's delete reaches another's transcripts.
#   * ``context_type`` — removed, deleting a WEBSITE destroys the owner's own authed
#                        chat history with the same pocket's agent.
#   * ``scope_id``     — removed, a sibling site's visitors are forgotten too.

from __future__ import annotations

import pytest
from pocketpaw_ee.paw_bar import purge as paw_bar_purge


class _DeleteResult:
    def __init__(self, n):
        self.deleted_count = n


class _FakeRunCollection:
    """Stands in for ``ChatRunDoc``, holding rows as plain dicts.

    ``find(query).delete()`` applies the query by equality across every key, which is
    exactly what Mongo does for these three, and is what lets a missing predicate
    show up as extra rows deleted rather than as a mock that agreed with itself.
    """

    def __init__(self, rows):
        self.rows = list(rows)
        self.last_query: dict | None = None

    def find(self, query):
        self.last_query = dict(query)
        outer = self

        class _Cursor:
            async def delete(self):
                keep, gone = [], 0
                for row in outer.rows:
                    if all(row.get(k) == v for k, v in query.items()):
                        gone += 1
                    else:
                        keep.append(row)
                outer.rows = keep
                return _DeleteResult(gone)

        return _Cursor()


class _Widget:
    def __init__(self, wid):
        self.id = wid


class _FakeStore:
    def __init__(self, widgets):
        self._widgets = widgets
        self.purged: list[tuple[str, str | None]] = []
        self.list_args: dict | None = None

    async def list_widgets(self, pocket_id=None, workspace_id=None, **_kw):
        self.list_args = {"pocket_id": pocket_id, "workspace_id": workspace_id}
        return list(self._widgets)

    async def purge_widget_threads(self, widget_id, *, workspace_id=None):
        self.purged.append((widget_id, workspace_id))
        return {"owner_messages": 3, "conversations": 2}


def _rows():
    return [
        # This site's visitors — the rows that must go.
        {"workspace": "w1", "context_type": "concierge", "scope_id": "pk1", "who": "visitor-a"},
        {"workspace": "w1", "context_type": "concierge", "scope_id": "pk1", "who": "visitor-b"},
        # The OWNER's own authed conversation with the same pocket's agent. Deleting a
        # website must not delete the owner's chat history.
        {"workspace": "w1", "context_type": "pocket", "scope_id": "pk1", "who": "owner"},
        # A sibling site in the same workspace.
        {"workspace": "w1", "context_type": "concierge", "scope_id": "pk2", "who": "sibling"},
        # Another tenant's site entirely.
        {"workspace": "w2", "context_type": "concierge", "scope_id": "pk1", "who": "other-tenant"},
    ]


@pytest.fixture
def wired(monkeypatch):
    runs = _FakeRunCollection(_rows())
    store = _FakeStore([_Widget("wid-1")])

    class _Models:
        ChatRunDoc = runs

    monkeypatch.setattr(paw_bar_purge, "_store", lambda: store)
    monkeypatch.setattr(paw_bar_purge, "_concierge_context_type", lambda: "concierge")
    import sys
    import types

    module = types.ModuleType("pocketpaw_ee.cloud.models.chat_run")
    module.ChatRunDoc = runs
    monkeypatch.setitem(sys.modules, "pocketpaw_ee.cloud.models.chat_run", module)
    return runs, store


async def test_the_purge_takes_this_sites_visitors_and_nothing_else(wired):
    runs, _store = wired
    counts = await paw_bar_purge.purge_site_transcripts(workspace_id="w1", pocket_id="pk1")

    assert counts["runs"] == 2
    survivors = sorted(r["who"] for r in runs.rows)
    assert survivors == ["other-tenant", "owner", "sibling"]


async def test_all_three_predicates_are_in_the_query(wired):
    """Asserted on the QUERY because a filter applied in Python would already have
    READ the other tenant's rows — the leak happens before the delete does."""
    runs, _store = wired
    await paw_bar_purge.purge_site_transcripts(workspace_id="w1", pocket_id="pk1")
    assert runs.last_query == {
        "workspace": "w1",
        "context_type": "concierge",
        "scope_id": "pk1",
    }


async def test_the_sqlite_half_is_purged_through_the_surfaces_own_store(wired):
    """A transcript is three stores. The out-of-band table holds VISITOR lines that
    arrived while the bot was muted — free text with no run behind it, and the only
    copy — so a purge that stopped at ``chat_runs`` leaves personal data alive."""
    _runs, store = wired
    counts = await paw_bar_purge.purge_site_transcripts(workspace_id="w1", pocket_id="pk1")

    assert store.list_args == {"pocket_id": "pk1", "workspace_id": "w1"}
    assert store.purged == [("wid-1", "w1")]
    assert counts["owner_messages"] == 3
    assert counts["conversations"] == 2


async def test_an_empty_scope_refuses_rather_than_matching_everything(wired):
    """Mutation: replace the raise with ``return {}``. An empty pocket would make the
    run query match every concierge conversation in the tenant."""
    for workspace_id, pocket_id in (("", "pk1"), ("w1", "")):
        with pytest.raises(ValueError):
            await paw_bar_purge.purge_site_transcripts(
                workspace_id=workspace_id, pocket_id=pocket_id
            )


def test_the_context_type_comes_from_the_enum_that_defines_it():
    """Three literals spell "concierge" in this tree. This one is derived, so a rename
    of ``ScopeKind.CONCIERGE`` cannot leave the purge matching nothing — which would
    be a silent no-op reporting zero rows, indistinguishable from a clean site."""
    from pocketpaw_ee.cloud.chat.agent_service import ScopeKind

    assert paw_bar_purge._concierge_context_type() == ScopeKind.CONCIERGE.value


def test_purge_site_records_hands_the_pocket_id_to_the_paw_bar_seam():
    """Mutation: drop ``pocket_id`` from the ``purge_records`` call in delete_job.

    A concierge run has never carried a site id, so without the pocket the purge has
    nothing to scope on — and the fallback is to skip, which means the transcripts
    quietly survive the delete exactly as they did before this shipped.
    """
    import inspect

    from pocketpaw_ee.sites import delete_job, service

    assert "pocket_id" in inspect.signature(service.purge_site_records).parameters
    deps_src = inspect.getsource(delete_job._DeleteDeps)
    assert "pocket_id=self.pocket_id" in deps_src

    records_src = inspect.getsource(service.purge_site_records)
    assert "purge_site_transcripts" in records_src
    # Skipped, never guessed, when there is no pocket to name.
    assert "if pocket_id:" in records_src
