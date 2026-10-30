# adapters.py — Application-layer SourceAdapter implementations.
# Created: 2026-06-02 (feat/retrieval-rehome, #1327) — re-homed from
# soul-protocol's deleted ``engine/retrieval/adapters.py``. soul-protocol 0.4.0
# (#179) kept the ``SourceAdapter`` / ``AsyncSourceAdapter`` *protocols* in
# ``soul_protocol.spec.retrieval`` but deleted the concrete adapters — they are
# application-layer, not spec.
#
# What ports here: ``ProjectionAdapter`` — the "local rebuilt view" shape.
# Soul memory, kb, and fabric all live inside the same Paw OS instance the
# router runs in; they don't need federation or credentials, so a plain
# callable that takes the request and returns candidates is enough. The old
# module also carried a ``MockAdapter`` test helper; that intentionally does
# NOT port (test fixtures belong in the test tree, and the Drive connector +
# its own ``FakeDriveClient`` already cover the dataref path).
#
# Adapted to the 0.4.0 surface: ``Credential`` / ``RetrievalCandidate`` /
# ``RetrievalRequest`` import from ``soul_protocol.spec.retrieval`` (the old
# code imported ``Credential`` from the sibling ``.broker`` and the rest from
# spec). No behavioral change — the callable contract is identical.

from __future__ import annotations

from collections.abc import Callable

from soul_protocol.spec.retrieval import Credential, RetrievalCandidate, RetrievalRequest


class ProjectionAdapter:
    """Adapter over a plain callable — the "local rebuilt view" shape.

    Soul memory, kb, and fabric all live inside the same Paw OS instance the
    router runs in. They don't need federation or credentials. A callable that
    takes the request and returns candidates is enough.
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
