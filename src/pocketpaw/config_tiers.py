# Field ownership registry for Settings — declares which tier owns each
# UI-exposed setting.
#
# Changes:
#   - 2026-08-16: Initial implementation. Declaration only: this module maps
#     field name -> owning tier and names the tenant-secret fields that stay
#     blocked. It builds no storage, no endpoints, and no resolver — nothing
#     reads TIER_OF yet, so shipping it changes zero runtime behaviour.

"""Who owns each ``Settings`` field.

``Settings`` is a process-global Pydantic model persisted to a single
``~/.pocketpaw/config.json``. The paw-enterprise frontend exposes 55 of its
fields across settings pages grouped under ACCOUNT and WORKSPACE headings, but
every one of them writes that same global file via ``PUT /api/v1/settings``. On
a shared deployment one user changing their theme changes it for everyone, and
"workspace" settings are not per-workspace at all.

The eventual fix is a three-tier resolver
(``user.preferences[f] ?? workspace.settings[f] ?? Settings.f``). This module is
step one of that work and only step one: it records *who owns what*, so the
storage, endpoints, and resolver that follow have one place to agree on.

The three tiers
---------------

``Tier.USER``
    Belongs to the signed-in human and follows them across workspaces — theme,
    avatar, notification and sound toggles.

``Tier.WORKSPACE``
    Belongs to the tenant: memory and embedding choices, voice and search
    providers, PII and injection scan policy, compaction tuning.

``Tier.PLATFORM``
    Belongs to the operator's box: filesystem paths, auth and rate limits,
    process behaviour, and the platform's own third-party credentials. A tenant
    must never set these.

Completeness is enforced over the UI-EXPOSED set, not all 326 fields
------------------------------------------------------------------

``TIER_OF`` deliberately holds 55 entries, not one per ``Settings`` field. The
55 are exactly the fields the frontend renders (each one carries a
``fieldKey="..."`` in ``paw-enterprise/src/routes/settings/``), and those are the
only fields a tenant can reach today. Classifying the other ~271 would be
speculation about surfaces that do not exist. The test suite enforces the
correspondence in both directions, so a new settings control added to the
frontend fails CI until its owner is declared here.

Why an unlisted field defaults to PLATFORM
------------------------------------------

``tier_of()`` returns ``Tier.PLATFORM`` for anything absent from ``TIER_OF``.
That is the safe default, and it is chosen rather than inherited: PLATFORM means
"only the operator's own config file sets this", which is precisely today's
behaviour for every field. A missing entry therefore preserves the status quo
and can never accidentally hand a tenant write access to something it should not
have. The failure mode of forgetting a field is "a tenant cannot customise it
yet" — inconvenient, and visible. The opposite default would fail silently and
in the dangerous direction.

The ``default_workspace_dir`` trap
----------------------------------

``default_workspace_dir`` renders on the *preferences* page, sitting among the
theme and notification controls that are genuinely per-user. It is not a
preference. It is the agent's working directory on the server's filesystem, so
it is PLATFORM. Page grouping in the frontend reflects where a control was
convenient to put, not who owns it — classify by what the field *does*.
``vectordb_path`` and ``media_download_dir`` are server paths for the same
reason.
"""

from __future__ import annotations

from enum import StrEnum


class Tier(StrEnum):
    """The owner of a settings field."""

    USER = "user"
    WORKSPACE = "workspace"
    PLATFORM = "platform"


# ---------------------------------------------------------------------------
# USER — follows the signed-in human across workspaces
# ---------------------------------------------------------------------------

_USER_FIELDS: frozenset[str] = frozenset(
    {
        "theme_preference",
        "user_avatar_emoji",
        "notifications_enabled",
        "sound_enabled",
        "tool_notifications_enabled",
    }
)


# ---------------------------------------------------------------------------
# PLATFORM — the operator's box. Filesystem paths, auth and rate limits,
# process behaviour, and the platform's own third-party credentials.
# ---------------------------------------------------------------------------

