---
{
  "title": "Credential Store v2 Tests: AES-256-GCM, Argon2id, AEAD, Migration, and Platform Fallbacks",
  "summary": "This test module validates the v2 upgrade of PocketPaw's `CredentialStore`, which replaces Fernet (PBKDF2-based) encryption with AES-256-GCM authenticated encryption using Argon2id key derivation. It covers the full v2 round-trip, authenticated data enforcement, automatic v1-to-v2 migration with backup, corruption recovery, and cross-platform hardware UUID fallbacks used to derive the encryption key.",
  "concepts": [
    "AES-256-GCM",
    "Argon2id",
    "AEAD",
    "VERSION_2_HEADER",
    "CredentialStore v2",
    "v1 migration",
    "backup restoration",
    "hardware UUID",
    "machine identity",
    "Fernet",
    "PBKDF2",
    "key derivation",
    "CredentialMigrationError"
  ],
  "categories": [
    "security",
    "testing",
    "credentials",
    "encryption",
    "test"
  ],
  "source_docs": [
    "7abc46ce57775af1"
  ],
  "backlinks": null,
  "word_count": 646,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The v1 `CredentialStore` used Fernet (AES-128-CBC + HMAC-SHA256) with PBKDF2-SHA256 key derivation. While secure, it had two weaknesses: PBKDF2 is GPU-friendly (making brute-force faster than Argon2id on stolen files), and Fernet does not bind ciphertext to any context, allowing an attacker who obtains the ciphertext to attempt decryption on a different machine. The v2 upgrade addresses both.

## V2 Encryption Scheme

V2 uses **AES-256-GCM** (authenticated encryption with associated data, AEAD) with **Argon2id** key derivation. The key is derived from a machine-specific identity string that includes hardware UUID, machine ID, and login name — making the key tied to the specific machine. The `VERSION_2_HEADER` bytes are bound as associated data (AAD) to the GCM tag, so a file with a modified or stripped header cannot be decrypted even if the key is known.

### Round-Trip Tests (`TestV2RoundTrip`)

`test_basic_round_trip` calls `set()`, then `clear_cache()` to force a disk re-read, then `get()`. This proves the entire encrypt→persist→read→decrypt cycle works. `test_v2_header_present` reads the raw bytes of `secrets.enc` and asserts the file begins with `VERSION_2_HEADER`, confirming the format upgrade happened. `test_new_instance_reads_v2` creates a second `CredentialStore` at the same path — proving the key derivation is reproducible on the same machine.

### AEAD Enforcement (`TestAEAD`)

`test_aad_used_in_encryption` reads the raw ciphertext, surgically replaces the AAD bytes while leaving the GCM tag intact, and asserts that decryption fails. This test exists because AEAD is only valuable if the AAD is actually passed during decryption — a common implementation mistake is to encrypt with AAD but decrypt without it, silently ignoring the binding.

`test_tampered_header_fails` modifies a single byte of the version header and asserts `get()` returns `{}` rather than raising an unhandled exception. Corruption must degrade gracefully.

### V1 Migration (`TestV1Migration`)

The test helpers `_build_v1_identity` and `_write_v1_secrets` reproduce the exact v1 Fernet encryption logic to create a realistic legacy file. `test_v1_migrates_to_v2` writes a v1 file, then calls `get()` on a v2-capable store and asserts both that the data is correctly returned and that the on-disk file has been rewritten in v2 format.

`test_migration_creates_backup` asserts a `secrets.enc.v1_backup` file exists after migration, giving operators a recovery path if the migration is faulty.

`test_migration_preserves_all_data` stores multiple keys in the v1 format and confirms all survive migration — preventing partial migrations that lose some keys.

### Migration Failure Recovery (`TestMigrationFailure`)

`test_bad_v1_key_restores_backup` simulates a corrupted v1 file (wrong identity bytes → wrong key → decryption failure). The store must restore the original file from backup and return an empty dict, rather than overwriting the backup with a corrupted v2 file. This test protects against the worst-case scenario: a migration bug that bricks a user's credentials.

### Corrupt Format Handling (`TestCorruptFormat`)

Three tests cover files that are neither valid v1 nor valid v2:
- Random bytes → empty dict returned.
- Empty file → empty dict returned.
- Truncated v2 file (header present but ciphertext cut off) → empty dict returned.

All cases must not raise exceptions, ensuring the agent can start even with a corrupted credential file and prompt the operator to re-enter credentials.

### V1 Identity Compatibility (`TestV1IdentityCompat`)

The v2 identity string extends the v1 identity with an additional hardware UUID component. Tests assert the v1 identity has exactly two `|`-separated parts and the v2 identity has three, and that the first and last parts are shared — ensuring the migration code can reconstruct the v1 key from a v2 store instance.

### Hardware UUID Fallbacks (`TestHardwareUUIDFallbacks`)

On macOS, `_get_macos_hardware_uuid()` runs `system_profiler` to get a stable hardware UUID. In CI environments (`CI=true`, `GITHUB_ACTIONS=true`), it returns a known constant instead of a real UUID to ensure deterministic test behavior. On non-Darwin platforms, it returns a fallback constant. The tests patch environment variables to trigger each code path.

## Known Gaps

`CredentialMigrationError` is imported in the test but no test directly asserts it is raised — the migration failure test catches the resulting empty-dict return rather than the exception itself.