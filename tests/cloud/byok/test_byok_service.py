# tests/cloud/byok/test_byok_service.py — the BYOK credential path.
#
# Created 2026-08-28 (feat/other-hand-byok).
#
# Three things are worth testing here and they are all about CONTAINMENT, not
# about CRUD working:
#
#   1. The key never leaves through the API surface. Status is built from
#      display columns and must not carry the credential in any field.
#   2. A stored key round-trips through the Fernet envelope, and a rotated
#      deployment key degrades to platform credentials instead of failing turns.
#   3. Two tenants with two keys cannot share a cached agent — the bleed case.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core import crypto
from pocketpaw_ee.cloud.byok import service as byok
from pocketpaw_ee.cloud.byok.dto import ByokSetRequest, ByokStatus

_REAL_KEY = "sk-ant-api03-" + "z" * 40


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())


class TestRequestValidation:
    """Reject at the edge what the provider would reject after a round trip."""

    def test_accepts_a_well_formed_anthropic_key(self):
        assert ByokSetRequest(api_key=_REAL_KEY).api_key == _REAL_KEY

    def test_rejects_a_key_from_the_wrong_provider(self):
        with pytest.raises(ValueError, match="sk-ant-"):
            ByokSetRequest(api_key="sk-proj-" + "a" * 40)

    def test_trims_the_trailing_newline_a_paste_leaves_behind(self):
        # Surrounding whitespace is the most common paste artefact and is not
        # the user making a mistake — strip it and accept the key.
        assert ByokSetRequest(api_key=f"  {_REAL_KEY}\n").api_key == _REAL_KEY

    def test_rejects_a_key_with_whitespace_INSIDE_it(self):
        # This one is not a paste artefact — it is a truncated copy, a wrapped
        # line, or a whole shell command. It would also be an illegal header
        # value, so it can never work.
        with pytest.raises(ValueError, match="whitespace"):
            ByokSetRequest(api_key="sk-ant-api03-" + "z" * 20 + " " + "z" * 20)

    def test_rejects_an_unknown_provider(self):
        with pytest.raises(ValueError, match="anthropic"):
            ByokSetRequest(provider="openai", api_key=_REAL_KEY)


class TestStatusNeverCarriesTheKey:
    def test_no_status_field_can_hold_a_credential(self):
        # Structural, not a spot-check: if someone adds a field that could carry
        # the key, this fails until they think about it.
        allowed = {
            "configured",
            "provider",
            "last4",
            "key_hint",
            "last_verified_at",
            "last_error",
            # The illustration credential (2026-09-11). Same display-only rule:
            # ``image_configured`` is a bool and the other two are a hint and a
            # last-four, none of which needs a decrypt to compute. There is no
            # ``image_verified_at`` because fal cannot be asked whether a key is
            # good without generating an image.
            "image_configured",
            "image_last4",
            "image_key_hint",
            "image_last_error",
        }
        assert set(ByokStatus.model_fields) == allowed

    def test_a_serialized_status_does_not_contain_the_key(self):
        status = ByokStatus(
            configured=True,
            provider="anthropic",
            last4=_REAL_KEY[-4:],
            key_hint="sk-ant-api03",
        )
        assert _REAL_KEY not in status.model_dump_json()


class TestEncryptionEnvelope:
    def test_a_key_round_trips_through_the_envelope(self):
        assert crypto.decrypt(crypto.encrypt(_REAL_KEY)) == _REAL_KEY

    def test_the_stored_form_is_not_the_plaintext(self):
        token = crypto.encrypt(_REAL_KEY)
        assert _REAL_KEY not in token

    @pytest.mark.asyncio
    async def test_a_rotated_deployment_key_degrades_to_platform(self, monkeypatch):
        # The failure mode this prevents: the operator rotates
        # CLOUD_ENCRYPTION_KEY, every stored BYOK row becomes undecryptable, and
        # every affected user's turns start FAILING. Degrading to platform
        # credentials means they re-enter the key from a working product.
        from cryptography.fernet import Fernet

        token = crypto.encrypt(_REAL_KEY)
        monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())

        class _Row:
            encrypted_key = token
            provider = "anthropic"

        # Stand in for the Beanie document entirely: the real class only grows
        # its queryable ``workspace`` attribute once ``init_beanie`` has run, and
        # this test is about the decrypt-failure branch, not about Mongo.
        class _StubDoc:
            workspace = "workspace"

            @staticmethod
            async def find_one(*_a, **_k):
                return _Row()

        monkeypatch.setattr(byok, "ByokProviderKey", _StubDoc)
        creds = await byok.resolve_turn_credentials("ws-1")
        assert creds.source == "platform"
        assert creds.api_key is None


