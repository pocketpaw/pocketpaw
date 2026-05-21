"""kb-go scopes bundled and auto-installed by PocketPaw.

Sibling to ``pocketpaw.bundled_skills``. Where bundled_skills ships
on-demand workflow markdown for the chat agent, bundled_kb ships
**pre-compiled kb-go scopes** — kb articles, concept indexes, and
BM25 search artifacts — that get mirrored to ``~/.knowledge-base/``
on dashboard boot.

Today the bundle ships one scope, ``ripple-recipes``: 3 hand-
authored pattern recipes (sales-pipeline dashboard, customer-support
app, recipe/how-to viewer) compiled via kb-go's agent-mode flow
(``kb prepare`` + Claude as the compiler + ``kb accept``). The
recipes give the chat agent a retrieval-augmented source of polished
example compositions, retrieved by intent through PocketPaw's
existing ``_get_kb_context`` injection in ``bootstrap.context_builder``.

The bundled scopes flow into the agent context automatically when:

1. ``auto_install_bundled_kb_scopes`` is True (the default), so the
   installer mirrors the scope on boot.
2. The user's ``kb_scopes`` setting includes ``"ripple-recipes"``.
   PocketPaw defaults this when no override is set; operators with
   explicit ``POCKETPAW_KB_SCOPES`` need to add it themselves.

Adding a new bundled scope: build it via ``kb prepare`` + your
agent + ``kb accept`` against your local ``~/.knowledge-base/``,
then copy the resulting directory into ``_bundled/<scope-name>/``.
The installer discovers it via directory iteration — no code
changes needed.
"""

from pocketpaw.bundled_kb.installer import (
    KbInstallResult,
    install_bundled_kb_scopes,
)

__all__ = ["KbInstallResult", "install_bundled_kb_scopes"]
