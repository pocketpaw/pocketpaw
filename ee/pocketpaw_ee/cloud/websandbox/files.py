# files.py — Web Cursor file read/write/list + create/rename/delete RPC core.
# Created 2026-07-15 (WC-4a, feat/websandbox-file-rpc).
# Changed 2026-07-16 (WC-4c, feat/code-mode): added the file.create / file.delete
# / file.move verbs, each path-jailed like list/read/write, plus best-effort
# on_delete / on_move durability hooks (siblings of on_write) so a delete or
# rename is reflected in the overlay tier and never resurrected on restore.
#
# Socket-agnostic sibling of terminal.py's PtyBridge: this is the file-operation
# half of the session WebSocket, multiplexed alongside the terminal on the SAME
# authenticated socket. The browser editor round-trips directory listings, file
# reads, and file writes through here; ws.py just parses one JSON frame and calls
# ``FileRpc.dispatch``. Keeping the logic here (taking a DaytonaClient + a
# sandbox_id, resolving the project dir lazily) makes the whole path-jail + size
# guard unit-testable with a fake client and no socket.
#
# SECURITY — path jail (the core of this slice): every client path is relative to
# the sandbox's PROJECT DIR (``client.get_project_dir``). A path is resolved by
# LEXICALLY normalizing ``<project_dir>/<rel>`` with posixpath (collapsing ``.``
# and ``..``) and asserting the result is the project dir or lives under it.
# ``..`` escapes and absolute paths both fail that check and return a
# ``file.error`` — they NEVER reach download_file/upload_bytes. The jail is
# lexical rather than realpath-based on purpose: the VM filesystem is REMOTE, so
# resolving remote symlinks would need a shell round-trip, and it would buy no
# security here anyway — the socket is already owner-bound to the caller's OWN VM
# (license -> ws_ticket -> get_sandbox -> authorize_sandbox in ws.py), so a
# symlink inside it can only point at files the owner already fully controls via
# the terminal. The jail's job is to stop a crafted ``path`` frame from escaping
# the project dir, and lexical normalization does exactly that.
#
# Frame contract (client->server / server->client), reqId echoed for correlation:
#   file.list   {path}          -> file.list.ok   {path, entries:[{name,path,isDir,size}]}
#   file.read   {path}          -> file.read.ok   {path, content}
#   file.write  {path, content} -> file.write.ok  {path}
#   file.create {path, isDir}   -> file.create.ok {path, isDir}
#   file.delete {path}          -> file.delete.ok {path}
#   file.move   {path, toPath}  -> file.move.ok   {path, toPath}   # rename == move
#   any failure                 -> file.error     {op, message}
# ``op`` in a file.error is list|read|write|create|delete|move.
# Responses are JSON text frames; terminal output stays binary, so the two
# multiplexed streams never collide. Content is UTF-8 TEXT (binary is out of
# scope for v1); a non-UTF-8 read returns file.error rather than corrupting the
# socket. A size cap (POCKETPAW_WEBSANDBOX_MAX_FILE_KB, default 1024) bounds both
# read and write so one giant file can't blow up the frame.
from __future__ import annotations

import logging
import os
import posixpath
from collections.abc import Awaitable, Callable

from pocketpaw_ee.cloud.daytona.client import DaytonaClient
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

logger = logging.getLogger(__name__)

# Best-effort write-through callback: (rel_path, encoded_bytes) -> awaitable.
# ws.py binds this to the CM-2a′ blob-storage mirror; a bare FileRpc leaves it
# None (no durability), which is exactly the shape the WC-4a tests exercise.
OnWrite = Callable[[str, bytes], Awaitable[None]]

# Best-effort delete hook: (rel_path) -> awaitable. ws.py binds this to the
# overlay DROP so a deleted file is not resurrected from the overlay on restore.
OnDelete = Callable[[str], Awaitable[None]]

# Best-effort move hook: (src_rel, dst_rel) -> awaitable. ws.py binds this to the
# overlay RE-KEY so a renamed file replays at its new path on restore.
OnMove = Callable[[str, str], Awaitable[None]]

# File-op size cap (KB). Applies to a read's downloaded bytes AND a write's
# encoded content, so neither a huge file nor a huge payload can blow up the
# multiplexed socket frame. Override via POCKETPAW_WEBSANDBOX_MAX_FILE_KB.
_DEFAULT_MAX_FILE_KB = 1024


def _max_file_bytes() -> int:
    """Per-file size cap in bytes (``POCKETPAW_WEBSANDBOX_MAX_FILE_KB``, default 1024)."""
    raw = os.environ.get("POCKETPAW_WEBSANDBOX_MAX_FILE_KB", "").strip()
    kb = _DEFAULT_MAX_FILE_KB
    if raw:
        try:
            kb = int(raw)
        except ValueError:
            logger.warning(
                "ignoring non-numeric POCKETPAW_WEBSANDBOX_MAX_FILE_KB=%r; using %d", raw, kb
            )
    return max(kb, 1) * 1024


