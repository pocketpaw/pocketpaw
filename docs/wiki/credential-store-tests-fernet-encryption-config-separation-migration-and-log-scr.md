---
{
  "title": "Credential Store Tests: Fernet Encryption, Config Separation, Migration, and Log Scrubbing",
  "summary": "This test module validates PocketPaw's `CredentialStore` — the Fernet-encrypted store that keeps API keys off disk in plaintext — along with the separation of secrets from non-secret config, a one-time migration from legacy plaintext `config.json`, strict Unix file permissions, and a `SecretFilter` that scrubs API keys from log output.",
  "concepts": [
    "CredentialStore",
    "Fernet encryption",
    "SECRET_FIELDS",
    "config.json",
    "plaintext migration",
    "salt.bin",
    "file permissions",
    "SecretFilter",
    "log scrubbing",
    "credential separation",
    "Settings.save",
    "Settings.load"
  ],
  "categories": [
    "security",
    "testing",
    "credentials",
    "configuration",
    "test"
  ],
  "source_docs": [
    "2ce025762c8712df"
  ],
  "backlinks": null,
  "word_count": 635,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Early versions of PocketPaw stored API keys in `config.json` alongside non-sensitive settings. If that file was ever committed to version control, shared with support, or readable by a co-tenant process, all credentials were exposed. The `CredentialStore` was built to eliminate that risk, and this test file is the comprehensive specification for its security guarantees.

## Core CRUD (`TestCredentialStore`)

The fixture creates a `CredentialStore` backed by a `tmp_path` directory, isolating each test from disk state. Basic tests verify the full lifecycle: `set()`, `get()`, `overwrite()`, `delete()`, and `get_all()`. `test_get_all_returns_copy` is a subtle but important test — it mutates the returned dict and asserts the store is unaffected, preventing callers from accidentally modifying in-memory state by reference.

## Persistence (`TestCredentialStorePersistence`)

Credentials must survive a `clear_cache()` call (which forces a re-read from disk) and must also be readable from a fresh `CredentialStore` instance pointed at the same directory. This proves the encryption key derivation is deterministic for the same machine — a critical property since restarts create new instances.

## Encryption on Disk (`TestCredentialStoreEncryption`)

Three tests verify that secrets are never written as plaintext:
1. `secrets.enc` exists after the first `set()` call.
2. The file is not valid JSON (it's ciphertext).
3. The raw bytes of the file do not contain the plaintext key value.

A separate `salt.bin` file is created alongside `secrets.enc`. This salt is fed into the KDF so that two machines with the same identity string produce different encryption keys, preventing pre-computed key attacks.

## File Permissions (`TestFilePermissions`, Unix only)

Both `secrets.enc` and `salt.bin` must be readable only by the owner (`0o600`). A world-readable file would expose encrypted ciphertext to co-tenant processes on shared hosts, making the encryption only as strong as the KDF against an attacker who can read the file. The tests skip on Windows (`sys.platform == "win32"`) where POSIX permission bits are not enforced.

## Config Separation (`TestConfigSecretSeparation`)

This is the most architecturally important test class. `Settings.save()` must route fields to two different destinations:
- **Secret fields** (API keys, tokens): go to `CredentialStore`, never appear in `config.json`.
- **Non-secret fields** (timeouts, modes, feature flags): go to `config.json`, never encrypted.

The tests also verify that `Settings.load()` merges both sources, presenting a unified `Settings` object to the rest of the application. `test_save_preserves_existing_secrets` confirms that saving updated non-secret settings doesn't overwrite or clear previously stored credentials.

## Plaintext Migration (`TestPlaintextMigration`)

Users who installed PocketPaw before the credential store existed have API keys in `config.json`. The migration path reads those keys, stores them in the encrypted store, removes them from `config.json`, and writes a `.migrated` flag file. The tests verify:
- Keys are present in the store after migration.
- Keys are absent from `config.json` after migration.
- The flag file is created.
- Migration runs only once (re-running does not fail or duplicate data).
- Migration gracefully handles a missing `config.json`.

## API Key Format Warnings (`TestValidateApiKeys`)

The store calls `validate_api_keys()` during save, which returns format warnings but never blocks the operation. Tests verify that valid keys produce no warnings and that common mismatches (wrong prefix, wrong format) produce actionable warning messages.

## Log Scrubbing (`TestSecretFilter`)

`SecretFilter` is a Python `logging.Filter` that replaces secret values in log records before they are emitted. Tests confirm that Anthropic, OpenAI, Slack, and Telegram tokens are replaced with `"[REDACTED]"` even when they appear in log message format args rather than the pre-formatted string. `test_filter_returns_true` is important: the filter must return `True` to allow the record to pass through (only redacted), not suppress it entirely.

## Known Gaps

No TODO or FIXME markers are present. The `SECRET_FIELDS` constant is tested for completeness in `TestSecretFieldsList`, but the test hardcodes the expected set — if new secret fields are added without updating `SECRET_FIELDS`, this test will catch it, but only if the test itself is updated to expect the new field.