class TestSettingsOverride:
    def test_platform_credentials_change_nothing(self):
        creds = byok.TurnCredentials(source="platform")
        assert byok.build_settings_override(creds) == {}

    def test_byok_credentials_carry_the_key_to_the_backend(self):
        creds = byok.TurnCredentials(source="byok", api_key=_REAL_KEY)
        assert byok.build_settings_override(creds) == {"byok_provider_api_key": _REAL_KEY}

    def test_a_byok_source_with_no_key_is_treated_as_platform(self):
        # Belt and braces: a malformed TurnCredentials must not produce an
        # override that blanks the platform key and breaks the turn.
        creds = byok.TurnCredentials(source="byok", api_key=None)
        assert byok.build_settings_override(creds) == {}


class TestNoCredentialBleedBetweenTenants:
    """The incident case: tenant B's turn billed to tenant A's Anthropic account."""

    def _fingerprint_for(self, key: str) -> str:
        from pocketpaw.agents.pydantic_ai import PydanticAIBackend
        from pocketpaw.config import Settings

        backend = PydanticAIBackend.__new__(PydanticAIBackend)
        backend.settings = Settings(byok_provider_api_key=key)
        return backend._credential_fingerprint()

    def test_two_tenants_keys_produce_different_cache_identities(self):
        a = self._fingerprint_for("sk-ant-api03-" + "a" * 40)
        b = self._fingerprint_for("sk-ant-api03-" + "b" * 40)
        assert a != b, "two BYOK tenants would share one cached agent"

    def test_the_same_key_is_stable_across_calls(self):
        # A fingerprint that varied per call would defeat the cache entirely —
        # correct, but every turn would rebuild the agent.
        assert self._fingerprint_for(_REAL_KEY) == self._fingerprint_for(_REAL_KEY)

    def test_no_key_is_its_own_bucket_not_a_hash(self):
        assert self._fingerprint_for("") == "none"

    def test_the_fingerprint_does_not_leak_the_key(self):
        fp = self._fingerprint_for(_REAL_KEY)
        assert _REAL_KEY not in fp
        assert len(fp) < len(_REAL_KEY)


class TestTheHeaderThatCarriesTheKey:
    """LiteLLM's BYOK path is a forwarded ``x-api-key`` header, so the header
    landing on the HTTP client IS the feature. Everything else is plumbing."""

    def _client_for(self, key: str | None):
        from pocketpaw.agents.pydantic_ai import PydanticAIBackend
        from pocketpaw.config import Settings

        backend = PydanticAIBackend.__new__(PydanticAIBackend)
        backend.settings = Settings(byok_provider_api_key=key)
        backend._http_client = None
        return backend._get_http_client()

    def test_a_byok_run_sends_the_users_key_as_x_api_key(self):
        client = self._client_for(_REAL_KEY)
        assert client is not None
        assert client.headers.get("x-api-key") == _REAL_KEY

    def test_a_platform_run_sends_no_provider_header(self):
        # The regression that would silently bill everyone to one account: a
        # stale header surviving onto a run that never asked for BYOK.
        client = self._client_for(None)
        assert client is not None
        assert "x-api-key" not in client.headers

    def test_an_empty_key_is_not_forwarded_as_a_blank_header(self):
        # A blank x-api-key is worse than none: the proxy forwards it and the
        # provider rejects the request with a confusing auth error.
        client = self._client_for("   ")
        assert client is not None
        assert "x-api-key" not in client.headers


# ---------------------------------------------------------------------------
# The illustration credential (2026-09-11, feat/byok-image-key).
#
# One row now holds TWO independent credentials, which creates exactly one new
# way to lose data: removing one taking the other with it. ``delete_key`` used
# to drop the whole document, and a user rotating their Anthropic key would
# have silently lost their illustrator.
# ---------------------------------------------------------------------------

from pocketpaw_ee.cloud.byok.dto import ByokImageKeyRequest  # noqa: E402

_FAL_KEY = "11111111-2222-3333-4444-555555555555:" + "f" * 32


class TestImageKeyValidation:
    def test_accepts_the_shape_fal_actually_issues(self):
        assert ByokImageKeyRequest(api_key=_FAL_KEY).api_key == _FAL_KEY

    def test_rejects_a_key_with_no_secret_half(self):
        with pytest.raises(ValueError, match="key-id"):
            ByokImageKeyRequest(api_key="11111111-2222-3333-4444-555555555555")

    def test_rejects_a_whole_shell_command(self):
        with pytest.raises(ValueError, match="whitespace"):
            ByokImageKeyRequest(api_key='export FAL_KEY="abc:def"')


