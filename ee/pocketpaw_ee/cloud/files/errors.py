"""Typed errors raised by providers and the aggregator."""

from __future__ import annotations


class FilesError(Exception):
    code: str = "files.error"
    http_status: int = 500


class ProviderUnsupported(FilesError):
    code = "files.operation_unsupported"
    http_status = 405


class CrossScopeMove(FilesError):
    code = "files.cross_scope_move"
    http_status = 409


class MountReadonly(FilesError):
    code = "files.mount_readonly"
    http_status = 403


class FilesForbidden(FilesError):
    code = "files.forbidden"
    http_status = 403


class MountNotFound(FilesError):
    code = "files.mount_not_found"
    http_status = 404


class EntryNotFound(FilesError):
    code = "files.not_found"
    http_status = 404


class NameConflict(FilesError):
    code = "files.name_conflict"
    http_status = 409


class ProviderUpstream(FilesError):
    code = "files.provider_error"
    http_status = 502
