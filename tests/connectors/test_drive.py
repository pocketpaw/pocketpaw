# Tests for the Google Drive SourceAdapter (Workstream C2).
# Created: 2026-04-16.
#
# Covers:
#   * DriveClient request plumbing (auth header, params, rate-limit retry,
#     point-in-time revision lookup, no-results is not an error).
#   * DriveSourceAdapter.query shape — candidate scope context, DataRef
#     payload, as_of pinned to point-in-time, score ordering.
#   * Token resolution precedence (credential > env > OAuth store).
#   * End-to-end with a real RetrievalRouter + InMemoryCredentialBroker,
#     asserting retrieval.query journal emission with the recorded payload.
#
# No real Drive API traffic — httpx.Client.request is stubbed with a
# scripted fake so we can walk each retry branch without httpx mocks.

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from soul_protocol.engine.journal import open_journal
from soul_protocol.engine.retrieval import (
    InMemoryCredentialBroker,
    RetrievalRouter,
)
from soul_protocol.spec.journal import Actor
from soul_protocol.spec.retrieval import (
    CandidateSource,
    RetrievalRequest,
)

from pocketpaw.connectors.drive import (
    DriveAuthError,
    DriveClient,
    DriveError,
    DriveNotFoundError,
    DriveRateLimitError,
    DriveSourceAdapter,
)
from pocketpaw.connectors.drive.auth import resolve_bearer_token

# ---------------------------------------------------------------------------
# HTTP scripting helpers — a tiny replacement for httpx_mock so we don't pull
# a new test dep into an already-heavy tree.
# ---------------------------------------------------------------------------


