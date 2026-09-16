"""``GET/PUT /api/v1/platform/settings`` — chunk 11 of the Paw Admin PRD.

Design: docs/design/drafts/2026-09-15-paw-admin-screen-settings-health.md.

These call the route handler functions directly (not over HTTP), same
convention as ``test_read_audit.py`` — the point under test is "does the
handler do the right thing", not the routing.

Filesystem isolation. ``Settings.load()``/``.save()`` and this route both
resolve ``get_config_dir()`` to ``Path.home() / ".pocketpaw"`` by default, and
the credential store singleton defaults to the same real home directory. Every
test here redirects both through the ``isolated_settings_env`` fixture so
nothing here ever touches a developer's real ``~/.pocketpaw``. Two distinct
bindings need patching, not one: ``config.py`` re-imports
``get_credential_store`` fresh from ``pocketpaw.credentials`` inside
``Settings.load()``/``.save()`` on every call (so patching the module
attribute reaches it), but ``ee/pocketpaw_ee/cloud/platform/settings.py``
imported the same name once at module load (so it needs its own patch). See
``tests/test_credentials.py``'s ``env`` fixture for the identical pattern.
"""

from __future__ import annotations

import json

import pytest
from pocketpaw_ee.cloud._core.errors import ConflictError, Forbidden, ValidationError
from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.user import User as UserDoc
from pocketpaw_ee.cloud.platform import settings as platform_settings
from starlette.datastructures import Headers
from starlette.requests import Request

from pocketpaw import config as pocketpaw_config
from pocketpaw import credentials as pocketpaw_credentials
from pocketpaw.credentials import CredentialStore

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_settings_env(tmp_path, monkeypatch):
    test_store = CredentialStore(config_dir=tmp_path)

    monkeypatch.setattr(pocketpaw_config, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(pocketpaw_config, "_MIGRATION_DONE_PATH", None)
    monkeypatch.setattr(pocketpaw_credentials, "get_credential_store", lambda: test_store)
    monkeypatch.setattr(platform_settings, "get_credential_store", lambda: test_store)

    (tmp_path / ".secrets_migrated").write_text("1")

    pocketpaw_config.get_settings.cache_clear()
    yield tmp_path, test_store
    pocketpaw_config.get_settings.cache_clear()


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/platform/settings",
            "headers": Headers(
                raw=[(b"user-agent", b"paw-admin/test"), (b"x-forwarded-for", b"203.0.113.9")]
            ).raw,
            "query_string": b"",
            "client": ("10.0.0.5", 1234),
        }
    )


async def _user(platform_role: str) -> UserDoc:
    doc = UserDoc(
        email=f"{platform_role}@paw.test",
        hashed_password="x",
        full_name=platform_role.title(),
        platform_role=platform_role,
    )
    await doc.insert()
    return doc


class _FakeGuardUser:
    """Minimal stand-in, matching test_platform_guard.py's ``_FakeUser``."""

    def __init__(self, platform_role: str | None) -> None:
        self.id = "operator-1"
        self.email = "ops@example.com"
        self.platform_role = platform_role


def _guard_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/platform/settings",
            "headers": Headers(raw=[(b"cookie", b"paw_auth=a-real-session-token")]).raw,
            "query_string": b"",
            "client": ("10.0.0.5", 1234),
        }
    )


# ---------------------------------------------------------------------------
# The asymmetry: platform.settings.read is OPERATOR, not SUPPORT
# ---------------------------------------------------------------------------


async def test_support_is_refused_the_settings_read() -> None:
    """The one asymmetry this chunk is built around, from the settings side.

    ``platform.settings.read`` is OPERATOR — unlike every other read in this
    namespace — because the payload includes deployment topology and
    credential shape. See ``ee/pocketpaw_ee/cloud/platform/settings.py``'s
    module docstring for why this is not a bug to fix down to SUPPORT.
    """
    guard = require_platform("platform.settings.read")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await guard(request=_guard_request(), user=_FakeGuardUser("support"))
    assert exc.value.status_code == 403


async def test_operator_is_allowed_the_settings_read() -> None:
    guard = require_platform("platform.settings.read")
    user = _FakeGuardUser("operator")
    assert await guard(request=_guard_request(), user=user) is user


async def test_operator_is_allowed_the_settings_write() -> None:
    guard = require_platform("platform.settings.write")
    user = _FakeGuardUser("operator")
    assert await guard(request=_guard_request(), user=user) is user


async def test_support_is_refused_the_settings_write() -> None:
    guard = require_platform("platform.settings.write")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await guard(request=_guard_request(), user=_FakeGuardUser("support"))
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Read: no secret content, audit trail, catalog rows
# ---------------------------------------------------------------------------


async def test_no_secret_value_appears_anywhere_in_the_read_response(
    mongo_db, isolated_settings_env
) -> None:
    tmp_path, store = isolated_settings_env
    secret_value = "sk-do-not-leak-this-9f8e7d6c5b4a"
    store.set("openai_api_key", secret_value)

    operator = await _user("operator")
    page = await platform_settings.get_platform_settings(request=_request(), operator=operator)

    dumped = page.model_dump_json()
    assert secret_value not in dumped

    field = next(f for f in page.other_fields if f.name == "openai_api_key")
    assert field.is_secret is True
    assert field.value is None
    assert field.default is None
    assert field.is_set is True
    assert field.provenance == "credential_store"


