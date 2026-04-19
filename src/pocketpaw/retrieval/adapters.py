# adapters.py — Concrete test + projection adapter implementations.
# Updated: feat/receive-retrieval-infra (2026-04-19) — moved here from
# soul-protocol/engine/retrieval/adapters.py as part of the 0.3.2 split.
# The `SourceAdapter` and `AsyncSourceAdapter` Protocols now live in
# soul_protocol.spec.retrieval (the language-agnostic standard); this
# file keeps only the two concrete implementations — application-layer,
# not part of the standard.
#
# Two concrete adapters live here:
#   * `MockAdapter` — returns fixed candidates and tracks invocations. Pure
#     test fixture; re-exported by __init__.py for tests that want to build
#     against it without reaching into the module path.
#   * `ProjectionAdapter` — wraps a callable so soul memory, kb, and fabric
#     can plug in without each building a class. Represents the
#     "local rebuilt view" case.
#
# External federation adapters (Drive, Salesforce, ...) live under
# src/pocketpaw/connectors/<source>/ and implement the SourceAdapter
# Protocol from the spec directly.

from __future__ import annotations

from collections.abc import Callable

from soul_protocol.spec.retrieval import Credential, RetrievalCandidate, RetrievalRequest


class MockAdapter:
    """Test adapter. Returns a fixed list, records every call.

    Parameters:
        candidates: What to return on `query`.
        delay_s: If >0, sleeps that long before returning. Lets tests
            exercise the router's per-source timeout path.
        raises: If set, raises this exception instead of returning.
    """

    supports_dataref: bool = False

    def __init__(
        self,
        candidates: list[RetrievalCandidate] | None = None,
        *,
        delay_s: float = 0.0,
        raises: Exception | None = None,
    ) -> None:
        self._candidates = candidates or []
        self._delay_s = delay_s
        self._raises = raises
        self.calls: list[tuple[RetrievalRequest, Credential | None]] = []

    def query(
        self,
        request: RetrievalRequest,
        credential: Credential | None,
    ) -> list[RetrievalCandidate]:
        self.calls.append((request, credential))
        if self._delay_s > 0:
            import time

            time.sleep(self._delay_s)
        if self._raises is not None:
            raise self._raises
        return list(self._candidates)


class ProjectionAdapter:
    """Adapter over a plain callable — the "local rebuilt view" shape.

    Soul memory, kb, and fabric all live inside the same Paw OS instance
    the router runs in. They don't need federation or credentials. A
    callable that takes the request and returns candidates is enough.
    """

    supports_dataref: bool = False

    def __init__(
        self,
        fn: Callable[[RetrievalRequest], list[RetrievalCandidate]],
    ) -> None:
        self._fn = fn

    def query(
        self,
        request: RetrievalRequest,
        credential: Credential | None,  # noqa: ARG002 — projection ignores creds
    ) -> list[RetrievalCandidate]:
        return list(self._fn(request))