class TestImageKeyDisplayColumns:
    """The hint must name the key WITHOUT naming the secret.

    ``_hint`` splits on ``-``, so reusing it on a fal key would print three
    segments of the UUID and tell the user nothing. Worse, a naive "first N
    characters" would be right up until fal changed its format.
    """

    def test_the_hint_is_the_key_id_and_never_the_secret(self):
        hint = byok._fal_hint(_FAL_KEY)
        assert hint == "11111111-2222-3333-4444-555555555555"
        assert "f" * 32 not in hint

    def test_last4_comes_from_the_secret_so_two_keys_sharing_an_id_differ(self):
        a = "same-id:" + "a" * 20 + "abcd"
        b = "same-id:" + "a" * 20 + "wxyz"
        assert byok._fal_last4(a) != byok._fal_last4(b)

    def test_a_key_of_an_unexpected_shape_yields_no_hint_rather_than_a_guess(self):
        assert byok._fal_hint("nocolonhere") == ""


class TestTheTwoCredentialsAreIndependent:
    """The data-loss case. Both directions, because both are one line of code
    apart from being wrong."""

    @pytest.mark.asyncio
    async def test_removing_the_llm_key_keeps_the_image_key(self, monkeypatch):
        saved: dict[str, object] = {}
        deleted: list[bool] = []

        class _Row:
            workspace = "ws-1"
            encrypted_key = "llm-token"
            last4 = "zzzz"
            key_hint = "sk-ant-api03"
            base_url = None
            model = None
            provider = "anthropic"
            last_verified_at = None
            last_error = None
            image_encrypted_key = "image-token"
            image_last4 = "ffff"
            image_key_hint = "key-id"
            image_last_error = None

            async def save(self):
                saved["encrypted_key"] = self.encrypted_key
                saved["image_encrypted_key"] = self.image_encrypted_key

            async def delete(self):
                deleted.append(True)

        row = _Row()

        class _StubDoc:
            workspace = "workspace"

            @staticmethod
            async def find_one(*_a, **_k):
                return row

        monkeypatch.setattr(byok, "ByokProviderKey", _StubDoc)
        await byok.delete_key("ws-1")

        assert deleted == [], "the row was dropped, taking the image key with it"
        assert saved["encrypted_key"] == "", "the LLM key was not actually removed"
        assert saved["image_encrypted_key"] == "image-token"

    @pytest.mark.asyncio
    async def test_removing_the_image_key_keeps_the_llm_key(self, monkeypatch):
        saved: dict[str, object] = {}
        deleted: list[bool] = []

        class _Row:
            workspace = "ws-1"
            encrypted_key = "llm-token"
            last4 = "zzzz"
            key_hint = "sk-ant-api03"
            base_url = None
            model = None
            provider = "anthropic"
            last_verified_at = None
            last_error = None
            image_encrypted_key = "image-token"
            image_last4 = "ffff"
            image_key_hint = "key-id"
            image_last_error = None

            async def save(self):
                saved["encrypted_key"] = self.encrypted_key
                saved["image_encrypted_key"] = self.image_encrypted_key

            async def delete(self):
                deleted.append(True)

        row = _Row()

        class _StubDoc:
            workspace = "workspace"

            @staticmethod
            async def find_one(*_a, **_k):
                return row

        monkeypatch.setattr(byok, "ByokProviderKey", _StubDoc)
        await byok.delete_image_key("ws-1")

        assert deleted == [], "the row was dropped, taking the LLM key with it"
        assert saved["image_encrypted_key"] is None
        assert saved["encrypted_key"] == "llm-token"


class TestImageKeyResolution:
    @pytest.mark.asyncio
    async def test_an_unreadable_row_reads_as_no_key_rather_than_raising(self, monkeypatch):
        """Same posture as the LLM key's rotated-Fernet case, for a smaller
        reason: a picture is not worth failing a turn over, so an unreadable
        image key falls back to the platform's instead of raising inside a tool
        call the agent is in the middle of."""
        from cryptography.fernet import Fernet

        token = crypto.encrypt(_FAL_KEY)
        monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())

        class _Row:
            image_encrypted_key = token

        class _StubDoc:
            workspace = "workspace"

            @staticmethod
            async def find_one(*_a, **_k):
                return _Row()

        monkeypatch.setattr(byok, "ByokProviderKey", _StubDoc)
        assert await byok.resolve_image_key("ws-1") is None

    @pytest.mark.asyncio
    async def test_no_workspace_never_touches_the_database(self, monkeypatch):
        class _Exploding:
            workspace = "workspace"

            @staticmethod
            async def find_one(*_a, **_k):
                raise AssertionError("a keyless turn queried for a credential")

        monkeypatch.setattr(byok, "ByokProviderKey", _Exploding)
        assert await byok.resolve_image_key(None) is None
