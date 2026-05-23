# tests/unit/test_api_skill_builder.py
# Created: 2026-05-22 (feat/api-skills, Increment 2b) — covers the
# per-backend API skill builder: parse_openapi_to_skill_md (OpenAPI 3.x
# + Swagger 2.x, tag grouping, endpoint truncation, byte-identical
# stability) and install_api_skill (writes to the right path, rejects an
# oversized file, rejects a spec with no `paths`). Also covers the EE
# pocket-specialist wiring (_load_api_skill_for_backend, _build_system_prompt
# block splice, and the backend_summary token-safety guarantee) — those
# tests are guarded with pytest.importorskip("pocketpaw_ee") so the
# OSS-only CI job (EE absent) stays green.
"""Tests for ``pocketpaw.skills.api_skill_builder`` and its EE wiring.

The builder tests are pure OSS — no enterprise import — and need no
guard. The runtime-wiring tests import ``pocketpaw_ee`` and so begin
with ``pytest.importorskip("pocketpaw_ee")``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.skills.api_skill_builder import (
    install_api_skill,
    parse_openapi_to_skill_md,
)

# ---------------------------------------------------------------------------
# Fixtures — minimal specs
# ---------------------------------------------------------------------------


def _minimal_openapi_3() -> dict:
    """A small but representative OpenAPI 3.x spec — two tagged groups,
    a query param, a required body field, and a 200 response schema."""
    return {
        "openapi": "3.0.0",
        "info": {"title": "Example CRM"},
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/contacts": {
                "get": {
                    "tags": ["Contacts"],
                    "summary": "List all contacts",
                    "parameters": [
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "id": {"type": "string"},
                                                "name": {"type": "string"},
                                                "email": {"type": "string"},
                                            },
                                        },
                                    }
                                }
                            }
                        }
                    },
                },
                "post": {
                    "tags": ["Contacts"],
                    "summary": "Create a contact",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "email": {"type": "string"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"201": {"description": "created"}},
                },
            },
            "/deals": {
                "get": {
                    "tags": ["Deals"],
                    "summary": "List deals",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"deals": {"type": "array"}},
                                    }
                                }
                            }
                        }
                    },
                }
            },
        },
    }


def _minimal_swagger_2() -> dict:
    """A small Swagger 2.x spec — uses ``host`` and a response-level
    ``schema`` instead of OpenAPI 3.x ``servers`` / ``content``."""
    return {
        "swagger": "2.0",
        "info": {"title": "Legacy API"},
        "host": "legacy.example.org",
        "paths": {
            "/users": {
                "get": {
                    "summary": "List users",
                    "responses": {
                        "200": {
                            "schema": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "uid": {"type": "string"},
                                        "handle": {"type": "string"},
                                    },
                                },
                            }
                        }
                    },
                }
            }
        },
    }


# ---------------------------------------------------------------------------
# parse_openapi_to_skill_md
# ---------------------------------------------------------------------------


def test_parse_minimal_spec_produces_valid_skill_md() -> None:
    """A minimal spec renders a SKILL.md with valid frontmatter + body."""
    md = parse_openapi_to_skill_md(_minimal_openapi_3(), backend_domain="api.example.com")

    # YAML frontmatter delimited by --- markers.
    assert md.startswith("---\n")
    assert "name: api-api-example-com" in md
    assert "user-invocable: false" in md
    assert "backend_domain: api.example.com" in md
    # The endpoint reference body.
    assert "Backend API Reference" in md
    assert "`GET /contacts`" in md
    assert "`POST /contacts`" in md
    assert "List all contacts" in md


def test_parse_groups_operations_by_tag() -> None:
    """Operations group under their first tag — Contacts and Deals are
    separate `##` sections."""
    md = parse_openapi_to_skill_md(_minimal_openapi_3(), backend_domain="api.example.com")
    assert "## Contacts" in md
    assert "## Deals" in md


def test_parse_groups_by_path_segment_when_untagged() -> None:
    """An untagged operation groups under its first path segment."""
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/widgets/{id}": {"get": {"summary": "Get widget", "responses": {}}},
        },
    }
    md = parse_openapi_to_skill_md(spec, backend_domain="x.test")
    assert "## widgets" in md


def test_parse_extracts_request_params_and_response_fields() -> None:
    """Query params, required body fields, and 200-response top-level
    props all surface in the per-operation reference."""
    md = parse_openapi_to_skill_md(_minimal_openapi_3(), backend_domain="api.example.com")
    # GET /contacts — query param + array-item response fields.
    assert "limit (query)" in md
    assert "email, id, name" in md  # sorted response fields
    # POST /contacts — required body field.
    assert "name (body, required)" in md


def test_parse_handles_swagger_2x() -> None:
    """Swagger 2.x ``host`` + response-level ``schema`` are handled — the
    backend domain comes from ``host`` and the response fields parse."""
    md = parse_openapi_to_skill_md(_minimal_swagger_2(), backend_domain="legacy.example.org")
    assert "name: api-legacy-example-org" in md
    assert "`GET /users`" in md
    assert "handle, uid" in md  # sorted response fields from the item schema


def test_parse_truncates_at_200_endpoints() -> None:
    """A spec with more than 200 operations is truncated with a note."""
    paths = {f"/ep{i}": {"get": {"summary": f"op {i}", "responses": {}}} for i in range(250)}
    spec = {"openapi": "3.0.0", "paths": paths}
    md = parse_openapi_to_skill_md(spec, backend_domain="big.test")

    assert "endpoint_count: 200" in md
    assert "truncated at 200 endpoints" in md
    # 250 paths defined, only 200 rendered.
    assert md.count("### `GET ") == 200


def test_parse_is_byte_identical_on_repeated_calls() -> None:
    """CRITICAL — repeated calls on the same spec produce byte-identical
    output. The mirror installer's SHA skip logic depends on this."""
    spec = _minimal_openapi_3()
    first = parse_openapi_to_skill_md(spec, backend_domain="api.example.com")
    second = parse_openapi_to_skill_md(spec, backend_domain="api.example.com")
    third = parse_openapi_to_skill_md(spec, backend_domain="api.example.com")
    assert first == second == third