class FakeResponse:
    """Stands in for httpx.Response for both sync and streaming paths."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: Any = None,
        body: bytes = b"",
        raise_on_request: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self._body = body
        self._raise = raise_on_request
        self.closed = False
        self.text = body.decode("utf-8", errors="replace") if body else ""

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data

    def iter_bytes(self, chunk_size: int | None = None):  # noqa: ARG002
        yield self._body

    def close(self) -> None:
        self.closed = True


class ScriptedClient:
    """httpx.Client stand-in with an in-order script of responses."""

    def __init__(self, script: list[FakeResponse | Exception]) -> None:
        # ``itertools.chain`` + final response lets tests assert retry pacing.
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> FakeResponse:
        self.calls.append(
            {"method": method, "url": url, "params": params, "headers": headers, "json": json}
        )
        if not self._script:
            raise AssertionError("ScriptedClient ran out of scripted responses")
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def build_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        return {"method": method, "url": url, **kwargs}

    def send(self, request: dict[str, Any], *, stream: bool = False):  # noqa: ARG002
        return self.request(request["method"], request["url"])

    def close(self) -> None:
        pass


def _ts(year: int = 2026, month: int = 4, day: int = 1, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _actor() -> Actor:
    return Actor(kind="agent", id="did:soul:test-agent")


# ---------------------------------------------------------------------------
# DriveClient tests
# ---------------------------------------------------------------------------


class TestDriveClient:
    def test_empty_token_raises(self) -> None:
        with pytest.raises(DriveAuthError):
            DriveClient(token="")

    def test_list_files_happy_path(self) -> None:
        script = [
            FakeResponse(
                json_data={
                    "files": [
                        {
                            "id": "file_1",
                            "name": "Q3 forecast",
                            "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2026-04-01T12:00:00.000Z",
                            "size": "1024",
                            "webViewLink": "https://drive.google.com/file_1",
                            "headRevisionId": "rev-latest",
                        }
                    ]
                }
            )
        ]
        http = ScriptedClient(script)
        client = DriveClient(token="fake-token", http=http)
        files = client.list_files(query="fullText contains 'forecast'", page_size=20)

        assert len(files) == 1
        assert files[0].id == "file_1"
        assert files[0].revision_id == "rev-latest"
        assert http.calls[0]["headers"]["Authorization"] == "Bearer fake-token"
        assert http.calls[0]["params"]["q"] == "fullText contains 'forecast'"
        assert http.calls[0]["params"]["pageSize"] == 20

    def test_list_files_no_results_returns_empty_list(self) -> None:
        http = ScriptedClient([FakeResponse(json_data={"files": []})])
        client = DriveClient(token="fake-token", http=http)
        assert client.list_files(query="name contains 'nope'") == []

    def test_401_raises_drive_auth_error(self) -> None:
        http = ScriptedClient([FakeResponse(status_code=401, json_data={"error": "unauth"})])
        client = DriveClient(token="stale", http=http)
        with pytest.raises(DriveAuthError):
            client.list_files()

    def test_404_raises_not_found(self) -> None:
        http = ScriptedClient([FakeResponse(status_code=404, json_data={})])
        client = DriveClient(token="ok", http=http)
        with pytest.raises(DriveNotFoundError):
            client.get_file("missing")

    def test_429_triggers_backoff_and_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_calls: list[float] = []
        monkeypatch.setattr(
            "pocketpaw.connectors.drive.client.time.sleep",
            lambda s: sleep_calls.append(s),
        )
        script = [
            FakeResponse(status_code=429, json_data={"error": {"message": "slow down"}}),
            FakeResponse(status_code=429, json_data={"error": {"message": "slow down"}}),
            FakeResponse(json_data={"files": [{"id": "f1", "name": "after retry"}]}),
        ]
        http = ScriptedClient(script)
        client = DriveClient(token="ok", http=http, base_backoff_s=0.01, max_backoff_s=0.1)
        files = client.list_files()

        assert len(files) == 1
        assert files[0].id == "f1"
        assert len(sleep_calls) == 2  # backed off twice, succeeded on 3rd attempt

    def test_403_quota_reason_also_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pocketpaw.connectors.drive.client.time.sleep", lambda s: None)
        quota_body = {
            "error": {
                "errors": [{"reason": "userRateLimitExceeded", "message": "quota"}],
                "code": 403,
            }
        }
        script = [
            FakeResponse(status_code=403, json_data=quota_body),
            FakeResponse(json_data={"files": []}),
        ]
        http = ScriptedClient(script)
        client = DriveClient(token="ok", http=http, base_backoff_s=0.01)
        client.list_files()  # should not raise
        assert len(http.calls) == 2

    def test_rate_limit_budget_exhausted_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pocketpaw.connectors.drive.client.time.sleep", lambda s: None)
        script = [FakeResponse(status_code=429, json_data={}) for _ in range(6)]
        http = ScriptedClient(script)
        client = DriveClient(token="ok", http=http, max_retries=2, base_backoff_s=0.01)
        with pytest.raises(DriveRateLimitError):
            client.list_files()

    def test_other_4xx_raises_generic_drive_error(self) -> None:
        http = ScriptedClient(
            [FakeResponse(status_code=500, body=b"boom", json_data={"error": "boom"})]
        )
        client = DriveClient(token="ok", http=http, max_retries=0)
        with pytest.raises(DriveError):
            client.list_files()

    def test_revision_at_picks_most_recent_before_point(self) -> None:
        script = [
            FakeResponse(
                json_data={
                    "revisions": [
                        {"id": "r1", "modifiedTime": "2026-03-01T00:00:00Z"},
                        {"id": "r2", "modifiedTime": "2026-03-15T00:00:00Z"},
                        {"id": "r3", "modifiedTime": "2026-04-10T00:00:00Z"},
                    ]
                }
            )
        ]
        http = ScriptedClient(script)
        client = DriveClient(token="ok", http=http)

        chosen = client.revision_at("file_1", _ts(2026, 4, 1))
        assert chosen is not None
        assert chosen.id == "r2"

    def test_revision_at_returns_none_when_all_revisions_are_future(self) -> None:
        script = [
            FakeResponse(
                json_data={"revisions": [{"id": "r1", "modifiedTime": "2027-01-01T00:00:00Z"}]}
            )
        ]
        http = ScriptedClient(script)
        client = DriveClient(token="ok", http=http)
        assert client.revision_at("file_1", _ts(2026, 4, 1)) is None

    def test_revision_at_requires_aware_timestamp(self) -> None:
        client = DriveClient(token="ok", http=ScriptedClient([]))
        with pytest.raises(ValueError):
            client.revision_at("file_1", datetime(2026, 4, 1))  # naive


# ---------------------------------------------------------------------------
# DriveSourceAdapter tests
# ---------------------------------------------------------------------------


class FakeDriveClient:
    """Minimal client stub exposing the methods the adapter calls."""

    def __init__(
        self,
        *,
        files: list[dict[str, Any]] | None = None,
        revisions: dict[str, list[dict[str, Any]]] | None = None,
        raise_on_list: Exception | None = None,
    ) -> None:
        from pocketpaw.connectors.drive.client import DriveFile, DriveRevision

        self._files = [DriveFile.from_api(f) for f in (files or [])]
        self._revisions = revisions or {}
        self._raise_on_list = raise_on_list
        self.list_calls: list[tuple[str | None, int]] = []
        self.revision_calls: list[tuple[str, datetime]] = []
        self._DriveRevision = DriveRevision

    def list_files(self, *, query: str | None = None, page_size: int = 20, **kwargs: Any):
        if self._raise_on_list is not None:
            raise self._raise_on_list
        self.list_calls.append((query, page_size))
        return list(self._files)

    def revision_at(self, file_id: str, point_in_time: datetime):
        self.revision_calls.append((file_id, point_in_time))
        revs = [self._DriveRevision.from_api(r) for r in self._revisions.get(file_id, [])]
        # simple "most recent <= point_in_time" without reimplementing client logic
        best = None
        for rev in revs:
            ts = datetime.fromisoformat(rev.modified_time.replace("Z", "+00:00"))
            if ts <= point_in_time and (
                best is None
                or ts > datetime.fromisoformat(best.modified_time.replace("Z", "+00:00"))
            ):
                best = rev
        return best


def _sample_files() -> list[dict[str, Any]]:
    return [
        {
            "id": "file_1",
            "name": "Q3 forecast",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-04-05T10:00:00Z",
            "size": "2048",
            "webViewLink": "https://drive.google.com/file_1",
            "headRevisionId": "rev-now",
        },
        {
            "id": "file_2",
            "name": "forecast notes",
            "mimeType": "text/plain",
            "modifiedTime": "2026-04-04T09:00:00Z",
            "webViewLink": "https://drive.google.com/file_2",
        },
    ]


def _make_request(query: str, scopes: list[str] | None = None, limit: int = 10) -> RetrievalRequest:
    return RetrievalRequest(
        query=query,
        actor=_actor(),
        scopes=scopes or ["org:sales:*"],
        limit=limit,
        strategy="parallel",
        timeout_s=5.0,
    )


class TestDriveSourceAdapter:
    def test_supports_dataref_is_true(self) -> None:
        assert DriveSourceAdapter.supports_dataref is True

    def test_query_returns_dataref_candidates(self) -> None:
        fake = FakeDriveClient(files=_sample_files())
        adapter = DriveSourceAdapter(
            client_factory=lambda token: fake,
            env={"GOOGLE_OAUTH_TOKEN": "env-token"},
        )
        request = _make_request("Q3 forecast")
        candidates = adapter.query(request, credential=None)

        assert len(candidates) == 2
        payload = candidates[0].content
        assert payload["kind"] == "dataref"
        assert payload["source"] == "drive"
        assert payload["id"] == "file_1"
        assert payload["scopes"] == ["org:sales:*"]
        # First candidate must rank higher than the second under position scoring.
        assert candidates[0].score is not None
        assert candidates[1].score is not None
        assert candidates[0].score > candidates[1].score
        # Without a point-in-time, as_of should be "now-ish" and cached=False.
        assert candidates[0].cached is False

    def test_query_translates_free_text_to_fulltext(self) -> None:
        fake = FakeDriveClient(files=[])
        adapter = DriveSourceAdapter(
            client_factory=lambda token: fake,
            env={"GOOGLE_OAUTH_TOKEN": "env-token"},
        )
        adapter.query(_make_request("revenue"), credential=None)
        assert fake.list_calls[0][0] == "fullText contains 'revenue'"

    def test_query_passes_native_drive_syntax_through(self) -> None:
        fake = FakeDriveClient(files=[])
        adapter = DriveSourceAdapter(
            client_factory=lambda token: fake,
            env={"GOOGLE_OAUTH_TOKEN": "env-token"},
        )
        adapter.query(
            _make_request("name contains 'forecast' and mimeType='text/plain'"),
            credential=None,
        )
        assert "mimeType" in fake.list_calls[0][0]

    def test_query_empty_results_is_not_error(self) -> None:
        fake = FakeDriveClient(files=[])
        adapter = DriveSourceAdapter(
            client_factory=lambda token: fake,
            env={"GOOGLE_OAUTH_TOKEN": "env-token"},
        )
        result = adapter.query(_make_request("anything"), credential=None)
        assert result == []

    def test_query_point_in_time_pins_revision_and_as_of(self) -> None:
        fake = FakeDriveClient(
            files=[_sample_files()[0]],
            revisions={
                "file_1": [
                    {"id": "rev-old", "modifiedTime": "2026-03-01T00:00:00Z"},
                    {"id": "rev-mid", "modifiedTime": "2026-03-15T00:00:00Z"},
                    {"id": "rev-new", "modifiedTime": "2026-04-10T00:00:00Z"},
                ]
            },
        )
        adapter = DriveSourceAdapter(
            client_factory=lambda token: fake,
            env={"GOOGLE_OAUTH_TOKEN": "env-token"},
        )
        request = _make_request("@at=2026-04-01T00:00:00Z | Q3 forecast")
        candidates = adapter.query(request, credential=None)

        assert len(candidates) == 1
        payload = candidates[0].content
        assert payload["revision_id"] == "rev-mid"
        assert candidates[0].as_of == _ts(2026, 4, 1, 0)
        assert fake.revision_calls == [("file_1", _ts(2026, 4, 1, 0))]

    def test_query_uses_credential_token_when_provided(self) -> None:
        from soul_protocol.engine.retrieval import InMemoryCredentialBroker

        broker = InMemoryCredentialBroker()
        credential = broker.acquire("drive", ["org:sales:*"])

        captured: dict[str, str] = {}

        def factory(token: str):
            captured["token"] = token
            return FakeDriveClient(files=[])

        adapter = DriveSourceAdapter(client_factory=factory, env={})
        adapter.query(_make_request("anything"), credential=credential)
        assert captured["token"] == credential.token

    def test_query_raises_auth_error_when_no_token_anywhere(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch the name the adapter actually imported (source.py captured
        # a reference at import time). With an empty env AND the token
        # resolver short-circuited, we should bubble DriveAuthError so the
        # router can record it as ``sources_failed``.
        from pocketpaw.connectors.drive import source as source_mod

        def stub(credential, *, env=None):  # noqa: ARG001
            raise DriveAuthError("no token")

        monkeypatch.setattr(source_mod, "resolve_bearer_token", stub)

        adapter = DriveSourceAdapter(
            client_factory=lambda token: FakeDriveClient(files=[]),
            env={},
        )
        with pytest.raises(DriveAuthError):
            adapter.query(_make_request("anything"), credential=None)


# ---------------------------------------------------------------------------
# resolve_bearer_token precedence tests
# ---------------------------------------------------------------------------


class TestResolveBearerToken:
    def test_credential_wins_over_env(self) -> None:
        from soul_protocol.engine.retrieval import InMemoryCredentialBroker

        broker = InMemoryCredentialBroker()
        cred = broker.acquire("drive", ["org:sales:*"])
        token = resolve_bearer_token(cred, env={"GOOGLE_OAUTH_TOKEN": "env-value"})
        assert token == cred.token

    def test_env_used_when_no_credential(self) -> None:
        token = resolve_bearer_token(None, env={"GOOGLE_OAUTH_TOKEN": "env-value"})
        assert token == "env-value"

    def test_raises_auth_error_when_nothing_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the integrations import path to raise so we exercise the
        # "no source left" branch deterministically.
        monkeypatch.setitem(
            __import__("sys").modules,
            "pocketpaw.integrations.oauth",
            None,
        )
        with pytest.raises(DriveAuthError):
            resolve_bearer_token(None, env={})


# ---------------------------------------------------------------------------
# End-to-end: RetrievalRouter + DriveSourceAdapter + journal emission
# ---------------------------------------------------------------------------


class TestRouterIntegration:
    @pytest.fixture
    def journal(self, tmp_path: Path):
        j = open_journal(tmp_path / "journal.db")
        yield j
        j.close()

    def test_router_dispatches_to_drive_adapter_and_emits_query_event(self, journal) -> None:
        broker = InMemoryCredentialBroker(journal=journal)
        router = RetrievalRouter(journal=journal, broker=broker)

        fake = FakeDriveClient(files=_sample_files())
        adapter = DriveSourceAdapter(
            client_factory=lambda token: fake,
            env={"GOOGLE_OAUTH_TOKEN": "unused"},  # broker will hand a real token
        )
        router.register_source(
            CandidateSource(
                name="drive",
                kind="dataref",
                scopes=["org:sales:*"],
                adapter_ref="pocketpaw.connectors.drive:DriveSourceAdapter",
            ),
            adapter,
        )

        result = router.dispatch(_make_request("Q3 forecast"))

        # Router produced candidates with the DataRef shape.
        assert len(result.candidates) == 2
        assert result.candidates[0].content["kind"] == "dataref"
        assert result.sources_queried == ["drive"]
        assert result.sources_failed == []

        # Journal captured retrieval.query + the three broker lifecycle events
        # (acquired/used — the broker also emits on acquire+use when a journal
        # is attached, we just assert retrieval.query is present).
        events = journal.query(limit=50)
        actions = [e.action for e in events]
        assert "retrieval.query" in actions
        assert "credential.acquired" in actions  # dataref kind triggers the broker
        assert "credential.used" in actions

        query_event = next(e for e in events if e.action == "retrieval.query")
        payload = query_event.payload
        assert payload["query"] == "Q3 forecast"
        assert payload["sources_queried"] == ["drive"]
        assert payload["candidate_count"] == 2

    def test_router_records_auth_failure_as_sources_failed(self, journal) -> None:
        broker = InMemoryCredentialBroker(journal=journal)
        router = RetrievalRouter(journal=journal, broker=broker)

        class FailingAdapter(DriveSourceAdapter):
            def query(self, request, credential):  # type: ignore[override]
                raise DriveAuthError("token rejected")

        adapter = FailingAdapter(
            client_factory=lambda token: FakeDriveClient(files=[]),
            env={"GOOGLE_OAUTH_TOKEN": "ignored"},
        )
        router.register_source(
            CandidateSource(
                name="drive",
                kind="dataref",
                scopes=["org:sales:*"],
                adapter_ref="pocketpaw.connectors.drive:DriveSourceAdapter",
            ),
            adapter,
        )

        result = router.dispatch(_make_request("anything"))
        assert result.candidates == []
        assert len(result.sources_failed) == 1
        failed_name, failed_reason = result.sources_failed[0]
        assert failed_name == "drive"
        assert "DriveAuthError" in failed_reason


# ---------------------------------------------------------------------------
# Smoke — make sure the module exposes the expected public API.
# ---------------------------------------------------------------------------


def test_public_api_exports() -> None:
    import pocketpaw.connectors.drive as mod

    assert hasattr(mod, "DriveSourceAdapter")
    assert hasattr(mod, "DriveClient")
    assert hasattr(mod, "DriveAuthError")
    assert hasattr(mod, "DriveRateLimitError")
    assert hasattr(mod, "DriveNotFoundError")
    assert hasattr(mod, "resolve_bearer_token")