async def test_settings_read_writes_an_audit_row(mongo_db, isolated_settings_env) -> None:
    operator = await _user("operator")
    await platform_settings.get_platform_settings(request=_request(), operator=operator)

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.settings.read"
    assert rows[0].status == "applied"
    assert rows[0].reason.startswith("read:")


async def test_catalog_rows_appear_in_other_fields(mongo_db, isolated_settings_env) -> None:
    operator = await _user("operator")
    page = await platform_settings.get_platform_settings(request=_request(), operator=operator)

    names = {f.name for f in page.other_fields}
    assert "catalog_cache_ttl_seconds" in names
    assert "catalog_models_dev_enabled" in names


async def test_billing_llm_proxy_and_payments_groups_are_built(
    mongo_db, isolated_settings_env
) -> None:
    operator = await _user("operator")
    page = await platform_settings.get_platform_settings(request=_request(), operator=operator)

    keys = {g.key for g in page.groups}
    assert keys == {"billing", "llm_proxy", "payments"}
    llm_group = next(g for g in page.groups if g.key == "llm_proxy")
    assert any(f.name == "litellm_api_base" for f in llm_group.fields)


# ---------------------------------------------------------------------------
# config.json status: missing / unparseable / rejected_by_schema / ok
# ---------------------------------------------------------------------------


async def test_config_json_missing_reports_missing_status(mongo_db, isolated_settings_env) -> None:
    operator = await _user("operator")
    page = await platform_settings.get_platform_settings(request=_request(), operator=operator)
    assert page.config_json.status == "missing"


async def test_config_json_garbage_reports_unparseable(mongo_db, isolated_settings_env) -> None:
    tmp_path, _store = isolated_settings_env
    (tmp_path / "config.json").write_text("{not valid json at all")

    operator = await _user("operator")
    page = await platform_settings.get_platform_settings(request=_request(), operator=operator)
    assert page.config_json.status == "unparseable"


async def test_config_json_schema_violation_reports_rejected_by_schema(
    mongo_db, isolated_settings_env
) -> None:
    tmp_path, _store = isolated_settings_env
    (tmp_path / "config.json").write_text(json.dumps({"billing_enforced": {"nested": True}}))

    operator = await _user("operator")
    page = await platform_settings.get_platform_settings(request=_request(), operator=operator)
    assert page.config_json.status == "rejected_by_schema"


async def test_config_json_valid_reports_ok(mongo_db, isolated_settings_env) -> None:
    tmp_path, _store = isolated_settings_env
    (tmp_path / "config.json").write_text(json.dumps({"litellm_max_tokens": 4096}))

    operator = await _user("operator")
    page = await platform_settings.get_platform_settings(request=_request(), operator=operator)
    assert page.config_json.status == "ok"


# ---------------------------------------------------------------------------
# Write: validation ladder
# ---------------------------------------------------------------------------


async def test_write_refuses_empty_reason(mongo_db, isolated_settings_env) -> None:
    operator = await _user("operator")
    body = platform_settings.SettingsWriteIn(changes={"litellm_max_tokens": 111}, reason="   ")
    with pytest.raises(ValidationError):
        await platform_settings.update_platform_settings(
            body=body, request=_request(), operator=operator
        )


async def test_write_refuses_no_changes(mongo_db, isolated_settings_env) -> None:
    operator = await _user("operator")
    body = platform_settings.SettingsWriteIn(changes={}, reason="testing")
    with pytest.raises(ValidationError):
        await platform_settings.update_platform_settings(
            body=body, request=_request(), operator=operator
        )


async def test_write_refuses_unknown_field(mongo_db, isolated_settings_env) -> None:
    operator = await _user("operator")
    body = platform_settings.SettingsWriteIn(changes={"totally_bogus_field": 1}, reason="testing")
    with pytest.raises(ValidationError):
        await platform_settings.update_platform_settings(
            body=body, request=_request(), operator=operator
        )


async def test_write_refuses_immutable_field(mongo_db, isolated_settings_env) -> None:
    operator = await _user("operator")
    body = platform_settings.SettingsWriteIn(changes={"terminal_enabled": False}, reason="testing")
    with pytest.raises(Forbidden):
        await platform_settings.update_platform_settings(
            body=body, request=_request(), operator=operator
        )


async def test_write_refuses_catalog_locked_field(mongo_db, isolated_settings_env) -> None:
    operator = await _user("operator")
    body = platform_settings.SettingsWriteIn(
        changes={"litellm_api_base": "https://evil.example"}, reason="testing"
    )
    with pytest.raises(Forbidden):
        await platform_settings.update_platform_settings(
            body=body, request=_request(), operator=operator
        )