_PLATFORM_FIELDS: frozenset[str] = frozenset(
    {
        # Server filesystem paths. default_workspace_dir renders on the
        # preferences page — see the module docstring's trap note.
        "default_workspace_dir",
        "media_download_dir",
        "media_max_file_size_mb",
        # Auth and rate limiting.
        "session_token_ttl_hours",
        "api_rate_limit_per_key",
        # Process behaviour.
        "health_check_on_startup",
        "self_audit_enabled",
        "self_audit_schedule",
        "a2a_enabled",
        "a2a_agent_name",
        "a2a_task_timeout",
        # The platform's own third-party credentials, billed to the operator.
        "google_oauth_client_id",
        "google_oauth_client_secret",
        "spotify_client_id",
        "spotify_client_secret",
        "google_api_key",
        # OCR runs against a platform credential or a locally installed
        # tesseract binary, so the choice is the operator's.
        "ocr_provider",
    }
)


# ---------------------------------------------------------------------------
# WORKSPACE — the tenant's own choices
# ---------------------------------------------------------------------------

_WORKSPACE_FIELDS: frozenset[str] = frozenset(
    {
        # Memory and embedding.
        "memory_backend",
        "memory_use_inference",
        "vectordb_path",
        "file_auto_learn",
        "file_vector_enabled",
        "vector_store",
        "embedding_provider",
        "embedding_model",
        "mem0_llm_provider",
        "mem0_llm_model",
        "mem0_embedder_provider",
        "mem0_embedder_model",
        "mem0_auto_learn",
        # Compaction tuning.
        "compaction_recent_window",
        "compaction_char_budget",
        "compaction_llm_summarize",
        # Voice.
        "tts_provider",
        "tts_voice",
        "voice_reply_enabled",
        "stt_provider",
        "stt_model",
        "sarvam_tts_language",
        "elevenlabs_api_key",
        "sarvam_api_key",
        # Search and image generation.
        "web_search_provider",
        "tavily_api_key",
        "brave_search_api_key",
        "image_model",
        # PII and injection scan policy.
        "pii_default_action",
        "pii_scan_memory",
        "pii_scan_audit",
        "injection_scan_llm",
        "injection_scan_llm_model",
    }
)


TIER_OF: dict[str, Tier] = {
    **{field: Tier.USER for field in _USER_FIELDS},
    **{field: Tier.WORKSPACE for field in _WORKSPACE_FIELDS},
    **{field: Tier.PLATFORM for field in _PLATFORM_FIELDS},
}


# ---------------------------------------------------------------------------
# Tenant secrets — classified, but not yet offered
# ---------------------------------------------------------------------------

# These four are in ``pocketpaw.credentials.SECRET_FIELDS`` and are classified
# WORKSPACE above, which is the correct long-term ownership: they are the
# tenant's own API keys, billed to the tenant, and a tenant should be able to
# bring its own.
#
# They must NOT be offered at the tenant tier until encryption at rest is
# settled. Today's secret storage encrypts with a key derived from machine
# identity into the operator's own ~/.pocketpaw/secrets.enc — a threat model
# that assumes one operator holding their own keys. Per-tenant secrets in a
# shared database are a different threat model: many mutually-untrusting
# tenants, keys that must stay unreadable to each other and to a database
# backup. That work does not exist yet.
#
# The entries stay in the WORKSPACE mapping on purpose. The intent is recorded
# so the eventual resolver does not have to rediscover it; the exposure is
# blocked separately. Deleting them here would silently promote them.
TENANT_SECRETS_BLOCKED: frozenset[str] = frozenset(
    {
        "elevenlabs_api_key",
        "sarvam_api_key",
        "tavily_api_key",
        "brave_search_api_key",
    }
)


def tier_of(field: str) -> Tier:
    """Return the tier that owns ``field``.

    Anything not explicitly classified is PLATFORM — the safe default, which
    preserves today's behaviour. See the module docstring for why.
    """
    return TIER_OF.get(field, Tier.PLATFORM)
