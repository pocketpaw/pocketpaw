# src/pocketpaw/bundled_templates/errors.py
# Created: 2026-05-25 (feat/rfc-03-v2-schema-chokepoint) — explicit
# error type the loader raises in ``strict=True`` mode so the CLI, the
# tests, and any future tooling can pattern-match on a typed exception
# instead of an opaque Pydantic ValidationError.
"""Typed error raised by the template loader's strict path.

``TemplateValidationError`` subclasses ``ValueError`` so any caller that
already handles ``ValueError`` (Pydantic's own base) keeps working. It
carries the offending ``slug`` and the underlying
``pydantic.ValidationError`` so a CLI ``template lint`` command can
render slug + per-field issues without re-parsing.
"""

from __future__ import annotations

from pydantic import ValidationError


class TemplateValidationError(ValueError):
    """Raised by :func:`pocketpaw.bundled_templates.loader.load_template`
    in ``strict=True`` mode when a template fails Pydantic validation
    against ``PocketTemplate``.

    Attributes:
        slug: The template slug that failed to validate.
        pydantic_error: The underlying ``pydantic.ValidationError``.
    """

    def __init__(self, slug: str, pydantic_error: ValidationError) -> None:
        self.slug = slug
        self.pydantic_error = pydantic_error
        super().__init__(self._format_message(slug, pydantic_error))

    @staticmethod
    def _format_message(slug: str, err: ValidationError) -> str:
        # Compose a human-readable, single-line summary that names the
        # slug + the count of issues, followed by the first issue's
        # field path + message. The full Pydantic dump is available via
        # ``self.pydantic_error`` for richer CLI output.
        errors = err.errors()
        count = len(errors)
        plural = "" if count == 1 else "s"
        head = f"template {slug!r} failed validation ({count} issue{plural})"
        if errors:
            first = errors[0]
            loc = ".".join(str(p) for p in first.get("loc", ()))
            msg = first.get("msg", "")
            return f"{head}: at {loc!r}: {msg}"
        return head


__all__ = ["TemplateValidationError"]