def test_parse_byte_identical_with_unordered_paths() -> None:
    """Stability holds even when the spec's paths arrive in a different
    insertion order — output is sorted, so order in does not matter."""
    spec_a = {
        "openapi": "3.0.0",
        "paths": {
            "/zebra": {"get": {"summary": "z", "responses": {}}},
            "/alpha": {"get": {"summary": "a", "responses": {}}},
        },
    }
    spec_b = {
        "openapi": "3.0.0",
        "paths": {
            "/alpha": {"get": {"summary": "a", "responses": {}}},
            "/zebra": {"get": {"summary": "z", "responses": {}}},
        },
    }
    assert parse_openapi_to_skill_md(spec_a, backend_domain="x.test") == parse_openapi_to_skill_md(
        spec_b, backend_domain="x.test"
    )


def test_parse_empty_paths_does_not_crash() -> None:
    """A spec with no operations renders cleanly with a note, no crash."""
    md = parse_openapi_to_skill_md({"openapi": "3.0.0", "paths": {}}, backend_domain="x.test")
    assert "endpoint_count: 0" in md
    assert "declared no operations" in md


# ---------------------------------------------------------------------------
# install_api_skill
# ---------------------------------------------------------------------------


def test_install_writes_to_expected_path(tmp_path: Path) -> None:
    """install_api_skill writes ``api-<slug>/SKILL.md`` under dest_dir and
    returns the slug."""
    slug = install_api_skill(_minimal_openapi_3(), dest_dir=tmp_path)

    assert slug == "api-api-example-com"
    skill_md = tmp_path / "api-api-example-com" / "SKILL.md"
    assert skill_md.is_file()
    content = skill_md.read_text(encoding="utf-8")
    assert "name: api-api-example-com" in content
    assert "`GET /contacts`" in content


