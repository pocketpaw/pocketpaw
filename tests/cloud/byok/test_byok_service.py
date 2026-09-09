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
            # A gateway's address and model id (2026-09-09). Both are public by
            # construction and neither needs a decrypt to read, which is the
            # bar this allow-list exists to make people clear.
            "base_url",
            "model",
            "last4",
            "key_hint",
            "last_verified_at",
            "last_error",
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


# ── Custom OpenAI-compatible gateway (2026-09-09, feat/byok-custom-gateway) ──
#
# A gateway key is a stranger's URL that our server then makes requests to, so
# the tests that matter are the two that stop it being an SSRF, plus the one
# that proves a gateway turn is actually pointed at the gateway rather than
# quietly running on the platform's own credentials.

_GATEWAY_KEY = "xpl_" + "a" * 40
_GATEWAY_URL = "https://api.experientiallabs.ai/v1"


class TestGatewayRequestValidation:
    """The DTO edge is where a bad gateway shape must die."""

    def test_accepts_a_gateway_key_with_a_url_and_a_model(self):
        req = ByokSetRequest(
            provider="openai_compatible",
            api_key=_GATEWAY_KEY,
            base_url=_GATEWAY_URL,
            model="claude-opus-5",
        )
        assert req.base_url == _GATEWAY_URL
        assert req.model == "claude-opus-5"

    def test_a_gateway_key_needs_no_anthropic_prefix(self):
        # The whole point: a gateway mints its own key format.
        req = ByokSetRequest(
            provider="openai_compatible",
            api_key=_GATEWAY_KEY,
            base_url=_GATEWAY_URL,
            model="gpt-5.5",
        )
        assert req.api_key == _GATEWAY_KEY

    def test_rejects_a_gateway_key_with_no_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            ByokSetRequest(
                provider="openai_compatible", api_key=_GATEWAY_KEY, model="claude-opus-5"
            )

    def test_rejects_a_gateway_key_with_no_model(self):
        with pytest.raises(ValueError, match="model"):
            ByokSetRequest(
                provider="openai_compatible", api_key=_GATEWAY_KEY, base_url=_GATEWAY_URL
            )

    def test_rejects_an_anthropic_key_carrying_a_base_url(self):
        # A URL that would be silently ignored is worse than a refusal: the
        # user believes they configured a gateway and every turn goes elsewhere.
        with pytest.raises(ValueError, match="openai_compatible"):
            ByokSetRequest(api_key=_REAL_KEY, base_url=_GATEWAY_URL)

    def test_rejects_an_unknown_provider(self):
        with pytest.raises(ValueError, match="provider must be"):
            ByokSetRequest(provider="cohere", api_key=_GATEWAY_KEY)


class TestGatewayUrlIsAnSsrfBoundary:
    """The base URL is typed input from a signed-out stranger, and the server
    dials it. Every one of these is a real target somebody would try."""

    def _with_url(self, url: str):
        return ByokSetRequest(
            provider="openai_compatible",
            api_key=_GATEWAY_KEY,
            base_url=url,
            model="claude-opus-5",
        )

    def test_rejects_the_cloud_metadata_address(self):
        # 169.254.169.254 hands out instance credentials on most clouds.
        with pytest.raises(ValueError):
            self._with_url("https://169.254.169.254/v1")

    def test_rejects_loopback(self):
        with pytest.raises(ValueError):
            self._with_url("https://127.0.0.1:8000/v1")

    def test_rejects_a_private_network_address(self):
        with pytest.raises(ValueError):
            self._with_url("https://10.0.0.5/v1")

    def test_rejects_plain_http(self):
        # A key in a header over http is a key on the wire.
        with pytest.raises(ValueError):
            self._with_url("http://api.experientiallabs.ai/v1")

    def test_strips_a_trailing_slash_so_the_path_join_is_predictable(self):
        assert self._with_url(_GATEWAY_URL + "/").base_url == _GATEWAY_URL


class TestGatewayTurnIsPointedAtTheGateway:
    """The override IS the feature. If it is empty or partial the turn runs on
    platform credentials and the user's key is never spent — a billing
    surprise for us, and silence for them."""

    def _creds(self):
        return byok.TurnCredentials(
            source="byok",
            api_key=_GATEWAY_KEY,
            provider="openai_compatible",
            base_url=_GATEWAY_URL,
            model="claude-opus-5",
        )

    def test_the_override_carries_url_key_and_model(self):
        o = byok.build_settings_override(self._creds())
        assert o["openai_compatible_base_url"] == _GATEWAY_URL
        assert o["openai_compatible_api_key"] == _GATEWAY_KEY
        assert o["openai_compatible_model"] == "claude-opus-5"

    def test_the_override_pins_the_provider_and_the_model(self):
        # pydantic_ai reads pydantic_ai_model FIRST; leaving it alone lets the
        # agent's configured claude-* name win and the gateway 404s on a model
        # it never heard of.
        o = byok.build_settings_override(self._creds())
        assert o["pydantic_ai_provider"] == "openai_compatible"
        assert o["pydantic_ai_model"] == "claude-opus-5"

    def test_a_gateway_turn_does_not_forward_an_x_api_key(self):
        # That header is the LiteLLM path. Sending both would let the proxy
        # bill the user for a call that never reached their gateway.
        assert "byok_provider_api_key" not in byok.build_settings_override(self._creds())

    def test_an_anthropic_turn_is_unchanged(self):
        creds = byok.TurnCredentials(source="byok", api_key=_REAL_KEY, provider="anthropic")
        assert byok.build_settings_override(creds) == {"byok_provider_api_key": _REAL_KEY}

    def test_the_settings_the_override_names_all_exist(self):
        # A typo'd key in the override dict is silently dropped by
        # create_isolated_backend, and the turn runs on platform credentials.
        from pocketpaw.config import Settings

        for field in byok.build_settings_override(self._creds()):
            assert field in Settings.model_fields, field

    def test_a_gateway_model_is_never_second_guessed(self):
        # A gateway's ids are its own namespace; refusing "gpt-5.5" because it
        # is not claude-* would dead-end every non-Anthropic gateway.
        assert byok.provider_allows_model("openai_compatible", "gpt-5.5")
        assert byok.provider_allows_model("openai_compatible", "claude-opus-5")
        assert not byok.provider_allows_model("cohere", "command-r")