class FileRpcError(Exception):
    """A file op failed cleanly — carried back to the browser as a ``file.error``
    frame, never raised out of the socket loop. ``op`` is
    ``list|read|write|create|delete|move``."""

    def __init__(self, op: str, message: str) -> None:
        super().__init__(message)
        self.op = op
        self.message = message


def _jail(project_dir: str, rel_path: str, op: str) -> str:
    """Resolve ``rel_path`` under ``project_dir`` and assert it stays inside.

    Lexically normalizes ``<project_dir>/<rel_path>`` with posixpath (VM is
    Linux; use posixpath NOT os.path so this is correct when the test/host is
    Windows) and requires the result to be the project dir or live under it.
    Absolute paths and ``..`` escapes both fail the containment check. Raises
    :class:`FileRpcError` on any escape or empty path — the caller turns that
    into a ``file.error`` frame, so download/upload is never reached for a
    traversal attempt.
    """
    if not isinstance(rel_path, str):
        raise FileRpcError(op, "a 'path' (relative to the project dir) is required")

    root = posixpath.normpath(project_dir)
    # Empty string or '.' addresses the project root itself. The editor's file
    # tree lists the repo root by requesting path '' (see the frontend
    # loadTree -> listDir('')), so this MUST resolve to the jail root rather than
    # being rejected — otherwise the root listing always errors and the tree
    # shows nothing. Absolute paths and '..' escapes are still refused below.
    stripped = rel_path.strip()
    if stripped in ("", ".", "./"):
        # Listing the root is valid (the tree needs it); reading/writing the
        # directory itself is not — those still require a real file path.
        if op == "list":
            return root
        raise FileRpcError(op, "a 'path' (relative to the project dir) is required")

    # posixpath.join drops the left side entirely if rel_path is absolute, so an
    # absolute path resolves outside the root and is rejected below (belt: the
    # explicit is_absolute check makes the intent obvious).
    if posixpath.isabs(rel_path):
        raise FileRpcError(op, "absolute paths are not allowed; use a path relative to the project")

    resolved = posixpath.normpath(posixpath.join(root, rel_path))
    if resolved != root and not resolved.startswith(root + "/"):
        raise FileRpcError(op, "path escapes the project directory")
    return resolved


def _rel_entry_path(rel_dir: str, name: str) -> str:
    """Build the project-relative path of an entry ``name`` inside ``rel_dir``.

    The browser passes these straight back into a follow-up file op, so they must
    stay relative to the project root (never absolute). ``.``/empty dir -> just
    the name."""
    base = "" if rel_dir.strip() in ("", ".", "./") else rel_dir.strip().strip("/")
    joined = posixpath.normpath(posixpath.join(base, name)) if base else name
    return joined.lstrip("/")