async def test_write_refuses_env_shadowed_field(
    mongo_db, isolated_settings_env, monkeypatch
) -> None:
    monkeypatch.setenv("POCKETPAW_LITELLM_MAX_TOKENS", "999")
    operator = await _user("operator")
    body = platform_settings.SettingsWriteIn(changes={"litellm_max_tokens": 111}, reason="testing")
    with pytest.raises(ConflictError):
        await platform_settings.update_platform_settings(
            body=body, request=_request(), operator=operator
        )


async def test_write_refuses_invalid_type(mongo_db, isolated_settings_env) -> None:
    operator = await _user("operator")
    body = platform_settings.SettingsWriteIn(
        changes={"litellm_max_tokens": "not-a-number"}, reason="testing"
    )
    with pytest.raises(ValidationError):
        await platform_settings.update_platform_settings(
            body=body, request=_request(), operator=operator
        )


async def test_config_unparseable_refuses_the_whole_write(mongo_db, isolated_settings_env) -> None:
    tmp_path, _store = isolated_settings_env
    (tmp_path / "config.json").write_text("{not valid json")

    operator = await _user("operator")
    body = platform_settings.SettingsWriteIn(changes={"litellm_max_tokens": 111}, reason="testing")
    with pytest.raises(ConflictError):
        await platform_settings.update_platform_settings(
            body=body, request=_request(), operator=operator
        )


# ---------------------------------------------------------------------------
# Write: field-scoped, the L7 mitigation
# ---------------------------------------------------------------------------


async def test_write_is_field_scoped_and_leaves_other_keys_untouched(
    mongo_db, isolated_settings_env
) -> None:
    tmp_path, _store = isolated_settings_env
    (tmp_path / "config.json").write_text(
        json.dumps({"litellm_max_tokens": 111, "billing_markup": 1.5})
    )

    operator = await _user("operator")
    body = platform_settings.SettingsWriteIn(
        changes={"litellm_max_tokens": 222}, reason="bumping token budget"
    )
    result = await platform_settings.update_platform_settings(
        body=body, request=_request(), operator=operator
    )

    assert result.applied == ["litellm_max_tokens"]
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["litellm_max_tokens"] == 222
    # The unrelated key survives untouched — this is the whole point of not
    # calling Settings.save() (L7), which would have re-dumped every field.
    assert on_disk["billing_markup"] == 1.5


async def test_write_reloads_settings_so_the_new_value_is_live(
    mongo_db, isolated_settings_env
) -> None:
    operator = await _user("operator")
    before = pocketpaw_config.get_settings()
    assert before.litellm_max_tokens != 4321

    body = platform_settings.SettingsWriteIn(
        changes={"litellm_max_tokens": 4321}, reason="testing reload"
    )
    await platform_settings.update_platform_settings(
        body=body, request=_request(), operator=operator
    )

    after = pocketpaw_config.get_settings()
    assert after.litellm_max_tokens == 4321


async def test_secret_write_goes_to_the_credential_store_never_to_config_json(
    mongo_db, isolated_settings_env
) -> None:
    tmp_path, store = isolated_settings_env
    body = platform_settings.SettingsWriteIn(
        changes={"anthropic_api_key": "sk-ant-newvalue"}, reason="rotating key"
    )
    await platform_settings.update_platform_settings(
        body=body, request=_request(), operator=await _user("operator")
    )

    assert store.get("anthropic_api_key") == "sk-ant-newvalue"
    config_path = tmp_path / "config.json"
    if config_path.exists():
        on_disk = json.loads(config_path.read_text())
        assert "anthropic_api_key" not in on_disk


# ---------------------------------------------------------------------------
# Write: audit trail, and the secret-masking mitigation
# ---------------------------------------------------------------------------


async def test_write_records_attempted_then_applied(mongo_db, isolated_settings_env) -> None:
    operator = await _user("operator")
    body = platform_settings.SettingsWriteIn(
        changes={"litellm_max_tokens": 555}, reason="raising the ceiling for a launch"
    )
    await platform_settings.update_platform_settings(
        body=body, request=_request(), operator=operator
    )

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "platform.settings.write"
    assert row.status == "applied"
    assert row.reason == "raising the ceiling for a launch"
    assert row.after["litellm_max_tokens"] == 555


async def test_write_audit_never_carries_a_raw_secret_value(
    mongo_db, isolated_settings_env
) -> None:
    """The mitigation for the risk found during this build.

    ``platform.audit.read`` (SUPPORT) is a LOWER rung than
    ``platform.settings.write`` (OPERATOR). Recording a raw secret in
    before/after would let a support operator read a secret back out through a
    route they cannot reach directly.
    """
    operator = await _user("operator")
    secret_value = "sk-ant-do-not-store-me-raw"
    body = platform_settings.SettingsWriteIn(
        changes={"anthropic_api_key": secret_value}, reason="rotating key"
    )
    await platform_settings.update_platform_settings(
        body=body, request=_request(), operator=operator
    )

    row = (await PlatformAuditEvent.find_all().to_list())[0]
    assert secret_value not in json.dumps(row.before)
    assert secret_value not in json.dumps(row.after)
    assert row.after["anthropic_api_key"] == {"is_set": True}
