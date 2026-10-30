# share_store.py — FL-12b "Public share links".
#
# Workspace-scoped store for ShareLink documents (mirrors MongoFileStore's
# tenant-filter discipline). Every WRITE and owner-side read carries a
# ``workspace`` filter so a create/revoke can never cross tenants. The one
# deliberate exception is ``get_by_token`` — the public GET /share/{token}
# route has NO workspace principal (it is token-gated, not auth-gated), so the
# lookup keys ONLY on the unguessable token. The token itself carries the
# workspace binding (``ShareLink.workspace_id``), which the public route then
# replays into presigned_get; there is no way to widen scope through it.

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.cloud.uploads.share_models import ShareLink


class ShareLinkStore:
    """Workspace-scoped store for public file share links."""

    async def create(
        self,
        *,
        file_id: str,
        workspace: str,
        owner_id: str,
    ) -> ShareLink:
        """Mint and persist a new share link for ``file_id`` in ``workspace``."""
        link = ShareLink(
            file_id=file_id,
            workspace_id=workspace,
            owner_id=owner_id,
        )
        await link.insert()
        return link

    async def get_by_token(self, token: str) -> ShareLink | None:
        """Resolve a link by its token ALONE (public route).

        No workspace filter by design — the public GET has no tenant principal.
        The token is unique and unguessable; the returned row carries its own
        ``workspace_id`` which the caller uses to mint the download. Callers
        MUST still check ``is_active()`` before serving.
        """
        return await ShareLink.find_one(ShareLink.token == token)

    async def get_owner_scoped(
        self,
        token: str,
        *,
        file_id: str,
        workspace: str,
    ) -> ShareLink | None:
        """Resolve a link for owner-side operations (revoke).

        Bound to ``(token, file_id, workspace)`` so an owner in workspace A can
        never revoke a link that belongs to file B or workspace B.
        """
        return await ShareLink.find_one(
            ShareLink.token == token,
            ShareLink.file_id == file_id,
            ShareLink.workspace_id == workspace,
        )

    async def revoke(
        self,
        token: str,
        *,
        file_id: str,
        workspace: str,
    ) -> ShareLink | None:
        """Mark a link revoked, workspace + file scoped. Returns the row or None.

        Idempotent: revoking an already-revoked link is a no-op that still
        returns the row.
        """
        link = await self.get_owner_scoped(token, file_id=file_id, workspace=workspace)
        if link is None:
            return None
        if not link.revoked:
            link.revoked = True
            await link.save()
        return link

    async def list_for_file(
        self,
        *,
        file_id: str,
        workspace: str,
    ) -> list[ShareLink]:
        """All (active + inactive) links for one file, workspace-scoped."""
        return await ShareLink.find(
            ShareLink.file_id == file_id,
            ShareLink.workspace_id == workspace,
        ).to_list()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)