class FileRpc:
    """File list/read/write over a Daytona VM, jailed to its project dir.

    One instance per session socket. Holds the DaytonaClient + sandbox_id (both
    already resolved + owner-authorized by ws.py) and resolves the project dir
    once, lazily. ``dispatch`` takes a parsed ``file.*`` frame and returns the
    response frame dict to send back (or ``None`` for a non-file frame, so the
    ws loop falls through to terminal handling). Op failures come back as a
    ``file.error`` frame — this class never raises into the socket loop.
    """

    def __init__(
        self,
        client: DaytonaClient,
        sandbox_id: str,
        project_dir: str | None = None,
        on_write: OnWrite | None = None,
        on_delete: OnDelete | None = None,
        on_move: OnMove | None = None,
    ) -> None:
        self._client = client
        self._sandbox_id = sandbox_id
        self._project_dir = project_dir
        # Best-effort write-through hook (CM-2a′): called with (rel_path, bytes)
        # after a successful VM write so ws.py can mirror the file to durable blob
        # storage. Never awaited in a way that can fail the save — see write_file.
        self._on_write = on_write
        # Best-effort durability siblings of on_write. on_delete(rel_path) DROPS
        # the overlay entry after a VM delete; on_move(src_rel, dst_rel) RE-KEYS
        # it after a VM move. Both are swallowed on failure so a good VM op is
        # never turned into a client error.
        self._on_delete = on_delete
        self._on_move = on_move

    async def _root(self) -> str:
        """The jail root: the pinned in-VM workspace dir (``WEBSANDBOX_WORKDIR``).

        Uses the shared constant, NOT the SDK's ``get_project_dir()`` (which
        returns ``/root`` on this image and mismatches where the repo is cloned
        and where the terminal opens). A ``project_dir`` passed to ``__init__``
        still overrides it (tests inject a fake root)."""
        if self._project_dir is None:
            self._project_dir = WEBSANDBOX_WORKDIR
        return self._project_dir

    # ── Ops (raise FileRpcError on failure) ───────────────────────────────

    async def list_dir(self, rel_path: str) -> list[dict]:
        """List a directory. Entries carry project-relative paths for the browser."""
        root = await self._root()
        abs_path = _jail(root, rel_path, "list")
        try:
            infos = await self._client.list_files(self._sandbox_id, abs_path)
        except FileNotFoundError as exc:
            raise FileRpcError("list", f"no such directory: {rel_path}") from exc
        entries: list[dict] = []
        for info in infos:
            name = getattr(info, "name", None)
            if not name:
                continue
            entries.append(
                {
                    "name": name,
                    "path": _rel_entry_path(rel_path, name),
                    "isDir": bool(getattr(info, "is_dir", False)),
                    "size": int(getattr(info, "size", 0) or 0),
                }
            )
        return entries

    async def read_file(self, rel_path: str) -> str:
        """Read a UTF-8 text file. Missing file / oversize / binary -> FileRpcError."""
        root = await self._root()
        abs_path = _jail(root, rel_path, "read")
        try:
            data = await self._client.download_file(self._sandbox_id, abs_path)
        except FileNotFoundError as exc:
            raise FileRpcError("read", f"no such file: {rel_path}") from exc
        if data is None:
            raise FileRpcError("read", f"no such file: {rel_path}")
        cap = _max_file_bytes()
        if len(data) > cap:
            raise FileRpcError(
                "read",
                f"file is {len(data) // 1024} KB, over the {cap // 1024} KB limit",
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileRpcError("read", "file is not UTF-8 text (binary is not supported)") from exc

    async def write_file(self, rel_path: str, content: str) -> None:
        """Write UTF-8 text to a file. Oversize / non-string content -> FileRpcError."""
        if not isinstance(content, str):
            raise FileRpcError("write", "'content' must be a string")
        root = await self._root()
        abs_path = _jail(root, rel_path, "write")
        data = content.encode("utf-8")
        cap = _max_file_bytes()
        if len(data) > cap:
            raise FileRpcError(
                "write",
                f"content is {len(data) // 1024} KB, over the {cap // 1024} KB limit",
            )
        await self._client.upload_bytes(self._sandbox_id, data, abs_path)

        # Write-through mirror to durable blob storage (CM-2a′), best-effort: the
        # VM write above already succeeded (that's what file.write.ok confirms), so
        # a mirror failure must NOT turn a good save into an error. Pass the
        # project-relative path so restore can replay it into the same jail slot.
        if self._on_write is not None:
            try:
                await self._on_write(rel_path.strip().lstrip("/"), data)
            except Exception:  # noqa: BLE001 — durability is best-effort; save already landed
                logger.debug("write-through mirror failed for %r", rel_path, exc_info=True)

    async def _exists(self, abs_path: str) -> bool:
        """Best-effort existence probe for a JAILED absolute path (file OR dir).

        Lists the parent directory and checks for the basename, so it detects a
        clash whether the target is a regular file or a directory (a plain
        ``download_file`` can't see a dir). A missing parent means the target
        can't exist yet, so a listing error is treated as "does not exist" — the
        create/move op then proceeds and the VM op itself is the final arbiter.
        Used to refuse clobbering an existing file on create and an existing
        destination on move.
        """
        parent, _, name = abs_path.rpartition("/")
        if not name:
            return False
        try:
            infos = await self._client.list_files(self._sandbox_id, parent or "/")
        except FileNotFoundError:
            return False
        return any(getattr(info, "name", None) == name for info in infos)

    async def create(self, rel_path: str, is_dir: bool) -> None:
        """Create a new file (empty) or directory. Refuses an existing target.

        ``is_dir`` picks ``create_folder`` vs an empty ``upload_bytes``. The
        project root is not a valid target (``_jail`` rejects empty/'.' for a
        non-list op). Refuses to clobber an existing path. A newly created FILE
        is mirrored through ``on_write`` with empty bytes so it lands in the
        overlay like a normal save; a new DIR carries no content, so no mirror.
        """
        root = await self._root()
        abs_path = _jail(root, rel_path, "create")
        if await self._exists(abs_path):
            raise FileRpcError("create", f"already exists: {rel_path}")
        if is_dir:
            await self._client.create_folder(self._sandbox_id, abs_path)
            return
        await self._client.upload_bytes(self._sandbox_id, b"", abs_path)
        if self._on_write is not None:
            try:
                await self._on_write(rel_path.strip().lstrip("/"), b"")
            except Exception:  # noqa: BLE001 — durability is best-effort; create already landed
                logger.debug("create mirror failed for %r", rel_path, exc_info=True)

    async def delete(self, rel_path: str) -> None:
        """Delete a file or directory (recursive). Refuses the project root.

        After the VM delete lands, calls ``on_delete(rel_path)`` best-effort so
        the overlay entry is dropped and restore can't resurrect the file.
        """
        if (rel_path or "").strip() in ("", ".", "./"):
            raise FileRpcError("delete", "refusing to delete the project root")
        root = await self._root()
        abs_path = _jail(root, rel_path, "delete")
        await self._client.delete_file(self._sandbox_id, abs_path, recursive=True)
        if self._on_delete is not None:
            try:
                await self._on_delete(rel_path.strip().lstrip("/"))
            except Exception:  # noqa: BLE001 — durability is best-effort; delete already landed
                logger.debug("overlay drop failed for %r", rel_path, exc_info=True)

    async def move(self, rel_path: str, to_path: str) -> None:
        """Move/rename a file or directory. Jails BOTH paths; refuses clobber.

        The source cannot be the project root, and the destination must not
        already exist. After the VM move lands, calls ``on_move(src, dst)``
        best-effort so the overlay entry is re-keyed to the new path.
        """
        if (rel_path or "").strip() in ("", ".", "./"):
            raise FileRpcError("move", "refusing to move the project root")
        root = await self._root()
        abs_src = _jail(root, rel_path, "move")
        abs_dst = _jail(root, to_path, "move")
        if await self._exists(abs_dst):
            raise FileRpcError("move", f"already exists: {to_path}")
        await self._client.move_file(self._sandbox_id, abs_src, abs_dst)
        if self._on_move is not None:
            try:
                await self._on_move(rel_path.strip().lstrip("/"), to_path.strip().lstrip("/"))
            except Exception:  # noqa: BLE001 — durability is best-effort; move already landed
                logger.debug("overlay re-key failed for %r -> %r", rel_path, to_path, exc_info=True)

    # ── Frame dispatch (never raises into the socket loop) ────────────────

    async def dispatch(self, msg: dict) -> dict | None:
        """Handle one parsed ``file.*`` frame; return the response frame to send.

        Returns ``None`` if ``msg`` is not a file frame (the ws loop then treats
        it as a terminal frame). All op failures — including a path-jail escape —
        come back as a ``file.error`` frame; this method never propagates an
        exception into the receive loop, so one bad frame can't tear the socket
        down.
        """
        mtype = msg.get("type")
        if not isinstance(mtype, str) or not mtype.startswith("file."):
            return None

        req_id = msg.get("reqId")
        req_id = req_id if isinstance(req_id, str) else ""
        path = msg.get("path")
        path = path if isinstance(path, str) else ""

        try:
            if mtype == "file.list":
                entries = await self.list_dir(path)
                return {"type": "file.list.ok", "reqId": req_id, "path": path, "entries": entries}
            if mtype == "file.read":
                content = await self.read_file(path)
                return {"type": "file.read.ok", "reqId": req_id, "path": path, "content": content}
            if mtype == "file.write":
                await self.write_file(path, msg.get("content"))
                return {"type": "file.write.ok", "reqId": req_id, "path": path}
            if mtype == "file.create":
                is_dir = bool(msg.get("isDir"))
                await self.create(path, is_dir)
                return {
                    "type": "file.create.ok",
                    "reqId": req_id,
                    "path": path,
                    "isDir": is_dir,
                }
            if mtype == "file.delete":
                await self.delete(path)
                return {"type": "file.delete.ok", "reqId": req_id, "path": path}
            if mtype == "file.move":
                to_path = msg.get("toPath")
                to_path = to_path if isinstance(to_path, str) else ""
                await self.move(path, to_path)
                return {
                    "type": "file.move.ok",
                    "reqId": req_id,
                    "path": path,
                    "toPath": to_path,
                }
            # A ``file.*`` type we don't implement — honest error, socket stays up.
            return {
                "type": "file.error",
                "reqId": req_id,
                "op": mtype.removeprefix("file."),
                "message": f"unsupported file op: {mtype}",
            }
        except FileRpcError as exc:
            return {"type": "file.error", "reqId": req_id, "op": exc.op, "message": exc.message}
        except Exception as exc:  # noqa: BLE001 — a file op must never kill the socket
            logger.warning(
                "websandbox file op failed: type=%s sandbox=%s", mtype, self._sandbox_id,
                exc_info=True,
            )
            op = mtype.removeprefix("file.") if mtype.startswith("file.") else mtype
            return {
                "type": "file.error",
                "reqId": req_id,
                "op": op,
                "message": f"file op failed: {exc}",
            }


__all__ = ["FileRpc", "FileRpcError"]