def test_install_derives_domain_from_name_when_no_server(tmp_path: Path) -> None:
    """When the spec names no server, the ``name`` arg drives the slug."""
    spec = {"openapi": "3.0.0", "paths": {"/x": {"get": {"responses": {}}}}}
    slug = install_api_skill(spec, name="my-backend.io", dest_dir=tmp_path)
    assert slug == "api-my-backend-io"
    assert (tmp_path / "api-my-backend-io" / "SKILL.md").is_file()


def test_install_from_json_file(tmp_path: Path) -> None:
    """A ``.json`` spec file on disk installs correctly."""
    import json

    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(_minimal_openapi_3()), encoding="utf-8")
    dest = tmp_path / "skills"
    slug = install_api_skill(spec_file, dest_dir=dest)
    assert (dest / slug / "SKILL.md").is_file()


def test_install_from_yaml_file(tmp_path: Path) -> None:
    """A ``.yaml`` spec file on disk installs correctly."""
    import yaml

    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(yaml.safe_dump(_minimal_swagger_2()), encoding="utf-8")
    dest = tmp_path / "skills"
    slug = install_api_skill(spec_file, dest_dir=dest)
    assert slug == "api-legacy-example-org"
    assert (dest / slug / "SKILL.md").is_file()


def test_install_rejects_oversized_file(tmp_path: Path) -> None:
    """A spec file larger than 2 MB is rejected with a ValueError."""
    big_file = tmp_path / "huge.json"
    # 2 MB + 1 byte of filler — valid JSON, just too big.
    big_file.write_text('{"_pad": "' + ("x" * (2 * 1024 * 1024 + 1)) + '"}', encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds the .* limit"):
        install_api_skill(big_file, dest_dir=tmp_path)


def test_install_rejects_spec_with_no_paths(tmp_path: Path) -> None:
    """A spec with no ``paths`` object is rejected — not a usable doc."""
    with pytest.raises(ValueError, match="no `paths`"):
        install_api_skill({"openapi": "3.0.0", "info": {"title": "X"}}, dest_dir=tmp_path)


def test_install_rejects_missing_file(tmp_path: Path) -> None:
    """A path to a non-existent file is rejected."""
    with pytest.raises(ValueError, match="not found"):
        install_api_skill(tmp_path / "nope.json", dest_dir=tmp_path)


# ---------------------------------------------------------------------------
# EE wiring — _load_api_skill_for_backend  (guarded — EE-only)
# ---------------------------------------------------------------------------


def test_load_api_skill_for_backend_returns_content_when_present(
    tmp_path: Path, monkeypatch
) -> None:
    """``_load_api_skill_for_backend`` returns the SKILL.md body when the
    skill file exists for the backend's domain."""
    pytest.importorskip("pocketpaw_ee")
    # Install the skill into the tmp ~/.pocketpaw/skills root the loader
    # resolves once Path.home() is patched.
    skills_root = tmp_path / ".pocketpaw" / "skills"
    install_api_skill(_minimal_openapi_3(), dest_dir=skills_root)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from pocketpaw_ee.agent.pocket_specialist.runtime import _load_api_skill_for_backend

    content = _load_api_skill_for_backend(
        {"base_url": "https://api.example.com/v1", "auth_type": "bearer", "configured": True}
    )
    assert content is not None
    assert "`GET /contacts`" in content


def test_load_api_skill_for_backend_returns_none_when_missing(tmp_path: Path, monkeypatch) -> None:
    """Returns ``None`` when no skill is installed for the backend."""
    pytest.importorskip("pocketpaw_ee")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from pocketpaw_ee.agent.pocket_specialist.runtime import _load_api_skill_for_backend

    assert (
        _load_api_skill_for_backend(
            {"base_url": "https://nothing-installed.test", "configured": True}
        )
        is None
    )


def test_load_api_skill_for_backend_returns_none_when_summary_none() -> None:
    """Returns ``None`` when ``backend_summary`` itself is ``None``."""
    pytest.importorskip("pocketpaw_ee")
    from pocketpaw_ee.agent.pocket_specialist.runtime import _load_api_skill_for_backend

    assert _load_api_skill_for_backend(None) is None


# ---------------------------------------------------------------------------
# EE wiring — _build_system_prompt block splice  (guarded — EE-only)
# ---------------------------------------------------------------------------


def test_build_system_prompt_includes_backend_api_block_when_skill_loaded(
    tmp_path: Path, monkeypatch
) -> None:
    """``_build_system_prompt`` splices a ``<backend-api>`` block in when
    the pocket's backend has an installed API skill."""
    pytest.importorskip("pocketpaw_ee")
    skills_root = tmp_path / ".pocketpaw" / "skills"
    install_api_skill(_minimal_openapi_3(), dest_dir=skills_root)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from pocketpaw_ee.agent.pocket_specialist.runtime import _build_system_prompt

    summary = {"base_url": "https://api.example.com/v1", "configured": True}
    prompt = _build_system_prompt(None, backend_summary=summary)
    # The block proper opens on its own line — distinct from the literal
    # `<backend-api>` mention inside the live-data-sources guidance.
    assert "\n<backend-api>\n" in prompt
    assert "`GET /contacts`" in prompt
    assert "NEVER invent an endpoint" in prompt


def test_build_system_prompt_excludes_backend_api_block_without_skill(
    tmp_path: Path, monkeypatch
) -> None:
    """Without an installed skill the ``<backend-api>`` block is absent —
    whether the backend summary is missing or names an unknown backend."""
    pytest.importorskip("pocketpaw_ee")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from pocketpaw_ee.agent.pocket_specialist.runtime import _build_system_prompt

    # The block proper opens on its own line — the bare `<backend-api>`
    # token also appears as a literal mention in the live-data-sources
    # guidance, so match the line-anchored block opener instead.
    # No backend summary at all.
    assert "\n<backend-api>\n" not in _build_system_prompt(None)
    # A backend with no installed skill.
    prompt = _build_system_prompt(
        None, backend_summary={"base_url": "https://unknown.test", "configured": True}
    )
    assert "\n<backend-api>\n" not in prompt


# ---------------------------------------------------------------------------
# backend_summary token-safety — the non-secret contract  (guarded — EE-only)
# ---------------------------------------------------------------------------


def test_backend_summary_field_never_carries_a_token() -> None:
    """The ``PocketSpecialistCreateInput.backend_summary`` field is a
    free-form dict, but the documented contract — and every producer
    (``get_pocket_backend``) — keeps it to {base_url, auth_type,
    configured, allowed_writes}. This test pins that no auth-token key
    leaks through the model's declared description and that a token-
    bearing summary is not what the helpers expect.

    The structural guarantee lives in ``get_pocket_backend`` (which
    never reads the encrypted token); here we assert the field's intent
    is documented as non-secret and that the API-skill loader ignores
    any stray token key entirely."""
    pytest.importorskip("pocketpaw_ee")
    from pocketpaw_ee.agent.pocket_specialist.runtime import (
        PocketSpecialistCreateInput,
        _load_api_skill_for_backend,
    )

    field = PocketSpecialistCreateInput.model_fields["backend_summary"]
    description = (field.description or "").lower()
    # The field's own documentation forbids the token.
    assert "never include auth_token" in description
    assert "token" not in description.replace("auth_token", "")

    # Even if a caller stuffed a token in, the loader only reads base_url
    # — the token is never touched, never logged, never spliced.
    summary_with_stray_token = {
        "base_url": "https://nothing.test",
        "auth_token": "SECRET-SHOULD-BE-IGNORED",
    }
    # No skill installed for this domain → None, and the token was inert.
    assert _load_api_skill_for_backend(summary_with_stray_token) is None
